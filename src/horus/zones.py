"""Curated airspace reference-zone registry.

The air analogue of PHAROS's maritime zone registry: a small, transparent set of areas —
terminal areas, overwater corridors, border/gray-zone watch boxes — each a coarse
(lat, lon) ring. Detectors use `zone_for()` to contextualise a position (a transponder gap
inside a watch box matters more than one over open water), and the API serves the rings as
GeoJSON for the map.

**These rings are deliberately coarse, illustrative rectangles** — enough to attribute an
incident to a named area, NOT authoritative airspace boundaries (no AIP/NOTAM geometry is
reproduced here). `sensitive=True` marks watch areas that weight the risk score. The
Singapore FIR neighbourhood is the operational focus.
"""

from __future__ import annotations

from dataclasses import dataclass

from horus.geo import point_in_polygon


@dataclass(frozen=True)
class ZoneInfo:
    zone_id: str
    name: str
    kind: str  # terminal | corridor | watch | border
    country: str | None
    polygon: list[list[float]]  # [[lat, lon], ...]
    sensitive: bool = False
    notes: str | None = None


# Coarse rings, [lat, lon] corners. Singapore FIR neighbourhood — the operational focus.
REGISTRY: list[ZoneInfo] = [
    ZoneInfo(
        "changi-terminal",
        "Changi Terminal Area (coarse)",
        "terminal",
        "SG",
        [[1.25, 103.90], [1.25, 104.15], [1.48, 104.15], [1.48, 103.90]],
        notes="Approximate arrival/departure area around Changi — dense, expected traffic.",
    ),
    ZoneInfo(
        "seletar-terminal",
        "Seletar Terminal Area (coarse)",
        "terminal",
        "SG",
        [[1.38, 103.82], [1.38, 103.92], [1.46, 103.92], [1.46, 103.82]],
        notes="Approximate area around Seletar — light/business traffic at low altitude.",
    ),
    ZoneInfo(
        "sg-strait-corridor",
        "Singapore Strait Overwater Corridor",
        "corridor",
        "SG",
        [[1.05, 103.50], [1.05, 104.30], [1.30, 104.30], [1.30, 103.50]],
        sensitive=True,
        notes="Overwater corridor above the world's busiest strait — GNSS-interference watch.",
    ),
    ZoneInfo(
        "riau-border-watch",
        "Riau Archipelago Border Watch",
        "border",
        None,
        [[0.50, 103.80], [0.50, 104.70], [1.10, 104.70], [1.10, 103.80]],
        sensitive=True,
        notes="Cross-border low-level activity watch box south of the Singapore Strait.",
    ),
    ZoneInfo(
        "malacca-air-approach",
        "Malacca Strait Air Approaches",
        "corridor",
        None,
        [[1.60, 101.80], [1.60, 103.30], [3.20, 103.30], [3.20, 101.80]],
        notes="High-density airway corridor over the Malacca approaches.",
    ),
    ZoneInfo(
        "scs-gray-zone",
        "South China Sea (central) Watch",
        "watch",
        None,
        [[4.0, 109.0], [4.0, 118.0], [16.0, 118.0], [16.0, 109.0]],
        sensitive=True,
        notes="Contested region with recurring open-source reports of GNSS interference.",
    ),
]

_BY_ID: dict[str, ZoneInfo] = {z.zone_id: z for z in REGISTRY}


def all_zones() -> list[ZoneInfo]:
    return list(REGISTRY)


def zone_by_id(zone_id: str) -> ZoneInfo | None:
    return _BY_ID.get(zone_id)


def zones_containing(lat: float, lon: float) -> list[ZoneInfo]:
    """Every registry zone containing (lat, lon) — zones can overlap (a terminal in a corridor)."""
    return [z for z in REGISTRY if point_in_polygon(lat, lon, [(p[0], p[1]) for p in z.polygon])]


def in_kind(lat: float, lon: float, kind: str) -> bool:
    """True if (lat, lon) falls inside any zone of the given kind (e.g. a terminal area)."""
    return any(z.kind == kind for z in zones_containing(lat, lon))


def zone_for(lat: float, lon: float, *, sensitive_only: bool = False) -> ZoneInfo | None:
    """The first registry zone containing (lat, lon), or None.

    Sensitive zones are tested first so a point inside an overlapping watch area is
    attributed to it. `sensitive_only` restricts the search to watch areas.
    """
    ordered = sorted(REGISTRY, key=lambda z: not z.sensitive)
    for zone in ordered:
        if sensitive_only and not zone.sensitive:
            continue
        if point_in_polygon(lat, lon, [(p[0], p[1]) for p in zone.polygon]):
            return zone
    return None
