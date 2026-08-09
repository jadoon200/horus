"""HORUS read-only air-domain-awareness API.

Every route is read-only over the air knowledge base; there is no mutation surface.
GeoJSON endpoints (/zones, /tracks, /aircraft/{icao24}/track) feed the map dashboard
directly; /air-picture serves the composite rollup; /geoint/evidence serves incidents in
ARGUS's EvidenceItem shape so the sibling all-source analyst can cite the air lane.
Incidents are decision support for human review, never automated verdicts.
"""

from __future__ import annotations

import math
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from horus.api.limits import RateLimiter
from horus.config import get_settings
from horus.db.base import get_session_factory, init_sqlite_schema
from horus.db.models import Aircraft, Incident, Position, Track
from horus.detect.ensemble import air_picture
from horus.detect.jamming import coverage_grid
from horus.detect.seq_anomaly import artifact_sha256, load_model
from horus.geoint import to_evidence
from horus.timeutil import utc_naive
from horus.tracks.build import sequence_from_points
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
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_rate_limiter = RateLimiter(
    settings.api_rate_limit_requests,
    settings.api_rate_limit_window_seconds,
    settings.api_trust_forwarded_header,
)


@app.middleware("http")
async def _rate_limit(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    if not _rate_limiter.allow(request):
        return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
    return await call_next(request)


def _scrub_non_finite(value: Any) -> Any:
    """Replace inf/NaN floats with their string form so an error body can be JSON-encoded."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _scrub_non_finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_non_finite(v) for v in value]
    return value


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return a clean 422 even when the rejected input carried a non-finite float.

    A pasted /score-track with an inf/NaN coordinate is correctly rejected by the model's
    `allow_inf_nan=False`, but FastAPI's default handler echoes the offending value in the
    error body — and inf/NaN are not JSON, so serializing the 422 itself raised and surfaced
    as a 500. Scrubbing the echoed values keeps the rejection a clean 422.
    """
    return JSONResponse(status_code=422, content={"detail": _scrub_non_finite(exc.errors())})


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

    detectors = ("jamming", "gap", "incursion", "spoof", "squawk", "anomaly")
    by_detector = {
        d: db.scalar(select(func.count()).select_from(Incident).where(Incident.detector == d)) or 0
        for d in detectors
    }
    # Collection freshness: a dashboard that looks live while the collector is dead is
    # worse than one that admits it. The age of the newest report is served so the UI can
    # show staleness instead of quietly rendering old sky as current.
    newest = db.scalar(select(func.max(Position.ts)))
    age_seconds = (
        (datetime.now(UTC) - utc_naive(newest).replace(tzinfo=UTC)).total_seconds()
        if newest
        else None
    )
    return {
        "aircraft": _count(Aircraft),
        "positions": _count(Position),
        "tracks": _count(Track),
        "incidents": _count(Incident),
        "incidents_by_detector": by_detector,
        "newest_report": utc_naive(newest).isoformat() if newest else None,
        "data_age_seconds": age_seconds,
        # The UI shows a snapshot banner in demo mode instead of a staleness warning: a baked
        # seed is honestly a snapshot, not a dead live feed.
        "demo_mode": get_settings().demo_mode,
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
def aircraft_track(
    icao24: str, db: Session = Depends(get_db), region: str | None = None
) -> dict[str, Any]:
    """One aircraft's fixes and its aligned integrity series, scoped to a single region.

    Scoped for the same reason /tracks is: one ICAO24 can be persisted under more than one
    region in the same database (a live slice and a backfilled or OpenSky-research one), and
    merging them zig-zags the geometry and interleaves two sources' NIC/NACp into one series
    — an integrity history that never happened. /tracks guarded against this and its sibling
    here did not.

    Without an explicit `region` the aircraft's most recent one is used, and the region that
    answered is reported back rather than left for the caller to assume.
    """
    scope = region
    if scope is None:
        scope = db.scalar(
            select(Position.region)
            .where(Position.icao24 == icao24)
            .order_by(Position.ts.desc())
            .limit(1)
        )
    q = select(Position).where(Position.icao24 == icao24)
    # A NULL region is its own scope — historical rows predating region stamping must not be
    # silently folded into a named lane.
    q = q.where(Position.region.is_(None) if scope is None else Position.region == scope)
    positions = db.scalars(q.order_by(Position.ts)).all()
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
            "region": scope,
            # Integrity series stay index-aligned with timestamps. Missing reports remain
            # null rather than becoming zero (which would fabricate a GNSS collapse).
            "ts_series": [utc_naive(p.ts).isoformat() for p in positions],
            "nic_series": [p.nic for p in positions],
            "nac_p_series": [p.nac_p for p in positions],
        },
    }


