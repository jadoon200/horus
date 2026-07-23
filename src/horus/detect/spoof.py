"""Kinematic-impossibility / identity-spoof detector.

An implied speed far above anything in civil aviation between consecutive fixes means the
fixes cannot both be genuine — the classic signature of two transmitters sharing one
ICAO address, or injected/garbled data. Repeated violations (not a single glitch) make
an incident.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from horus.config import get_settings
from horus.db.models import Incident, Position
from horus.detect.base import TECH_SPOOF, make_incident
from horus.geo import implied_speed_kn
from horus.logging import get_logger
from horus.timeutil import utc_naive

log = get_logger(__name__)

_MIN_VIOLATIONS = 3


def detect_spoof(session: Session, region: str | None = None) -> list[Incident]:
    s = get_settings()
    q = select(Position).order_by(Position.icao24, Position.ts)
    if region:
        q = q.where(Position.region == region)

    violations: dict[str, list[Position]] = {}
    speeds: dict[str, list[float]] = {}
    prev: Position | None = None
    for p in session.scalars(q):
        if prev is not None and prev.icao24 == p.icao24:
            dt = (utc_naive(p.ts) - utc_naive(prev.ts)).total_seconds()
            v = implied_speed_kn(prev.lat, prev.lon, p.lat, p.lon, dt)
            if v > s.spoof_max_speed_kt:
                violations.setdefault(p.icao24, []).append(p)
                speeds.setdefault(p.icao24, []).append(v)
        prev = p

    incidents: list[Incident] = []
    for icao24, hits in violations.items():
        if len(hits) < _MIN_VIOLATIONS:
            continue  # one glitch is data noise, not a pattern
        vmax = max(speeds[icao24])
        incidents.append(
            make_incident(
                incident_id=f"spoof:{icao24}",
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
