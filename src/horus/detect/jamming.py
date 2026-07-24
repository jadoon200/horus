"""GNSS-interference detector — the flagship.

Aircraft broadcast their own navigation-integrity figures in every ADS-B message: NIC
(Navigation Integrity Category) and NACp collapse when the aircraft's GNSS solution
degrades. One aircraft with bad NIC is an avionics quirk; *many aircraft in the same
place at the same time* is the well-established open-source signature of GNSS jamming
(the signal behind GPSJam-style maps). This detector aggregates per-aircraft worst-NIC
over a spatial grid x time window and emits an **area-level** incident when a cell's
degraded fraction crosses the threshold.

Small-sample honesty: a cell observing fewer than `gnss_min_aircraft` aircraft is
UNSCOREABLE and is skipped (counted, not scored) — three aircraft can't establish an
area signature. The lone-dip confounder is handled by the fraction + minimum together.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from horus.config import Settings, get_settings
from horus.db.models import Incident, Position
from horus.detect.base import TECH_JAMMING, grade_samples, make_incident
from horus.logging import get_logger
from horus.timeutil import utc_naive

log = get_logger(__name__)

# Time buckets are anchored to a FIXED epoch, never to the corpus minimum. Anchoring to
# "the earliest report we happen to hold" makes bucket boundaries — and therefore incident
# ids and even the incident count — depend on what else is in the database: one unrelated
# report arriving from slightly earlier would re-cut every window. A live collector
# accumulates data continuously, so that is a reproducibility bug, not a theoretical one.
_EPOCH = datetime(1970, 1, 1)


@dataclass
class _CellWindow:
    """Observed aircraft → worst NIC inside one grid cell x time window."""

    worst_nic: dict[str, int] = field(default_factory=dict)
    # Second integrity channel, tracked per aircraft alongside NIC. Healthy traffic never
    # reports NACp 0 (measured), so NIC and NACp collapsing together is a much sharper
    # signature than either alone.
    worst_nac_p: dict[str, int] = field(default_factory=dict)
    ts_min: datetime | None = None
    ts_max: datetime | None = None
    # Region of the data that formed this cell, so an unscoped run still attributes the
    # incident to where the reports came from rather than to the (absent) query filter.
    region: str | None = None


@dataclass
class JammingRunStats:
    """Transparency for the eval/API: what was scoreable at all."""

    cells_seen: int = 0
    cells_unscoreable: int = 0
    incidents: int = 0


def align_to_window(ts: datetime, settings: Settings) -> datetime:
    """Snap a timestamp DOWN to the jamming bucket boundary containing it.

    Buckets are epoch-anchored, so an arbitrary `since` almost always falls mid-bucket.
    An incremental refresh must align its boundary here: a bucket straddling an unaligned
    `since` keeps its stored incident (ts_start before the boundary, so the delete misses
    it) while the scoped detector regenerates the same bucket id from the partial evidence
    — a primary-key collision, reproduced before this helper existed. Aligned, every
    bucket the detector can regenerate is also fully covered by the delete, and no bucket
    is ever scored on partial evidence.
    """
    window = timedelta(minutes=settings.gnss_window_minutes)
    return _EPOCH + ((ts - _EPOCH) // window) * window


def _load_positions(
    session: Session,
    region: str | None,
    since: datetime | None,
    until: datetime | None = None,
) -> list[Position]:
    q = select(Position).where(Position.nic.is_not(None), Position.on_ground.is_(False))
    if region:
        q = q.where(Position.region == region)
    if since is not None:
        q = q.where(Position.ts >= since)
    if until is not None:
        q = q.where(Position.ts <= until)
    return list(session.scalars(q))


def _fine_key(p: Position, cell_deg: float, window: timedelta) -> tuple[int, int, int]:
    ts = utc_naive(p.ts)
    return (
        int(p.lat // cell_deg),
        int(p.lon // cell_deg),
        int((ts - _EPOCH) // window),
    )


def _accumulate(cw: _CellWindow, p: Position) -> None:
    ts = utc_naive(p.ts)
    assert p.nic is not None
    prev = cw.worst_nic.get(p.icao24)
    if prev is None or p.nic < prev:
        cw.worst_nic[p.icao24] = p.nic
    if p.nac_p is not None:
        prev_nac = cw.worst_nac_p.get(p.icao24)
        if prev_nac is None or p.nac_p < prev_nac:
            cw.worst_nac_p[p.icao24] = p.nac_p
    cw.ts_min = ts if cw.ts_min is None or ts < cw.ts_min else cw.ts_min
    cw.ts_max = ts if cw.ts_max is None or ts > cw.ts_max else cw.ts_max
    if cw.region is None:
        cw.region = p.region


def _aggregate_levels(
    positions: list[Position], s: Settings
) -> Iterator[tuple[int, float, tuple[int, int, int], _CellWindow, bool]]:
    """Yield (level, cell_deg, key, cell, scoreable) finest-first, then the unscoreable sky.

    The single source of truth for multi-resolution scoring: the detector and the coverage
    map both consume this, so the map can never disagree with the incidents drawn on it.

    A fixed cell size forces one choice for the whole map, and over real traffic that choice
    is wrong somewhere — at 0.5° over Singapore 83% of cells held too few aircraft. So each
    resolution is tried in turn and a cell is accepted only where it meets the aircraft
    minimum; sky that fails falls through to the next, coarser level. Whatever never
    qualifies at any resolution is yielded last with `scoreable=False` rather than dropped.
    """
    window = timedelta(minutes=s.gnss_window_minutes)
    levels = [s.gnss_cell_deg * (2**k) for k in range(s.gnss_coarsen_levels + 1)]
    claimed: set[tuple[int, int, int]] = set()

    for level, cell_deg in enumerate(levels):
        cells: dict[tuple[int, int, int], _CellWindow] = {}
        members: dict[tuple[int, int, int], set[tuple[int, int, int]]] = {}
        for p in positions:
            fine = _fine_key(p, s.gnss_cell_deg, window)
            if fine in claimed:
                continue  # already answered at a finer resolution
            key = (int(p.lat // cell_deg), int(p.lon // cell_deg), fine[2])
            _accumulate(cells.setdefault(key, _CellWindow()), p)
            members.setdefault(key, set()).add(fine)

        for key, cw in sorted(cells.items()):
            if len(cw.worst_nic) < s.gnss_min_aircraft:
                continue  # try again, coarser
            claimed |= members.get(key, set())
            yield level, cell_deg, key, cw, True

    # Sky that never met the minimum at any resolution, reported at the finest granularity.
    leftover: dict[tuple[int, int, int], _CellWindow] = {}
    for p in positions:
        fine = _fine_key(p, s.gnss_cell_deg, window)
        if fine not in claimed:
            _accumulate(leftover.setdefault(fine, _CellWindow()), p)
    for key, cw in sorted(leftover.items()):
        yield 0, s.gnss_cell_deg, key, cw, False


def detect_jamming(
    session: Session,
    region: str | None = None,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> tuple[list[Incident], JammingRunStats]:
    """Area-level GNSS-interference incidents.

    `since`/`until` restrict scoring to reports inside that interval. `since` is the
    incremental lane's handle (a cell-window whose reports are entirely in the past cannot
    change); `until` exists for evaluations that must describe a bounded interval — a
    controlled comparison whose statistics cover one window but whose incidents cover the
    whole database is describing two different experiments in one table. Both are filters
    on the *input reports*; callers who care about bucket boundaries align them (see
    `align_to_window`).
    """
    s = get_settings()
    positions = _load_positions(session, region, since, until)

    incidents: list[Incident] = []
    stats = JammingRunStats()
    for level, cell_deg, key, cw, scoreable in _aggregate_levels(positions, s):
        if not scoreable:
            stats.cells_unscoreable += 1
            continue
        ci, cj, wk = key
        _score_cell(incidents, s, cw, ci, cj, wk, cell_deg=cell_deg, level=level, region=region)

    window = timedelta(minutes=s.gnss_window_minutes)
    stats.cells_seen = len({_fine_key(p, s.gnss_cell_deg, window) for p in positions})
    stats.incidents = len(incidents)
    log.info(
        "detect_jamming",
        cells=stats.cells_seen,
        unscoreable=stats.cells_unscoreable,
        incidents=stats.incidents,
    )
    return incidents, stats


@dataclass
class CoverageCell:
    """One patch of sky and what could honestly be said about it.

    The dashboard's three states come from here. `scoreable=False` is not an absence of
    data to be left blank or, worse, drawn as "clear" — it is a positive statement that too
    few aircraft were observed to judge, and it is the majority of the map at Singapore
    traffic densities. A map that colours unscoreable sky is lying about its own coverage.
    """

    lat: float
    lon: float
    cell_deg: float
    level: int
    aircraft_observed: int
    scoreable: bool
    degraded_fraction: float | None  # None exactly when not scoreable
    hard_loss: int
    ts_start: datetime | None
    ts_end: datetime | None


def coverage_grid(
    session: Session, region: str | None = None, *, since: datetime | None = None
) -> list[CoverageCell]:
    """The scoreability map behind the incidents.

    Shares `_aggregate_levels` with the detector rather than recomputing the grid, so what
    the map draws is exactly what the detector decided — a dashboard that disagrees with
    the evaluation is worse than no dashboard.
    """
    s = get_settings()
    positions = _load_positions(session, region, since)
    cells: list[CoverageCell] = []
    for level, cell_deg, key, cw, scoreable in _aggregate_levels(positions, s):
        ci, cj, _wk = key
        total = len(cw.worst_nic)
        degraded = {a: n for a, n in cw.worst_nic.items() if n <= s.gnss_bad_nic_max}
        hard = {
            a
            for a, n in degraded.items()
            if n <= s.gnss_hard_loss_nic
            and cw.worst_nac_p.get(a, s.gnss_hard_loss_nac_p) <= s.gnss_hard_loss_nac_p
        }
        cells.append(
            CoverageCell(
                lat=(ci + 0.5) * cell_deg,
                lon=(cj + 0.5) * cell_deg,
                cell_deg=cell_deg,
                level=level,
                aircraft_observed=total,
                scoreable=scoreable,
                degraded_fraction=(len(degraded) / total) if scoreable else None,
                hard_loss=len(hard),
                ts_start=cw.ts_min,
                ts_end=cw.ts_max,
            )
        )
    return cells


def _score_cell(
    incidents: list[Incident],
    s: Settings,
    cw: _CellWindow,
    ci: int,
    cj: int,
    wk: int,
    *,
    cell_deg: float,
    level: int,
    region: str | None,
) -> None:
    """Emit an incident for one scoreable cell-window, if it crosses the threshold."""
    total = len(cw.worst_nic)
    degraded = {a: n for a, n in cw.worst_nic.items() if n <= s.gnss_bad_nic_max}
    frac = len(degraded) / total
    if frac < s.gnss_bad_fraction_threshold:
        return
    # Hard loss = both integrity channels collapsed on the same aircraft. Healthy traffic
    # never reports NACp 0, so two-channel agreement is the sharp tier; an aircraft that
    # never broadcast NACp is judged on NIC alone rather than assumed degraded.
    hard_loss = {
        a
        for a, n in degraded.items()
        if n <= s.gnss_hard_loss_nic
        and cw.worst_nac_p.get(a, s.gnss_hard_loss_nac_p) <= s.gnss_hard_loss_nac_p
    }
    hard_frac = len(hard_loss) / total
    assert cw.ts_min is not None and cw.ts_max is not None
    incidents.append(
        make_incident(
            # The level is part of the id: the same patch of sky answered at a coarser
            # resolution is a different claim, and must not collide with a finer one.
            incident_id=f"jam:L{level}:{ci}:{cj}:{wk}",
            detector="jamming",
            incident_type="GNSS interference",
            # Confidence grows with how completely the cell degraded, and again when the
            # second channel corroborates the first.
            score=min(1.0, 0.5 + 0.4 * frac + 0.1 * hard_frac),
            reliability=grade_samples(total),
            ts_start=cw.ts_min,
            ts_end=cw.ts_max,
            lat=(ci + 0.5) * cell_deg,
            lon=(cj + 0.5) * cell_deg,
            affected_count=len(degraded),
            techniques=[TECH_JAMMING],
            evidence={
                "aircraft_observed": total,
                "aircraft_degraded": len(degraded),
                "degraded_fraction": round(frac, 3),
                "aircraft_hard_loss": len(hard_loss),
                "hard_loss_fraction": round(hard_frac, 3),
                "hard_loss_aircraft": sorted(hard_loss),
                "worst_nic_by_aircraft": dict(sorted(degraded.items())),
                "bad_nic_max": s.gnss_bad_nic_max,
                # Which resolution actually answered this patch of sky — a coarse cell is
                # a weaker spatial claim than a fine one and should read as such.
                "cell_deg": cell_deg,
                "resolution_level": level,
                "window_minutes": s.gnss_window_minutes,
            },
            region=cw.region or region,
        )
    )
