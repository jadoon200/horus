"""Kinematic-impossibility / identity-spoof detector.

An implied speed far above anything in civil aviation between consecutive fixes means the
fixes cannot both be genuine — the classic signature of two transmitters sharing one
ICAO address, or injected/garbled data. Repeated violations (not a single glitch) make
an incident.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from horus.config import get_settings
from horus.db.models import Incident, Position
from horus.detect.base import TECH_SPOOF, latest_positions_before, make_incident
from horus.geo import implied_speed_kn
from horus.logging import get_logger
from horus.timeutil import utc_naive

log = get_logger(__name__)

_MIN_VIOLATIONS = 3


def _violating_pairs(
    positions: Iterable[Position],
    ceiling_kt: float,
    seeds: dict[str, Position] | None = None,
) -> tuple[dict[str, list[Position]], dict[str, list[float]]]:
    """Scan ordered (icao24, ts) positions for impossible consecutive-fix speeds.

    `seeds` supplies each aircraft's last report from before the scanned range, so a pair
    straddling the range boundary is still formed.
    """
    violations: dict[str, list[Position]] = {}
    speeds: dict[str, list[float]] = {}
    prev: Position | None = None
    for p in positions:
        if seeds is not None and (prev is None or prev.icao24 != p.icao24):
            prev = seeds.get(p.icao24)
        if prev is not None and prev.icao24 == p.icao24:
            dt = (utc_naive(p.ts) - utc_naive(prev.ts)).total_seconds()
            v = implied_speed_kn(prev.lat, prev.lon, p.lat, p.lon, dt)
            if v > ceiling_kt:
                violations.setdefault(p.icao24, []).append(p)
                speeds.setdefault(p.icao24, []).append(v)
        prev = p
    return violations, speeds


def detect_spoof(
    session: Session, region: str | None = None, *, since: datetime | None = None
) -> list[Incident]:
    """Kinematic-impossibility calls.

    `since` scopes the scan for the incremental lane: aircraft with a violating pair
    closing at or after the boundary (found with a seeded window scan) are re-derived from
    their FULL history, so the incident keeps its true first violation — and therefore its
    id. Deriving from the window alone shifts the first violation every time the rolling
    boundary crosses a hit, minting a fresh id beside the stored one — one duplicate per
    crossing, reproduced before this path existed. Aircraft whose violations all settled
    before the boundary are untouched.
    """
    s = get_settings()
    q = select(Position).order_by(Position.icao24, Position.ts)
    if region:
        q = q.where(Position.region == region)

    if since is not None:
        seeds = latest_positions_before(session, since, region)
        recent, _ = _violating_pairs(
            session.scalars(q.where(Position.ts >= since)), s.spoof_max_speed_kt, seeds
        )
        if not recent:
            log.info("detect_spoof", incidents=0)
            return []
        q = q.where(Position.icao24.in_(set(recent)))

    violations, speeds = _violating_pairs(session.scalars(q), s.spoof_max_speed_kt)

    incidents: list[Incident] = []
    for icao24, hits in violations.items():
        if len(hits) < _MIN_VIOLATIONS:
            continue  # one glitch is data noise, not a pattern
        vmax = max(speeds[icao24])
        incidents.append(
            make_incident(
                # Time-scoped for the same reason as incursion: an id without a timestamp
                # collides across incremental refresh windows.
                incident_id=f"spoof:{icao24}:{utc_naive(hits[0].ts).isoformat()}",
                detector="spoof",
                incident_type="kinematic impossibility / identity anomaly",
                score=min(1.0, 0.6 + 0.1 * len(hits)),
                reliability="C",  # many independent impossible steps corroborate each other
                ts_start=hits[0].ts,
                ts_end=hits[-1].ts,
                lat=hits[0].lat,
                lon=hits[0].lon,
                icao24=icao24,
                techniques=[TECH_SPOOF],
                evidence={
                    "violations": len(hits),
                    "max_implied_speed_kt": round(vmax, 0),
                    "ceiling_kt": s.spoof_max_speed_kt,
                    "caveat": "may be two units sharing an address or feed corruption",
                },
                region=hits[0].region or region,
            )
        )
    log.info("detect_spoof", incidents=len(incidents))
    return incidents
