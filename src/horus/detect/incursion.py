"""Low-level watch-box incursion detector.

A notable cross-border profile is an aircraft sustaining *genuinely low, roughly-level*
flight inside a border/watch box (kind "border" — e.g. the Riau box). Zones are coarse
illustrative rings, so an incident is framing for human review, not a violation claim.

**The real-traffic confound, handled not hidden.** Measured over 18 h of real Singapore
traffic, the naive form of this detector (any low-ish aircraft with a few in-box samples)
was dominated by ordinary terminal traffic: the Riau box overlaps Changi/Batam approach
airspace, so a 10,000 ft floor caught the whole arrival stream — A380s, 777s, 787s on
scheduled airline callsigns. Three domain-correct rules narrow it to the plausible cases:

- A **dedicated low floor** (5,000 ft), not the gap detector's 10,000 ft — a low-level
  incursion is actually low, not an airliner passing under 10k on approach.
- **Contiguous visits**: an aircraft's in-box samples are split at silences, so "dwell" is
  a real continuous presence rather than a span across a day of repeated transits.
- **Roughly-level flight**: an aircraft climbing or descending through the box is
  transitioning to/from an airport (an approach or departure), not loitering low — the same
  insight as the dark-aircraft descent exclusion. Missing vertical rate fails open.

The remaining survivors still include regional airliners on the low inter-island airway
that overlaps the box; that residual confound is recorded in `docs/EVAL.md`, not claimed
away, and mirrors the maritime sibling's loitering-vs-anchorage limitation.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.orm import Session

from horus.config import Settings, get_settings
from horus.db.models import Incident, Position
from horus.detect.base import TECH_INCURSION, make_incident
from horus.logging import get_logger
from horus.timeutil import utc_naive
from horus.zones import zones_containing

log = get_logger(__name__)

_INCURSION_KINDS = frozenset({"border"})


def _visits(samples: list[Position], gap_minutes: float) -> list[list[Position]]:
    """Split one aircraft's in-box samples into contiguous visits at silences."""
    out: list[list[Position]] = [[samples[0]]]
    for prev, cur in pairwise(samples):
        if (utc_naive(cur.ts) - utc_naive(prev.ts)).total_seconds() / 60.0 > gap_minutes:
            out.append([cur])
        else:
            out[-1].append(cur)
    return out


def _is_level(visit: list[Position], max_rate_fpm: float) -> bool:
    """Roughly level = the median absolute vertical rate is small. No rate data → level.

    Failing open on missing data is deliberate: an aircraft genuinely holding low altitude
    but not broadcasting a vertical rate should still be surfaced for review, not silently
    dropped. A climbing/descending airliner reliably reports its rate, so this does not
    weaken the exclusion of the approach/departure class it targets.
    """
    rates = [abs(p.baro_rate_fpm) for p in visit if p.baro_rate_fpm is not None]
    if not rates:
        return True
    return statistics.median(rates) < max_rate_fpm


def detect_incursions(
    session: Session, region: str | None = None, *, since: datetime | None = None
) -> list[Incident]:
    """Low-level watch-box incursions. `since` scopes the scan for the incremental lane."""
    s = get_settings()
    q = select(Position).where(Position.on_ground.is_(False)).order_by(Position.ts)
    if region:
        q = q.where(Position.region == region)
    if since is not None:
        q = q.where(Position.ts >= since)

    # (icao24, zone_id) -> ordered in-box samples below the dedicated low floor. The floor is
    # incursion's own (5k), NOT the gap detector's 10k — the whole point of the triage.
    hits: dict[tuple[str, str], list[Position]] = {}
    for p in session.scalars(q):
        if (p.alt_baro_ft or 0.0) >= s.incursion_max_altitude_ft:
            continue  # above the low-level band: ordinary traffic
        for zone in zones_containing(p.lat, p.lon):
            if zone.kind in _INCURSION_KINDS:
                hits.setdefault((p.icao24, zone.zone_id), []).append(p)

    incidents: list[Incident] = []
    for (icao24, zone_id), samples in hits.items():
        for visit in _visits(samples, s.incursion_visit_gap_minutes):
            if len(visit) < 3:
                continue  # a clipped corner isn't a dwell
            if not _is_level(visit, s.incursion_max_level_rate_fpm):
                continue  # climbing/descending through the box = an approach/departure
            dwell_min = (visit[-1].ts - visit[0].ts).total_seconds() / 60.0
            incidents.append(_make(icao24, zone_id, visit, dwell_min, region, s))
    log.info("detect_incursions", incidents=len(incidents))
    return incidents


def _make(
    icao24: str,
    zone_id: str,
    visit: list[Position],
    dwell_min: float,
    region: str | None,
    s: Settings,
) -> Incident:
    rates = [p.baro_rate_fpm for p in visit if p.baro_rate_fpm is not None]
    return make_incident(
        # Visit-start timestamp keeps the id unique per visit and time-scoped (an
        # untimestamped id collides across incremental refresh windows).
        incident_id=f"incursion:{icao24}:{zone_id}:{utc_naive(visit[0].ts).isoformat()}",
        detector="incursion",
        incident_type="low-level watch-box incursion",
        score=min(1.0, 0.5 + 0.02 * dwell_min),
        reliability="C",  # well-sampled low-level positions inside the ring
        ts_start=visit[0].ts,
        ts_end=visit[-1].ts,
        lat=visit[len(visit) // 2].lat,
        lon=visit[len(visit) // 2].lon,
        icao24=icao24,
        techniques=[TECH_INCURSION],
        evidence={
            "zone_id": zone_id,
            "samples_in_visit": len(visit),
            "dwell_minutes": round(dwell_min, 1),
            "min_alt_ft": min(float(p.alt_baro_ft or 0.0) for p in visit),
            "max_alt_ft": max(float(p.alt_baro_ft or 0.0) for p in visit),
            "median_abs_vertical_rate_fpm": (
                round(statistics.median([abs(r) for r in rates])) if rates else None
            ),
            "altitude_floor_ft": s.incursion_max_altitude_ft,
            "caveat": "watch rings are illustrative, not authoritative airspace; the box "
            "overlaps a busy low-level regional airway, so survivors may still be routine",
        },
        region=visit[0].region or region,
    )
