"""Small, read-only primitives for the joint air + sea picture.

HORUS and PHAROS already expose incidents in the same ARGUS ``EvidenceItem`` shape.  The
capstone does not create a new fused datastore or claim a linked event: it places air and
maritime evidence in the same coarse space-time cell so an analyst can decide whether the
coincidence warrants review.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin

from horus.geo import haversine_km

Bounds = tuple[float, float, float, float]  # min_lat, min_lon, max_lat, max_lon
Cell = tuple[int, int]


@dataclass(frozen=True)
class FusionEvidence:
    """The shared, geolocated subset of an ARGUS-compatible evidence item."""

    lane: str
    doc_id: str
    title: str
    source: str
    reliability: str
    credibility: int
    summary: str
    published: datetime
    url: str | None
    detector: str
    lat: float
    lon: float
    zone: str | None


@dataclass(frozen=True)
class AirSeaCorrelation:
    """One review candidate; deliberately not a causal or attributed relationship."""

    air: FusionEvidence
    sea: FusionEvidence
    cell: Cell
    delta_minutes: float
    distance_km: float


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _published(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_evidence(row: dict[str, Any], lane: str, api_url: str) -> FusionEvidence | None:
    """Validate one sibling response row, returning ``None`` when it cannot be correlated."""
    doc_id = str(row.get("doc_id") or "").strip()
    title = str(row.get("title") or "").strip()
    published = _published(row.get("published"))
    lat = _finite_float(row.get("lat"))
    lon = _finite_float(row.get("lon"))
    if not doc_id or not title or published is None or lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    credibility_value = _finite_float(row.get("credibility"))
    credibility = int(credibility_value) if credibility_value is not None else 6
    credibility = max(1, min(6, credibility))
    raw_url = str(row.get("url") or "").strip()
    resolved_url = urljoin(api_url.rstrip("/") + "/", raw_url.lstrip("/")) if raw_url else None
    zone_value = str(row.get("zone") or "").strip()
    return FusionEvidence(
        lane=lane,
        doc_id=doc_id,
        title=title,
        source=str(row.get("source") or lane).strip(),
        reliability=str(row.get("reliability") or "F").strip().upper()[:1] or "F",
        credibility=credibility,
        summary=str(row.get("summary") or title).strip(),
        published=published,
        url=resolved_url,
        detector=str(row.get("detector") or "unknown").strip(),
        lat=lat,
        lon=lon,
        zone=zone_value or None,
    )


def spatial_cell(lat: float, lon: float, cell_deg: float) -> Cell:
    if cell_deg <= 0:
        raise ValueError("cell_deg must be positive")
    return math.floor(lat / cell_deg), math.floor(lon / cell_deg)


def _inside(evidence: FusionEvidence, bounds: Bounds) -> bool:
    min_lat, min_lon, max_lat, max_lon = bounds
    return min_lat <= evidence.lat <= max_lat and min_lon <= evidence.lon <= max_lon


def correlate_air_sea(
    air: list[FusionEvidence],
    sea: list[FusionEvidence],
    *,
    cell_deg: float = 0.5,
    time_window: timedelta = timedelta(hours=3),
    bounds: Bounds = (0.5, 102.5, 2.0, 104.8),
) -> list[AirSeaCorrelation]:
    """Return air/sea pairs in the same coarse cell and within ``time_window``.

    Matching uses event start times because that is the shared timestamp both sibling APIs
    guarantee.  Distance and time separation remain visible so the coarse-cell abstraction
    cannot be mistaken for emitter geolocation or a precise encounter.
    """
    if time_window <= timedelta(0):
        raise ValueError("time_window must be positive")
    min_lat, min_lon, max_lat, max_lon = bounds
    if min_lat > max_lat or min_lon > max_lon:
        raise ValueError("invalid bounds")

    sea_by_cell: dict[Cell, list[FusionEvidence]] = {}
    for item in sea:
        if _inside(item, bounds):
            sea_by_cell.setdefault(spatial_cell(item.lat, item.lon, cell_deg), []).append(item)

    matches: list[AirSeaCorrelation] = []
    for air_item in air:
        if not _inside(air_item, bounds):
            continue
        cell = spatial_cell(air_item.lat, air_item.lon, cell_deg)
        for sea_item in sea_by_cell.get(cell, []):
            delta = abs(air_item.published - sea_item.published)
            if delta > time_window:
                continue
            matches.append(
                AirSeaCorrelation(
                    air=air_item,
                    sea=sea_item,
                    cell=cell,
                    delta_minutes=delta.total_seconds() / 60.0,
                    distance_km=haversine_km(
                        air_item.lat,
                        air_item.lon,
                        sea_item.lat,
                        sea_item.lon,
                    ),
                )
            )
    return sorted(
        matches,
        key=lambda item: (
            -item.air.published.timestamp(),
            item.delta_minutes,
            item.distance_km,
            item.air.doc_id,
            item.sea.doc_id,
        ),
    )


def cell_label(cell: Cell, cell_deg: float) -> str:
    """Human-readable latitude/longitude bounds for a coarse cell."""
    lat, lon = cell[0] * cell_deg, cell[1] * cell_deg
    return f"{lat:.1f}-{lat + cell_deg:.1f}°N, {lon:.1f}-{lon + cell_deg:.1f}°E"
