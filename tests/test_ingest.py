from datetime import UTC, datetime

import httpx
import respx
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from horus.db.base import Base
from horus.db.models import Aircraft, Position
from horus.ingest.adsb import fetch_point, parse_aircraft
from horus.ingest.persist import persist_samples

_NOW = datetime(2026, 7, 23, 4, 0, 0, tzinfo=UTC)

# One realistic readsb entry (the fields adsb.lol actually serves), plus edge cases.
_GOOD = {
    "hex": "76ceef",
    "flight": "SIA321  ",
    "r": "9V-SHH",
    "t": "A359",
    "category": "A5",
    "lat": 1.31,
    "lon": 103.95,
    "alt_baro": 12000,
    "alt_geom": 12500,
    "gs": 310.5,
    "track": 82.3,
    "baro_rate": -640,
    "squawk": "2057",
    "nic": 8,
    "nac_p": 9,
    "sil": 3,
    "rssi": -21.5,
    "seen_pos": 2.1,
}


def _session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_parse_good_entry_corrects_position_age() -> None:
    s = parse_aircraft(_GOOD, _NOW, max_seen_pos_seconds=60)
    assert s is not None
    assert s.icao24 == "76ceef" and s.callsign == "SIA321"
    assert (s.nic, s.nac_p, s.sil) == (8, 9, 3)
    # ts is corrected backwards by seen_pos (2.1 s).
    assert abs((_NOW - s.ts).total_seconds() - 2.1) < 0.01
    assert s.on_ground is False and s.alt_baro_ft == 12000.0


def test_parse_rejects_unusable_entries() -> None:
    assert parse_aircraft({}, _NOW, max_seen_pos_seconds=60) is None  # no hex
    assert (  # TIS-B synthetic id — no stable aircraft identity
        parse_aircraft({**_GOOD, "hex": "~2f00a1"}, _NOW, max_seen_pos_seconds=60) is None
    )
    no_pos = {k: v for k, v in _GOOD.items() if k not in ("lat", "lon")}
    assert parse_aircraft(no_pos, _NOW, max_seen_pos_seconds=60) is None
    stale = {**_GOOD, "seen_pos": 300.0}  # coasted estimate, not a fresh report
    assert parse_aircraft(stale, _NOW, max_seen_pos_seconds=60) is None


def test_parse_ground_altitude() -> None:
    s = parse_aircraft({**_GOOD, "alt_baro": "ground"}, _NOW, max_seen_pos_seconds=60)
    assert s is not None
    assert s.on_ground is True and s.alt_baro_ft is None


@respx.mock
def test_fetch_point_parses_snapshot() -> None:
    respx.get("https://api.adsb.lol/v2/point/1.35/103.82/250").mock(
        return_value=httpx.Response(
            200,
            json={
                "ac": [_GOOD, {**_GOOD, "hex": "76cf01", "seen_pos": 500.0}, "junk"],
                "total": 2,
                "now": int(_NOW.timestamp() * 1000),
            },
        )
    )
    samples = fetch_point(1.35, 103.82, 250)
    # The stale plot and the junk entry are dropped; the fresh one survives.
    assert [s.icao24 for s in samples] == ["76ceef"]


def test_persist_dedups_and_upserts_identity() -> None:
    s1 = parse_aircraft(_GOOD, _NOW, max_seen_pos_seconds=60)
    assert s1 is not None
    with _session() as db:
        assert persist_samples(db, [s1], source="adsb-lol", region="sg") == 1
        db.commit()
        # Same sample re-polled -> deduped; new identity detail still upserts.
        assert persist_samples(db, [s1], source="adsb-lol", region="sg") == 0
        db.commit()
        assert db.scalar(select(Position).limit(1)) is not None
        aircraft = db.get(Aircraft, "76ceef")
        assert aircraft is not None and aircraft.callsign == "SIA321"
        assert aircraft.type_code == "A359"


def test_dedup_lookup_is_bounded_but_still_catches_duplicates() -> None:
    """Re-polling must dedup, without scanning an aircraft's whole history each time.

    The lookup is bounded to the batch's own time span: only a stored position whose
    second-truncated timestamp matches one in the batch can collide. Unbounded, a
    long-running collector would load every historical row for every aircraft in view on
    every poll — a scaling trap for the live lane.
    """
    from datetime import timedelta

    s1 = parse_aircraft(_GOOD, _NOW, max_seen_pos_seconds=60)
    assert s1 is not None
    with _session() as db:
        # A year of history for this aircraft, far outside any future batch's window.
        old = parse_aircraft(_GOOD, _NOW - timedelta(days=365), max_seen_pos_seconds=60)
        assert old is not None
        assert persist_samples(db, [old], source="adsb-lol", region="sg") == 1
        db.commit()

        # A fresh sample still inserts (history is irrelevant to it)...
        assert persist_samples(db, [s1], source="adsb-lol", region="sg") == 1
        db.commit()
        # ...and re-polling the same sample is still deduped despite the bounded lookup.
        assert persist_samples(db, [s1], source="adsb-lol", region="sg") == 0
        db.commit()
        assert db.scalar(select(func.count()).select_from(Position)) == 2
