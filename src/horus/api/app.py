"""HORUS read-only air-domain-awareness API.

Every route is read-only over the air knowledge base; there is no mutation surface.
GeoJSON endpoints (/zones, /tracks, /aircraft/{icao24}/track) feed the map dashboard
directly; /air-picture serves the composite rollup; /geoint/evidence serves incidents in
ARGUS's EvidenceItem shape so the sibling all-source analyst can cite the air lane.
Incidents are decision support for human review, never automated verdicts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from horus.api.limits import RateLimiter
from horus.config import get_settings
from horus.db.base import get_session_factory, init_sqlite_schema
from horus.db.models import Aircraft, Incident, Position, Track
from horus.detect.ensemble import air_picture
from horus.geoint import to_evidence
from horus.zones import all_zones

__version__ = "0.1.0"


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Free single-container deploys run on a SQLite file with no migration step; create
    # the schema on boot (no-op on Postgres, where Alembic owns it).
    init_sqlite_schema()
    yield


settings = get_settings()
app = FastAPI(
    title="HORUS",
    version=__version__,
    description="Air-domain awareness & GNSS-interference monitoring",
    lifespan=_lifespan,
)

_allowed = [o.strip() for o in settings.api_allowed_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed or [],
    allow_origin_regex=None if _allowed else r"http://localhost:\d+",
    allow_methods=["GET"],
    allow_headers=["*"],
)

_rate_limiter = RateLimiter(
    settings.api_rate_limit_requests, settings.api_rate_limit_window_seconds, False
)


@app.middleware("http")
async def _rate_limit(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    if not _rate_limiter.allow(request):
        return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
    return await call_next(request)


def get_db() -> Iterator[Session]:
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()


class Health(BaseModel):
    status: str = "ok"
    version: str


@app.get("/health", response_model=Health)
def health() -> Health:
    return Health(version=__version__)


@app.get("/stats")
def stats(db: Session = Depends(get_db)) -> dict[str, Any]:
    def _count(model: Any) -> int:
        return db.scalar(select(func.count()).select_from(model)) or 0

    detectors = ("jamming", "gap", "incursion", "spoof", "anomaly")
    by_detector = {
        d: db.scalar(select(func.count()).select_from(Incident).where(Incident.detector == d)) or 0
        for d in detectors
    }
    return {
        "aircraft": _count(Aircraft),
        "positions": _count(Position),
        "tracks": _count(Track),
        "incidents": _count(Incident),
        "incidents_by_detector": by_detector,
    }


@app.get("/aircraft")
def list_aircraft(
    db: Session = Depends(get_db), limit: int = Query(200, le=1000)
) -> list[dict[str, Any]]:
    rows = db.scalars(select(Aircraft).limit(limit)).all()
    return [
        {
            "icao24": a.icao24,
            "callsign": a.callsign,
            "registration": a.registration,
            "type_code": a.type_code,
            "category": a.category,
            "first_seen": a.first_seen.isoformat() if a.first_seen else None,
            "last_seen": a.last_seen.isoformat() if a.last_seen else None,
        }
        for a in rows
    ]


def _track_geojson(track: Track, coords: list[list[float]]) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},  # [lon, lat]
        "properties": {
            "track_id": track.track_id,
            "icao24": track.icao24,
            "start_ts": track.start_ts.isoformat(),
            "end_ts": track.end_ts.isoformat(),
            "point_count": track.point_count,
            "distance_km": track.distance_km,
        },
    }


@app.get("/aircraft/{icao24}/track")
def aircraft_track(icao24: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    positions = db.scalars(
        select(Position).where(Position.icao24 == icao24).order_by(Position.ts)
    ).all()
    if not positions:
        raise HTTPException(404, "no positions for this aircraft")
    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": [[p.lon, p.lat, p.alt_baro_ft or 0.0] for p in positions],
        },
        "properties": {
            "icao24": icao24,
            "n": len(positions),
            "nic_series": [p.nic for p in positions],
        },
    }


@app.get("/tracks")
def tracks_geojson(
    db: Session = Depends(get_db), limit: int = Query(300, le=2000)
) -> dict[str, Any]:
    features = []
    for t in db.scalars(select(Track).order_by(Track.start_ts).limit(limit)):
        positions = db.scalars(
            select(Position)
            .where(
                Position.icao24 == t.icao24,
                Position.ts >= t.start_ts,
                Position.ts <= t.end_ts,
            )
            .order_by(Position.ts)
        ).all()
        features.append(_track_geojson(t, [[p.lon, p.lat] for p in positions]))
    return {"type": "FeatureCollection", "features": features}


@app.get("/zones")
def zones_geojson() -> dict[str, Any]:
    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                # GeoJSON rings are [lon, lat] and closed.
                "coordinates": [
                    [[p[1], p[0]] for p in [*z.polygon, z.polygon[0]]],
                ],
            },
            "properties": {
                "zone_id": z.zone_id,
                "name": z.name,
                "kind": z.kind,
                "sensitive": z.sensitive,
                "notes": z.notes,
            },
        }
        for z in all_zones()
    ]
    return {"type": "FeatureCollection", "features": features}


@app.get("/incidents")
def list_incidents(
    db: Session = Depends(get_db),
    detector: str | None = None,
    min_score: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(200, le=1000),
) -> list[dict[str, Any]]:
    q = select(Incident).where(Incident.score >= min_score).order_by(Incident.score.desc())
    if detector:
        q = q.where(Incident.detector == detector)
    return [_incident_dict(i) for i in db.scalars(q.limit(limit))]


def _incident_dict(i: Incident) -> dict[str, Any]:
    return {
        "incident_id": i.incident_id,
        "icao24": i.icao24,
        "track_id": i.track_id,
        "detector": i.detector,
        "incident_type": i.incident_type,
        "score": i.score,
        "severity": i.severity,
        "reliability": i.reliability,
        "ts_start": i.ts_start.isoformat(),
        "ts_end": i.ts_end.isoformat() if i.ts_end else None,
        "lat": i.lat,
        "lon": i.lon,
        "zone": i.zone_id,
        "affected_count": i.affected_count,
        "techniques": i.techniques or [],
        "evidence": i.evidence or {},
    }


@app.get("/incidents/{incident_id}")
def incident_detail(incident_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    inc = db.get(Incident, incident_id)
    if inc is None:
        raise HTTPException(404, "no such incident")
    return _incident_dict(inc)


@app.get("/air-picture")
def get_air_picture(db: Session = Depends(get_db), region: str | None = None) -> dict[str, Any]:
    """The composite: per-aircraft rollups (riskiest first) + area-level GNSS incidents."""
    return air_picture(db, region)


@app.get("/geoint/evidence")
def geoint_evidence(
    db: Session = Depends(get_db),
    min_score: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(100, le=500),
) -> list[dict[str, Any]]:
    """Incidents as citable, source-rated evidence (ARGUS-compatible EvidenceItem shape)."""
    q = select(Incident).where(Incident.score >= min_score).order_by(Incident.score.desc())
    return [to_evidence(i) for i in db.scalars(q.limit(limit))]


# Serve the built SPA when present (single-container deploy) — mounted last so API
# routes always win.
_dist = Path(__file__).resolve().parents[3] / "frontend" / "dist"
if _dist.exists():  # pragma: no cover - deploy path
    app.mount("/", StaticFiles(directory=_dist, html=True), name="spa")