@app.get("/tracks")
def tracks_geojson(
    db: Session = Depends(get_db), limit: int = Query(300, le=2000)
) -> dict[str, Any]:
    features = []
    for t in db.scalars(select(Track).order_by(Track.start_ts).limit(limit)):
        pos_q = select(Position).where(
            Position.icao24 == t.icao24,
            Position.ts >= t.start_ts,
            Position.ts <= t.end_ts,
        )
        # One ICAO24 can be persisted under more than one region (a live slice and a
        # backfilled or OpenSky-research one) in the same database. Without this filter the
        # track's rendered geometry merges both regions' fixes and zig-zags across the map;
        # the track was gap-split within its own region, so its points are only that region's.
        if t.region is not None:
            pos_q = pos_q.where(Position.region == t.region)
        positions = db.scalars(pos_q.order_by(Position.ts)).all()
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


@app.get("/gnss-coverage")
def gnss_coverage(
    db: Session = Depends(get_db),
    region: str | None = None,
    hours: float = Query(6.0, gt=0, le=720),
) -> dict[str, Any]:
    """The scoreability map: where the sky could be judged, and where it could not.

    Three states, not two. `scoreable=false` is a positive statement that too few aircraft
    were observed to judge that patch — at Singapore traffic densities it is most of the
    map — and the dashboard renders it as its own state rather than as "clear". A map that
    colours unscoreable sky is lying about its own coverage.
    """
    # A baked demo has no rolling window — its data sits at a fixed past time, so a
    # "last N hours" filter would return an empty map. In demo mode the whole snapshot is
    # shown and the UI labels it a snapshot.
    demo = get_settings().demo_mode
    since = None if demo else datetime.now(UTC) - timedelta(hours=hours)
    cells = coverage_grid(db, region, since=since)

    # The grid is cell-WINDOWS (space x time), so over a six-hour request the same patch of
    # sky appears once per ten-minute window. A map has no time axis, so those must be
    # collapsed to one entry per location before drawing — stacking them just accumulates
    # opacity until the whole map reads as solid colour, which is what happened the first
    # time this was rendered.
    #
    # Collapsing rule: a location is scoreable if ANY window could judge it, and its
    # reported fraction is the WORST one observed. That is the honest summary for a watch
    # display — "at some point in this window, this fraction of aircraft here were
    # degraded" — and it never lets a quiet later window erase an earlier event.
    merged: dict[tuple[float, float, float], dict[str, Any]] = {}
    for c in cells:
        key = (c.lat, c.lon, c.cell_deg)
        prev = merged.get(key)
        if prev is None:
            merged[key] = {
                "lat": c.lat,
                "lon": c.lon,
                "cell_deg": c.cell_deg,
                "level": c.level,
                "aircraft_observed": c.aircraft_observed,
                "scoreable": c.scoreable,
                "degraded_fraction": c.degraded_fraction,
                "hard_loss": c.hard_loss,
                "windows": 1,
            }
            continue
        prev["windows"] = int(prev["windows"]) + 1
        prev["aircraft_observed"] = max(int(prev["aircraft_observed"]), c.aircraft_observed)
        prev["hard_loss"] = max(int(prev["hard_loss"]), c.hard_loss)
        if c.scoreable:
            best = prev["degraded_fraction"]
            prev["scoreable"] = True
            prev["degraded_fraction"] = (
                c.degraded_fraction
                if best is None
                else max(float(best), c.degraded_fraction or 0.0)
            )

    out = list(merged.values())
    return {
        # The window that was actually applied. In demo mode none was, and reporting the
        # requested `hours` anyway had the dashboard captioning a baked snapshot "worst over
        # 6 h" — a six-hour aggregation the deployed demo never performed.
        "window_hours": None if demo else hours,
        "cell_windows": len(cells),
        "cells_total": len(out),
        "cells_scoreable": sum(1 for c in out if c["scoreable"]),
        "cells_unscoreable": sum(1 for c in out if not c["scoreable"]),
        "min_aircraft": get_settings().gnss_min_aircraft,
        "cells": out,
    }


