"""Incremental processing for the continuous lane — rebuild only what changed.

A full rebuild reprocesses every position ever collected. At 27k rows that costs ~1.6 s,
which is comfortable against a 30 s poll; at two weeks of collection (~700k rows) the same
work would exceed the poll interval and the picture would fall permanently behind the sky.
So this is preventive rather than a fix for a present fault, and it is written to be
obviously correct rather than maximally clever.

Two different problems, handled differently:

- **Tracks are per-aircraft**, so only aircraft with new positions need rebuilding. Their
  existing tracks are deleted and re-derived from that aircraft's own history; untouched
  aircraft keep theirs.
- **GNSS interference is area-level** and depends on *all* aircraft in a cell, so it cannot
  be scoped per aircraft without changing what it measures. It is scoped in *time* instead:
  only recent windows are re-scored, because a cell-window whose reports are all in the past
  cannot change. Older incidents are left exactly as they were.

Anything that would alter a settled result is left alone rather than recomputed
approximately — a stale-but-true incident is better than a fresh-but-wrong one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from horus.config import Settings, get_settings
from horus.db.models import Incident, Position, Track
from horus.detect.gaps import detect_gaps
from horus.detect.incursion import detect_incursions
from horus.detect.jamming import align_to_window, detect_jamming
from horus.detect.spoof import detect_spoof
from horus.detect.squawk import detect_squawks
from horus.logging import get_logger
from horus.timeutil import utc_naive
from horus.tracks.build import segment_aircraft

log = get_logger(__name__)


@dataclass
class CycleStats:
    """What one incremental cycle actually did — logged and asserted against in tests."""

    dirty_aircraft: int = 0
    tracks_rebuilt: int = 0
    incidents_refreshed: int = 0
    window_hours: float = 0.0


def dirty_aircraft_since(
    session: Session, since: datetime | None, region: str | None = None
) -> set[str]:
    """Aircraft with at least one position newer than `since`.

    `None` means "everything" — the first cycle after a restart has no watermark and must
    treat the whole corpus as dirty rather than silently skipping it.
    """
    q = select(Position.icao24).distinct()
    if region:
        q = q.where(Position.region == region)
    if since is not None:
        q = q.where(Position.ts > since)
    return set(session.scalars(q))


def rebuild_tracks_for(
    session: Session, icao24s: set[str], *, region: str | None, settings: Settings
) -> int:
    """Re-derive tracks for the given aircraft only; returns the number written."""
    if not icao24s:
        return 0
    del_q = delete(Track).where(Track.icao24.in_(icao24s))
    if region:
        del_q = del_q.where(Track.region == region)
    session.execute(del_q)

    pos_q = (
        select(Position).where(Position.icao24.in_(icao24s)).order_by(Position.icao24, Position.ts)
    )
    if region:
        pos_q = pos_q.where(Position.region == region)

    by_aircraft: dict[str, list[Position]] = {}
    for p in session.scalars(pos_q):
        if not p.on_ground:
            by_aircraft.setdefault(p.icao24, []).append(p)

    written = 0
    for icao24, positions in by_aircraft.items():
        for track in segment_aircraft(icao24, positions, settings):
            session.add(track)
            written += 1
    return written


def refresh_recent_incidents(
    session: Session, *, since: datetime, region: str | None, settings: Settings
) -> int:
    """Re-run the deterministic detectors over the recent window only.

    Incidents that *start* inside the window are deleted and re-derived. A settled incident
    from before the window is untouched: its inputs are all in the past, so recomputing it
    could only reproduce it or — if the window clipped its evidence — corrupt it.

    `since` is first snapped DOWN to the jamming bucket boundary. Unaligned, a bucket
    straddling it kept its stored incident (starting before the boundary) while the scoped
    detector regenerated the same bucket id from partial evidence — a primary-key
    collision. Alignment widens the refresh by at most one bucket, and re-deriving that
    bucket from its now-fully-visible evidence is idempotent.
    """
    since = align_to_window(since, settings)
    del_q = delete(Incident).where(
        Incident.detector.in_(("jamming", "gap", "incursion", "spoof", "squawk")),
        Incident.ts_start >= since,
    )
    if region:
        del_q = del_q.where(Incident.region == region)
    session.execute(del_q)

    fresh: list[Incident] = []
    jam, _ = detect_jamming(session, region, since=since)
    fresh.extend(jam)
    # `since` is pushed into each detector's query rather than filtering their output:
    # scanning the whole corpus and discarding most of it is what the incremental lane
    # exists to avoid.
    fresh.extend(detect_gaps(session, region, since=since))
    fresh.extend(detect_incursions(session, region, since=since))
    fresh.extend(detect_spoof(session, region, since=since))
    fresh.extend(detect_squawks(session, region, since=since))

    # Any detector can re-derive an incident whose stored row STARTS before the rolling
    # boundary — a squawk visit, a watch-box dwell, a violation streak, or a silence
    # straddling it. Those stable, time-scoped ids escape the window delete above, so they
    # are replaced explicitly: extending the same event must update its row, never collide
    # with it or sit beside it as a duplicate. (Also collapses an id two passes derived in
    # this same transaction to one row.)
    unique = {i.incident_id: i for i in fresh}
    if unique:
        replace_q = delete(Incident).where(Incident.incident_id.in_(unique))
        # Region-scoped like the window delete above: a per-aircraft id (gap/spoof/incursion/
        # squawk) carries no region, so a shared ICAO24 collected under two regions can yield
        # the same id in each. An sg-live cycle must never delete another region's incident.
        if region:
            replace_q = replace_q.where(Incident.region == region)
        session.execute(replace_q)
    session.add_all(list(unique.values()))
    return len(unique)


def process_cycle(
    session: Session,
    *,
    watermark: datetime | None,
    region: str | None = None,
    window_hours: float = 6.0,
    settings: Settings | None = None,
) -> tuple[CycleStats, datetime]:
    """One incremental cycle. Returns the stats and the new watermark.

    The new watermark is the newest position timestamp *observed in this cycle*, not the
    wall clock: taking `now()` would silently skip any report that arrives while the cycle
    is running.
    """
    s = settings or get_settings()
    stats = CycleStats(window_hours=window_hours)

    dirty = dirty_aircraft_since(session, watermark, region)
    stats.dirty_aircraft = len(dirty)
    stats.tracks_rebuilt = rebuild_tracks_for(session, dirty, region=region, settings=s)
    session.flush()

    since = datetime.now(UTC) - timedelta(hours=window_hours)
    stats.incidents_refreshed = refresh_recent_incidents(
        session, since=utc_naive(since), region=region, settings=s
    )

    newest_q = select(Position.ts).order_by(Position.ts.desc()).limit(1)
    if region:
        newest_q = newest_q.where(Position.region == region)
    newest = session.scalar(newest_q)
    new_watermark = utc_naive(newest) if newest else (watermark or utc_naive(datetime.now(UTC)))

    log.info(
        "process_cycle",
        dirty=stats.dirty_aircraft,
        tracks=stats.tracks_rebuilt,
        incidents=stats.incidents_refreshed,
    )
    return stats, new_watermark
