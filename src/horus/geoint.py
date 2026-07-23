"""GEOINT evidence bridge — expose HORUS incidents as citable, source-rated evidence.

The composability payoff, mirrored from PHAROS: each air incident is shaped into an
evidence item whose fields match ARGUS's `EvidenceItem` (doc_id / title / source /
reliability A-F / credibility 1-6 / summary / published / url), so ARGUS's read-only
bridge pattern (the way it already consumes SENTINEL campaigns and PHAROS incidents) can
cite the air lane with no schema translation — plus air-domain extras (icao24 / zone /
techniques / affected_count) an all-source tool can use or ignore.

HORUS only *serves* this; it is read-only, and every item stays human-review support.
"""

from __future__ import annotations

from typing import Any

from horus.db.models import Incident
from horus.zones import zone_by_id

_DETECTOR_PHRASE = {
    "jamming": "GNSS interference",
    "gap": "Dark aircraft (transponder gap)",
    "incursion": "Low-level watch-box incursion",
    "spoof": "Kinematic impossibility / identity anomaly",
    "anomaly": "Trajectory anomaly",
}


def credibility_from_score(score: float) -> int:
    """Map a detector score [0,1] to NATO-Admiralty information-credibility 2-6.

    `1` (confirmed) is reserved — a single ADS-B-derived signal is never "confirmed by
    other sources". Higher score → more credible (lower number).
    """
    if score >= 0.85:
        return 2
    if score >= 0.65:
        return 3
    if score >= 0.45:
        return 4
    if score >= 0.25:
        return 5
    return 6


def incident_summary(inc: Incident) -> str:
    phrase = _DETECTOR_PHRASE.get(inc.detector, inc.incident_type)
    where = ""
    if inc.zone_id and (zone := zone_by_id(inc.zone_id)):
        where = f" in the {zone.name}"
    ev = inc.evidence or {}
    detail = ""
    if inc.detector == "jamming":
        detail = (
            f" — {ev.get('aircraft_degraded', '?')}/{ev.get('aircraft_observed', '?')} aircraft "
            f"degraded (worst-NIC cluster)"
        )
    elif inc.detector == "gap":
        detail = (
            f" — silent {ev.get('gap_minutes', '?')} min at altitude, reappearing "
            f"{ev.get('displacement_km', '?')} km displaced"
        )
    elif inc.detector == "incursion":
        detail = f" — dwelling {ev.get('dwell_minutes', '?')} min below the altitude floor"
    elif inc.detector == "spoof":
        detail = f" — implied speed {ev.get('max_implied_speed_kt', '?')} kt (impossible)"
    subject = f"aircraft {inc.icao24}" if inc.icao24 else "area-level signal"
    caveat = " A gap may be benign coverage loss." if inc.detector == "gap" else ""
    return (
        f"{phrase}: {subject}{where}{detail}. Human-review decision support, not a verdict.{caveat}"
    )


def to_evidence(inc: Incident) -> dict[str, Any]:
    """Shape an incident as an ARGUS-compatible evidence item + air-domain extras."""
    phrase = _DETECTOR_PHRASE.get(inc.detector, inc.incident_type)
    zone = zone_by_id(inc.zone_id) if inc.zone_id else None
    subject = f"aircraft {inc.icao24}" if inc.icao24 else (zone.name if zone else "area")
    return {
        # ARGUS EvidenceItem fields (map 1:1) --------------------------------------------
        "doc_id": inc.incident_id,
        "title": f"{phrase} - {subject}",
        "source": "HORUS air domain awareness",
        "reliability": inc.reliability,  # Admiralty A-F (ADS-B confidence)
        "credibility": credibility_from_score(inc.score),  # Admiralty 1-6
        "summary": incident_summary(inc),
        "published": inc.ts_start.isoformat(),
        "url": f"/incidents/{inc.incident_id}",  # resolvable on this API
        # Air-domain extras (an all-source tool may use or ignore) ------------------------
        "kind": "geoint-air",
        "detector": inc.detector,
        "icao24": inc.icao24,
        "lat": inc.lat,
        "lon": inc.lon,
        "zone": zone.name if zone else None,
        "affected_count": inc.affected_count,
        "techniques": inc.techniques or [],
        "region": inc.region,
    }