@app.get("/geoint/evidence")
def geoint_evidence(
    db: Session = Depends(get_db),
    min_score: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(100, le=500),
) -> list[dict[str, Any]]:
    """Incidents as citable, source-rated evidence (ARGUS-compatible EvidenceItem shape)."""
    q = select(Incident).where(Incident.score >= min_score).order_by(Incident.score.desc())
    return [to_evidence(i) for i in db.scalars(q.limit(limit))]


class TrackPoint(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    alt_ft: float | None = None
    ts: float  # epoch seconds


class ScoreTrackRequest(BaseModel):
    points: list[TrackPoint]


# The frozen GRU runs torch at request time; cap concurrency so a burst can't exhaust the
# free container's memory. Excess is shed as 503 rather than the process being OOM-killed.
_inference_slots = threading.BoundedSemaphore(settings.api_max_concurrent_inference)


@app.get("/model")
def model_info() -> dict[str, Any]:
    """What flagship model is served, and whether it is the benchmarked freeze.

    `model_source` is trained-artifact when a frozen GRU is present, runtime-fallback when
    none is — so a deployment scoring with no model is visible, never silent.
    """
    sha = artifact_sha256()
    pinned = get_settings().anomaly_artifact_sha256
    return {
        "flagship": "GRU sequence autoencoder",
        "model_source": "trained-artifact" if sha else "runtime-fallback",
        "artifact_sha256": sha,
        "sha_pinned": bool(pinned),
        "sha_matches_pin": (sha == pinned) if (sha and pinned) else None,
    }


@app.post("/score-track")
def score_track(req: ScoreTrackRequest) -> dict[str, Any]:
    """Stateless trajectory-anomaly scoring of a pasted track.

    Inspects ONLY the supplied points — never fetches a URL or reads the database — so the
    API stays effectively read-only. Loads the frozen artifact and reports the reconstruction
    error against the frozen population threshold, plus `model_source` so a runtime fallback
    is explicit. An unusual shape is human-review decision support, never a verdict.
    """
    s = get_settings()
    n = len(req.points)
    if n < 2:
        raise HTTPException(422, "need at least two points to form a track")
    if n > s.score_track_max_points:
        raise HTTPException(422, f"too many points (max {s.score_track_max_points})")

    try:
        raw = [(datetime.fromtimestamp(p.ts, tz=UTC), p.lat, p.lon, p.alt_ft) for p in req.points]
        seq = sequence_from_points(raw, s)
    except (OSError, OverflowError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc

    sha = artifact_sha256()
    if sha is None:
        # No artifact baked/trained — say so rather than fabricating a score.
        return {"model_source": "runtime-fallback", "scored": False, "detail": "no frozen model"}
    if s.anomaly_artifact_sha256 and sha != s.anomaly_artifact_sha256:
        return {
            "model_source": "trained-artifact",
            "scored": False,
            "artifact_sha256": sha,
            "detail": "frozen artifact does not match the configured SHA-256 pin",
        }

    # Loading torch state is part of inference and can briefly allocate as much memory as
    # scoring. Acquire before loading so the concurrency guard covers the whole expensive
    # path rather than allowing a burst of parallel deserializations.
    if not _inference_slots.acquire(blocking=False):
        raise HTTPException(503, "inference busy; retry shortly")
    try:
        model = load_model()
        if model is None:  # Artifact disappeared between the SHA check and the load.
            return {
                "model_source": "runtime-fallback",
                "scored": False,
                "detail": "frozen model became unavailable",
            }
        error = float(model.errors([seq])[0])
    finally:
        _inference_slots.release()

    return {
        "model_source": "trained-artifact",
        "scored": True,
        "reconstruction_error": error,
        "population_threshold": model.threshold,
        "anomalous": error > model.threshold,
        "artifact_sha256": artifact_sha256(),
        "caveat": "unusual trajectory shape — human-review decision support, not a verdict",
    }


# Serve the built SPA when present (single-container deploy) — mounted last so API
# routes always win.
_dist = Path(__file__).resolve().parents[3] / "frontend" / "dist"
if _dist.exists():  # pragma: no cover - deploy path
    app.mount("/", StaticFiles(directory=_dist, html=True), name="spa")
