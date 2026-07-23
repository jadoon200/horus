"""Low-level watch-box incursion detector.

High-altitude overflight of a watch area is normal airline traffic; what matters is an
aircraft *down low* inside a border/watch box (kind "border" — e.g. the Riau box), the
low-level cross-border profile. One incident per aircraft x zone with the observed dwell
window. Zones are coarse illustrative rings, so the incident is framing, not a violation
claim.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from horus.config import get_settings
from horus.db.models import Incident, Position
from horus.detect.base import TECH_INCURSION, make_incident
from horus.logging import get_logger
from horus.zones import zones_containing

log = get_logger(__name__)

_INCURSION_KINDS = frozenset({"border"})


def detect_incursions(session: Session, region: str | None = None) -> list[Incident]:
    s = get_settings()
    q = select(Position).where(Position.on_ground.is_(False)).order_by(Position.ts)
    if region:
        q = q.where(Position.region == region)

    # (icao24, zone_id) -> ordered in-zone low-level samples
    hits: dict[tuple[str, str], list[Position]] = {}
    for p in session.scalars(q):
        if (p.alt_baro_ft or 0.0) >= s.gap_min_altitude_ft:
            continue  # at altitude: ordinary overflight
        for zone in zones_containing(p.lat, p.lon):
            if zone.kind in _INCURSION_KINDS:
                hits.setdefault((p.icao24, zone.zone_id), []).append(p)

    incidents: list[Incident] = []
    for (icao24, zone_id), samples in hits.items():
        if len(samples) < 3:
            continue  # a clipped corner isn't a dwell
        dwell_min = (samples[-1].ts - samples[0].ts).total_seconds() / 60.0
        incidents.append(
            make_incident(
                incident_id=f"incursion:{icao24}:{zone_id}",
                detector="incursion",
                incident_type="low-level watch-box incursion",
                score=min(1.0, 0.5 + 0.02 * dwell_min),
                reliability="C",  # well-sampled low-level positions inside the ring
                ts_start=samples[0].ts,
                ts_end=samples[-1].ts,
                lat=samples[len(samples) // 2].lat,
                lon=samples[len(samples) // 2].lon,
                icao24=icao24,
                techniques=[TECH_INCURSION],
                evidence={
                    "zone_id": zone_id,
                    "samples_in_zone": len(samples),
                    "dwell_minutes": round(dwell_min, 1),
                    "max_alt_ft": max(float(p.alt_baro_ft or 0.0) for p in samples),
                    "caveat": "watch rings are illustrative, not authoritative airspace",
                },
                region=samples[0].region or region,
            )
        )
    log.info("detect_incursions", incidents=len(incidents))
    return incidents
