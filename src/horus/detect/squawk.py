"""Emergency-squawk notable-event channel.

7500/7600/7700 are aircraft self-reports, not diagnoses. A repeated code is surfaced for
human review with a modest score and C-grade evidence; it is never described as a hijack,
radio failure, or emergency verdict. Consecutive same-code samples form one visit, so a
long broadcast becomes one review row rather than an alert storm.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from horus.config import get_settings
from horus.db.models import Incident, Position
from horus.detect.base import TECH_EMERGENCY, make_incident
from horus.timeutil import utc_naive

_MEANINGS = {
    "7500": "unlawful-interference code",
    "7600": "radio-communication-failure code",
    "7700": "general-emergency code",
}


@dataclass
class _Visit:
    icao24: str
    code: str
    first: Position
    last: Position
    samples: list[Position] = field(default_factory=list)


def _incident(visit: _Visit, min_samples: int) -> Incident | None:
    if len(visit.samples) < min_samples:
        return None
    start = utc_naive(visit.first.ts)
    end = utc_naive(visit.last.ts)
    return make_incident(
        incident_id=f"squawk:{visit.icao24}:{visit.code}:{start.isoformat()}",
        detector="squawk",
        incident_type=f"emergency squawk {visit.code}",
        # Deliberately moderate: this is a single-aircraft self-report and may be erroneous.
        score=0.55,
        reliability="C",
        ts_start=start,
        ts_end=end,
        lat=visit.last.lat,
        lon=visit.last.lon,
        icao24=visit.icao24,
        techniques=[TECH_EMERGENCY],
        evidence={
            "squawk": visit.code,
            "meaning": _MEANINGS.get(visit.code, "configured emergency code"),
            "samples": len(visit.samples),
            "duration_seconds": max(0.0, (end - start).total_seconds()),
            "caveat": "aircraft self-report for human review; miscoding is possible",
        },
        region=visit.last.region,
    )


def detect_squawks(
    session: Session, region: str | None = None, *, since: datetime | None = None
) -> list[Incident]:
    """Return one incident per repeated same-code visit.

    Incremental runs first find aircraft with an emergency sample in the refresh window,
    then load only those aircraft's history. That preserves the visit's true start/id while
    keeping warm-cycle work bounded by rare candidate aircraft rather than the corpus.
    """
    settings = get_settings()
    codes = set(settings.squawk_emergency_codes)

    targets: set[str] | None = None
    if since is not None:
        target_q = select(Position.icao24).where(
            Position.ts >= since,
            Position.squawk.in_(codes),
        )
        if region:
            target_q = target_q.where(Position.region == region)
        targets = set(session.scalars(target_q.distinct()))
        if not targets:
            return []

    q = select(Position).where(Position.squawk.is_not(None))
    if targets is not None:
        q = q.where(Position.icao24.in_(targets))
    if region:
        q = q.where(Position.region == region)
    q = q.order_by(Position.icao24, Position.ts)

    gap = timedelta(minutes=settings.track_gap_minutes)
    visits: list[_Visit] = []
    active: _Visit | None = None

    def finish() -> None:
        nonlocal active
        if active is not None:
            visits.append(active)
        active = None

    for position in session.scalars(q):
        code = position.squawk
        if code not in codes:
            finish()
            continue
        continues = (
            active is not None
            and active.icao24 == position.icao24
            and active.code == code
            and utc_naive(position.ts) - utc_naive(active.last.ts) <= gap
        )
        if not continues:
            finish()
            active = _Visit(
                icao24=position.icao24,
                code=code,
                first=position,
                last=position,
            )
        assert active is not None
        active.last = position
        active.samples.append(position)
    finish()

    out: list[Incident] = []
    for visit in visits:
        if since is not None and utc_naive(visit.last.ts) < utc_naive(since):
            continue
        incident = _incident(visit, settings.squawk_min_samples)
        if incident is not None:
            out.append(incident)
    return out
