"""Dark-aircraft detector — transponder silence at altitude with displaced reappearance.

The coverage confound is the whole game here (even more than for AIS): low-altitude
traffic drops out of terrestrial ADS-B reception routinely, so a silence is only
suspicious when the aircraft was last seen AT ALTITUDE (well inside coverage), stayed
silent well past the track-gap threshold, reappeared far away, and the silence doesn't
overlap a recorded *collector* outage (our own downtime must never be an incident).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from horus.config import Settings, get_settings
from horus.db.models import CollectorRun, CoverageOutage, Incident, Position
from horus.detect.base import TECH_DARK, make_incident
from horus.geo import haversine_km
from horus.logging import get_logger
from horus.timeutil import utc_naive

log = get_logger(__name__)


@dataclass(frozen=True)
class CollectionBoundary:
    lat: float
    lon: float
    radius_nm: int
    source: str


def _collection_boundary(
    session: Session,
    position: Position,
    settings: Settings,
) -> CollectionBoundary:
    """Find the run circle that collected a position, or a conservative fallback.

    `seen_pos` correction can put a position just before the run's `started_at`, so the
    match allows the configured freshness window before start. A completed run's
    `last_message_at` bounds the other side. If historical rows lack provenance, return the
    configured circle: `_could_have_left_coverage` only applies it when the position lies
    inside, so a Baltic point never gets suppressed by Singapore geometry.
    """
    ts = utc_naive(position.ts)
    slack = settings.adsb_max_seen_pos_seconds
    candidates = session.scalars(
        select(CollectorRun)
        .where(
            CollectorRun.region == position.region,
            CollectorRun.center_lat.is_not(None),
            CollectorRun.center_lon.is_not(None),
            CollectorRun.radius_nm.is_not(None),
        )
        .order_by(CollectorRun.started_at.desc())
    )
    for run in candidates:
        start = utc_naive(run.started_at)
        end_raw = run.last_message_at or run.stopped_at
        end = utc_naive(end_raw) if end_raw is not None else None
        if ts < start - timedelta(seconds=slack):
            continue
        if end is not None and ts > end:
            continue
        assert run.center_lat is not None
        assert run.center_lon is not None
        assert run.radius_nm is not None
        return CollectionBoundary(
            lat=run.center_lat,
            lon=run.center_lon,
            radius_nm=run.radius_nm,
            source=f"collector-run:{run.id}",
        )
    return CollectionBoundary(
        lat=settings.adsb_center_lat,
        lon=settings.adsb_center_lon,
        radius_nm=settings.adsb_radius_nm,
        source="config-fallback",
    )


def _could_have_left_coverage(
    lat: float,
    lon: float,
    gs_kt: float | None,
    gap_minutes: float,
    boundary: CollectionBoundary,
) -> bool:
    """Could the aircraft have flown out of the collection circle and back in this gap?

    The collector watches a fixed-radius circle. An aircraft that departs, crosses the
    boundary and returns hours later reappears displaced after a long silence — which is
    the dark-aircraft signature exactly, but the explanation is our own collection volume,
    not the aircraft. If there was time to reach the boundary and come back, the silence is
    fully explained by geometry and the call is not attributable.

    Deliberately conservative: it uses the aircraft's own last known ground speed and the
    straight-line distance to the boundary, so it only suppresses calls where exit is
    *comfortably* possible. This is the air-domain analogue of the maritime sibling's
    coverage model — never claim what coverage can explain.
    """
    speed = gs_kt or 0.0
    if speed <= 0:
        return False
    # Distance to the boundary, in nautical miles, from the collection centre.
    dlat_nm = (lat - boundary.lat) * 60.0
    dlon_nm = (lon - boundary.lon) * 60.0 * math.cos(math.radians((lat + boundary.lat) / 2))
    from_centre = math.hypot(dlat_nm, dlon_nm)
    # A recorded boundary is applicable only to positions inside it. Bad historical
    # provenance or the conservative config fallback can put a position outside the circle;
    # fail OPEN (keep the call for review) rather than clamp the distance and silently
    # suppress it as able to leave.
    if from_centre > boundary.radius_nm:
        return False
    to_boundary = boundary.radius_nm - from_centre
    # Out and back at its own cruise speed, in minutes.
    round_trip_minutes = 2.0 * (to_boundary / speed) * 60.0
    return gap_minutes >= round_trip_minutes


def _outage_windows(session: Session) -> list[tuple[datetime, datetime | None]]:
    """Recorded collector downtime, tz-normalized. An open outage has no close time."""
    return [
        (utc_naive(o.opened_at), utc_naive(o.closed_at) if o.closed_at else None)
        for o in session.scalars(select(CoverageOutage))
    ]


def detect_gaps(
    session: Session, region: str | None = None, *, since: datetime | None = None
) -> list[Incident]:
    """Dark-aircraft candidates.

    `since` scopes the scan for the incremental lane. A gap's `ts_start` is the last report
    *before* the silence, so restricting input positions to `ts >= since` yields exactly the
    incidents that start inside the window — a gap opening earlier is correctly excluded
    rather than half-seen, because it is already stored and unchanged.
    """
    s = get_settings()
    q = select(Position).order_by(Position.icao24, Position.ts)
    if region:
        q = q.where(Position.region == region)
    if since is not None:
        q = q.where(Position.ts >= since)
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
            # Descending at the moment of silence = landing, not going dark.
            descending = (prev.baro_rate_fpm or 0.0) < s.gap_max_descent_fpm
            # Long enough to have left the collection circle and returned = our boundary,
            # not the aircraft's behaviour.
            boundary = _collection_boundary(session, prev, s)
            left_coverage = _could_have_left_coverage(
                prev.lat,
                prev.lon,
                prev.gs_kt,
                gap_min,
                boundary,
            )
            if (
                gap_min >= s.gap_min_minutes
                and displacement >= s.gap_min_displacement_km
                and at_altitude
                and not descending
                and not left_coverage
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
                            "vertical_rate_fpm": prev.baro_rate_fpm,
                            "ground_speed_kt": prev.gs_kt,
                            "coverage_boundary_source": boundary.source,
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
