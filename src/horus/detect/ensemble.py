"""Composite air picture — fuse per-detector incidents into reviewable rollups.

The analogue of PHAROS's per-vessel maritime rollup: per-aircraft entries (which
detectors agree, unioned technique tags, a transparent risk score) plus the area-level
GNSS-interference incidents alongside — the "one screen" a watch officer would triage.
The risk arithmetic is deliberately simple and fully exposed in the output: reviewers
must be able to see *why* a row ranks where it does.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from horus.db.models import Incident
from horus.zones import zone_by_id

# A second agreeing detector matters more than a higher score on one.
_AGREEMENT_BONUS = 0.15
_SENSITIVE_ZONE_BONUS = 0.1


def _rollup(icao24: str, incidents: list[Incident]) -> dict[str, Any]:
    detectors = sorted({i.detector for i in incidents})
    techniques = sorted({t for i in incidents for t in (i.techniques or [])})
    best = max(incidents, key=lambda i: i.score)
    in_sensitive = any(
        (z := zone_by_id(i.zone_id)) is not None and z.sensitive for i in incidents if i.zone_id
    )
    risk = min(
        1.0,
        best.score
        + _AGREEMENT_BONUS * (len(detectors) - 1)
        + (_SENSITIVE_ZONE_BONUS if in_sensitive else 0.0),
    )
    return {
        "icao24": icao24,
        "risk": round(risk, 3),
        "risk_breakdown": {
            "best_incident_score": best.score,
            "agreeing_detectors": len(detectors),
            "agreement_bonus": round(_AGREEMENT_BONUS * (len(detectors) - 1), 3),
            "sensitive_zone_bonus": _SENSITIVE_ZONE_BONUS if in_sensitive else 0.0,
        },
        "detectors": detectors,
        "techniques": techniques,
        "reliability": min(i.reliability for i in incidents),  # best (A < F) evidence grade
        "incidents": [i.incident_id for i in incidents],
        "zone": best.zone_id,
        "ts_start": min(i.ts_start for i in incidents).isoformat(),
    }


def air_picture(session: Session, region: str | None = None) -> dict[str, Any]:
    """{aircraft: [rollups riskiest-first], areas: [area-level incidents]}."""
    q = select(Incident)
    if region:
        q = q.where(Incident.region == region)
    by_aircraft: dict[str, list[Incident]] = {}
    areas: list[Incident] = []
    for inc in session.scalars(q):
        if inc.icao24 is None:
            areas.append(inc)
        else:
            by_aircraft.setdefault(inc.icao24, []).append(inc)

    rollups = sorted(
        (_rollup(icao, incs) for icao, incs in by_aircraft.items()),
        key=lambda r: -float(r["risk"]),
    )
    areas.sort(key=lambda i: -i.score)
    return {
        "aircraft": rollups,
        "areas": [
            {
                "incident_id": a.incident_id,
                "incident_type": a.incident_type,
                "score": a.score,
                "severity": a.severity,
                "reliability": a.reliability,
                "lat": a.lat,
                "lon": a.lon,
                "zone": a.zone_id,
                "affected_count": a.affected_count,
                "ts_start": a.ts_start.isoformat(),
                "ts_end": a.ts_end.isoformat() if a.ts_end else None,
                "evidence": a.evidence,
            }
            for a in areas
        ],
    }
