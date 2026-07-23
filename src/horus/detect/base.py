"""Shared detector plumbing: air-threat tags, reliability grading, incident assembly.

Every incident is human-review decision support, never an automated verdict. The
`reliability` grade is a NATO-Admiralty-style statement about the *ADS-B evidence
quality* (sample density, altitude regime, coverage health), separate from the detector's
confidence `score` — a strong pattern on weak evidence must look different from a strong
pattern on strong evidence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from horus.db.models import Incident
from horus.zones import zone_for

# Air-threat technique tags — the ATT&CK analogue for the air domain (portfolio-local
# vocabulary, kept deliberately small and explained in the dashboard's explainer view).
TECH_JAMMING = "AT-GNSS-JAM"  # area GNSS interference (integrity collapse across aircraft)
TECH_DARK = "AT-DARK-AIRCRAFT"  # transponder silence at altitude with displacement
TECH_INCURSION = "AT-ZONE-INCURSION"  # low-level entry into a watch/border box
TECH_SPOOF = "AT-KINEMATIC-SPOOF"  # physically impossible kinematics / identity anomaly
TECH_ANOMALY = "AT-TRAJ-ANOMALY"  # trajectory shape far from the learned pattern of life


def severity_for(score: float) -> str:
    if score >= 0.85:
        return "critical"
    if score >= 0.65:
        return "high"
    if score >= 0.4:
        return "moderate"
    return "low"


def grade_samples(n_aircraft: int, *, at_altitude: bool = True) -> str:
    """Admiralty-style ADS-B confidence grade from corroboration and regime.

    More independently-observed aircraft → higher confidence that the *signal* (not one
    faulty unit) is real. Low-altitude evidence is capped a grade lower: reception there
    is terrain-limited, so absence/degradation is weaker evidence.
    """
    if n_aircraft >= 8:
        grade = "B"
    elif n_aircraft >= 4:
        grade = "C"
    elif n_aircraft >= 2:
        grade = "D"
    else:
        grade = "E"
    if not at_altitude and grade < "E":
        grade = chr(ord(grade) + 1)
    return grade


def make_incident(
    *,
    incident_id: str,
    detector: str,
    incident_type: str,
    score: float,
    reliability: str,
    ts_start: datetime,
    ts_end: datetime | None,
    lat: float | None,
    lon: float | None,
    icao24: str | None = None,
    track_id: str | None = None,
    affected_count: int | None = None,
    techniques: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
    region: str | None = None,
) -> Incident:
    zone = zone_for(lat, lon) if lat is not None and lon is not None else None
    return Incident(
        incident_id=incident_id,
        icao24=icao24,
        track_id=track_id,
        detector=detector,
        incident_type=incident_type,
        score=max(0.0, min(1.0, score)),
        severity=severity_for(score),
        reliability=reliability,
        ts_start=ts_start,
        ts_end=ts_end,
        lat=lat,
        lon=lon,
        zone_id=zone.zone_id if zone else None,
        affected_count=affected_count,
        techniques=techniques or [],
        evidence=evidence or {},
        region=region,
    )
