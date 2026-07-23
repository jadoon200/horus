"""Dark-aircraft detector — transponder silence at altitude with displaced reappearance.

The coverage confound is the whole game here (even more than for AIS): low-altitude
traffic drops out of terrestrial ADS-B reception routinely, so a silence is only
suspicious when the aircraft was last seen AT ALTITUDE (well inside coverage), stayed
silent well past the track-gap threshold, reappeared far away, and the silence doesn't
overlap a recorded *collector* outage (our own downtime must never be an incident).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from horus.config import get_settings
from horus.db.models import CoverageOutage, Incident, Position
from horus.detect.base import TECH_DARK, make_incident
from horus.geo import haversine_km
from horus.logging import get_logger
from horus.timeutil import utc_naive

log = get_logger(__name__)


def _outage_windows(session: Session) -> list[tuple[datetime, datetime | None]]:
    """Recorded collector downtime, tz-normalized. An open outage has no close time."""
    return [
        (utc_naive(o.opened_at), utc_naive(o.closed_at) if o.closed_at else None)
        for o in session.scalars(select(CoverageOutage))
    ]


def detect_gaps(session: Session, region: str | None = None) -> list[Incident]:
    s = get_settings()
    q = select(Position).order_by(Position.icao24, Position.ts)
    if region:
        q = q.where(Position.region == region)
    outages = _outage_windows(session)

    incidents: list[Incident] = []
    prev: Position | None = None
    for p in session.scalars(q):
        if prev is not None and prev.icao24 == p.icao24:
            t0, t1 = utc_naive(prev.ts), utc_naive(p.ts)
            gap_min = (t1 - t0).total_seconds() / 60.0
            # Half-open overlap: the silence intersects a recorded outage.
            in_outage = any(o0 <= t1 and (o1 is None or t0 <= o1) for o0, o1 in outages)
            displacement = haversine_km(prev.lat, prev.lon, p.lat, p.lon)
            at_altitude = (prev.alt_baro_ft or 0.0) >= s.gap_min_altitude_ft
            if (
                gap_min >= s.gap_min_minutes
                and displacement >= s.gap_min_displacement_km
                and at_altitude
                and not in_outage
            ):
                # Longer + farther = more confident it's not a reception blip.
                score = min(1.0, 0.4 + 0.3 * (gap_min / 30.0) + 0.3 * (displacement / 300.0))
                incidents.append(
                    make_incident(
                        incident_id=f"gap:{p.icao24}:{t0.isoformat()}",
                        detector="gap",
                        incident_type="dark aircraft (transponder gap)",
                        score=score,
                        # A single aircraft's own silence: corroboration is inherently 1.
                        reliability="D" if gap_min < 30 else "C",
                        ts_start=prev.ts,
                        ts_end=p.ts,
                        lat=(prev.lat + p.lat) / 2.0,
                        lon=(prev.lon + p.lon) / 2.0,
                        icao24=p.icao24,
                        techniques=[TECH_DARK],
                        evidence={
                            "gap_minutes": round(gap_min, 1),
                            "displacement_km": round(displacement, 1),
                            "last_alt_baro_ft": prev.alt_baro_ft,
                            "altitude_floor_ft": s.gap_min_altitude_ft,
                            "caveat": "a gap may be benign coverage loss; graded, not judged",
                        },
                        # The data's own region, not the query filter: an unscoped run
                        # must still stamp incidents with where the data came from.
                        region=prev.region or region,
                    )
                )
        prev = p
    log.info("detect_gaps", incidents=len(incidents))
    return incidents
