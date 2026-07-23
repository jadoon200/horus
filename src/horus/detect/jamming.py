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

from horus.config import get_settings
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
    session: Session, region: str | None = None
) -> tuple[list[Incident], JammingRunStats]:
    s = get_settings()
    q = select(Position).where(Position.nic.is_not(None), Position.on_ground.is_(False))
    if region:
        q = q.where(Position.region == region)

    window = timedelta(minutes=s.gnss_window_minutes)
    cell_deg = s.gnss_cell_deg
    cells: dict[tuple[int, int, int], _CellWindow] = {}

    # One pass. (This used to scan twice — once to find the corpus minimum timestamp — which
    # both doubled the I/O and introduced the corpus-dependent bucketing described above.)
    for p in session.scalars(q):
        ts = utc_naive(p.ts)
        key = (
            int(p.lat // cell_deg),
            int(p.lon // cell_deg),
            int((ts - _EPOCH) // window),
        )
        cw = cells.setdefault(key, _CellWindow())
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

    stats = JammingRunStats(cells_seen=len(cells))
    incidents: list[Incident] = []
    for (ci, cj, wk), cw in sorted(cells.items()):
        total = len(cw.worst_nic)
        if total < s.gnss_min_aircraft:
            stats.cells_unscoreable += 1
            continue
        degraded = {a: n for a, n in cw.worst_nic.items() if n <= s.gnss_bad_nic_max}
        frac = len(degraded) / total
        if frac < s.gnss_bad_fraction_threshold:
            continue
        # Hard loss = both integrity channels collapsed on the same aircraft. Healthy
        # traffic never reports NACp 0, so two-channel agreement is the sharp tier; an
        # aircraft that never broadcast NACp is judged on NIC alone rather than assumed.
        hard_loss = {
            a
            for a, n in degraded.items()
            if n <= s.gnss_hard_loss_nic
            and cw.worst_nac_p.get(a, s.gnss_hard_loss_nac_p) <= s.gnss_hard_loss_nac_p
        }
        hard_frac = len(hard_loss) / total
        lat = (ci + 0.5) * cell_deg
        lon = (cj + 0.5) * cell_deg
        assert cw.ts_min is not None and cw.ts_max is not None
        incidents.append(
            make_incident(
                incident_id=f"jam:{ci}:{cj}:{wk}",
                detector="jamming",
                incident_type="GNSS interference",
                # Confidence grows with how completely the cell degraded, and again when
                # the second channel corroborates the first.
                score=min(1.0, 0.5 + 0.4 * frac + 0.1 * hard_frac),
                reliability=grade_samples(total),
                ts_start=cw.ts_min,
                ts_end=cw.ts_max,
                lat=lat,
                lon=lon,
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
                    "cell_deg": cell_deg,
                    "window_minutes": s.gnss_window_minutes,
                },
                region=cw.region or region,
            )
        )
    stats.incidents = len(incidents)
    log.info(
        "detect_jamming",
        cells=stats.cells_seen,
        unscoreable=stats.cells_unscoreable,
        incidents=stats.incidents,
    )
    return incidents, stats
