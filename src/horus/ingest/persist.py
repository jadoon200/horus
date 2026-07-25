"""Idempotent persistence for ADS-B samples.

Re-polling overlapping snapshots is the normal case (every poll re-reports slow-moving
state), so persistence must dedup on (icao24, ts, source, region) — timezone-safely,
because SQLite round-trips aware datetimes as naive clock fields (see horus.timeutil, a
lesson inherited from the PHAROS pilot). Source + region are part of identity so an
optional cross-check can coexist without contaminating or silently displacing the primary
keyless lane.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from horus.db.models import Aircraft, Position
from horus.ingest.adsb import AdsbSample
from horus.logging import get_logger
from horus.timeutil import utc_naive

log = get_logger(__name__)

# Slack around a batch's time span when checking for already-stored duplicates; covers
# sub-second truncation without widening the scan.
_DEDUP_MARGIN = timedelta(minutes=1)


def _upsert_aircraft(session: Session, sample: AdsbSample) -> None:
    row = session.get(Aircraft, sample.icao24)
    if row is None:
        row = Aircraft(icao24=sample.icao24, first_seen=sample.ts)
        session.add(row)
    # Identity fields are advisory and can appear/refresh at any poll.
    if sample.callsign:
        row.callsign = sample.callsign
    if sample.registration:
        row.registration = sample.registration
    if sample.type_code:
        row.type_code = sample.type_code
    if sample.category:
        row.category = sample.category
    if row.first_seen is None or utc_naive(sample.ts) < utc_naive(row.first_seen):
        row.first_seen = sample.ts
    if row.last_seen is None or utc_naive(sample.ts) > utc_naive(row.last_seen):
        row.last_seen = sample.ts


def persist_samples(
    session: Session, samples: Iterable[AdsbSample], *, source: str, region: str | None
) -> int:
    """Insert new positions (and upsert aircraft identity); returns rows inserted.

    Dedup: one position per (icao24, second-resolution ts, source, region). Existing keys
    are read back tz-normalized so the check holds on SQLite and Postgres alike.
    """
    batch = list(samples)
    if not batch:
        return 0
    icaos = {s.icao24 for s in batch}
    # Bound the dedup lookup to the batch's own time span. Only a stored position whose
    # second-truncated timestamp equals one in this batch can collide, so a wider window is
    # wasted work — and unbounded it is a scaling trap: a long-running collector would load
    # every historical row for every aircraft currently in view on each poll.
    lo = min(s.ts for s in batch) - _DEDUP_MARGIN
    hi = max(s.ts for s in batch) + _DEDUP_MARGIN
    existing = {
        (icao, utc_naive(ts).replace(microsecond=0), stored_source, stored_region)
        for icao, ts, stored_source, stored_region in session.execute(
            select(Position.icao24, Position.ts, Position.source, Position.region).where(
                Position.icao24.in_(icaos), Position.ts >= lo, Position.ts <= hi
            )
        )
    }
    inserted = 0
    seen_in_batch: set[tuple[str, object, str, str | None]] = set()
    for s in batch:
        key = (s.icao24, utc_naive(s.ts).replace(microsecond=0), source, region)
        if key in existing or key in seen_in_batch:
            continue
        seen_in_batch.add(key)
        _upsert_aircraft(session, s)
        session.add(
            Position(
                icao24=s.icao24,
                ts=s.ts,
                lat=s.lat,
                lon=s.lon,
                alt_baro_ft=s.alt_baro_ft,
                alt_geom_ft=s.alt_geom_ft,
                gs_kt=s.gs_kt,
                track_deg=s.track_deg,
                baro_rate_fpm=s.baro_rate_fpm,
                on_ground=s.on_ground,
                squawk=s.squawk,
                nic=s.nic,
                nac_p=s.nac_p,
                sil=s.sil,
                rssi=s.rssi,
                source=source,
                region=region,
            )
        )
        inserted += 1
    log.info("persist_samples", batch=len(batch), inserted=inserted)
    return inserted
