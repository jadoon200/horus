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


def detect_jamming(
    session: Session, region: str | None = None, *, since: datetime | None = None
) -> tuple[list[Incident], JammingRunStats]:
    """Area-level GNSS-interference incidents.

    `since` restricts scoring to reports at or after that time — the incremental lane's
    handle. A cell-window whose reports are entirely in the past cannot change, so
    re-scoring it would only reproduce what is already stored. Note this is a filter on the
    *input reports*, so a window straddling the boundary is scored on its visible part
    only; callers pass a `since` comfortably older than any window they care about.
    """
    s = get_settings()
    q = select(Position).where(Position.nic.is_not(None), Position.on_ground.is_(False))
    if region:
        q = q.where(Position.region == region)
    if since is not None:
        q = q.where(Position.ts >= since)

    window = timedelta(minutes=s.gnss_window_minutes)
    positions = list(session.scalars(q))

    # Multi-resolution scoring. A fixed cell size forces one choice for the whole map, and
    # over real traffic that choice is nearly always wrong somewhere: at 0.5° over Singapore
    # 83% of cells held too few aircraft to score. Instead, aggregate at each resolution in
    # turn (finest first) and accept a cell only where it meets the aircraft minimum; any
    # sky still unscoreable falls through to the next, coarser level. Dense airways keep
    # tight localization, sparse sky still gets an answer, and the resolution actually used
    # is recorded on the incident rather than implied.
    levels = [s.gnss_cell_deg * (2**k) for k in range(s.gnss_coarsen_levels + 1)]
    incidents: list[Incident] = []
    stats = JammingRunStats()
    claimed: set[tuple[int, int, int]] = set()  # finest-level keys already answered

    for level, cell_deg in enumerate(levels):
        cells: dict[tuple[int, int, int], _CellWindow] = {}
        fine_members: dict[tuple[int, int, int], set[tuple[int, int, int]]] = {}
        for p in positions:
            ts = utc_naive(p.ts)
            bucket = int((ts - _EPOCH) // window)
            fine_key = (
                int(p.lat // s.gnss_cell_deg),
                int(p.lon // s.gnss_cell_deg),
                bucket,
            )
            if fine_key in claimed:
                continue  # already answered at a finer resolution
            key = (int(p.lat // cell_deg), int(p.lon // cell_deg), bucket)
            cw = cells.setdefault(key, _CellWindow())
            fine_members.setdefault(key, set()).add(fine_key)
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

        if level == 0:
            stats.cells_seen = len(cells)

        for (ci, cj, wk), cw in sorted(cells.items()):
            total = len(cw.worst_nic)
            if total < s.gnss_min_aircraft:
                continue  # try again, coarser
            # This patch of sky is answered; don't re-aggregate it at a coarser level.
            claimed |= fine_members.get((ci, cj, wk), set())
            _score_cell(incidents, s, cw, ci, cj, wk, cell_deg=cell_deg, level=level, region=region)

    # Whatever never met the minimum at any resolution stays honestly unscored.
    fine_keys = {
        (
            int(p.lat // s.gnss_cell_deg),
            int(p.lon // s.gnss_cell_deg),
            int((utc_naive(p.ts) - _EPOCH) // window),
        )
        for p in positions
    }
    stats.cells_unscoreable = len(fine_keys - claimed)
    stats.incidents = len(incidents)
    log.info(
        "detect_jamming",
        cells=stats.cells_seen,
        unscoreable=stats.cells_unscoreable,
        incidents=stats.incidents,
    )
    return incidents, stats


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
