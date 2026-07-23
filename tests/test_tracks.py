from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from horus.config import get_settings
from horus.db.base import Base
from horus.db.models import Track
from horus.ingest.synthetic import generate, seed_db
from horus.tracks.build import build_tracks


def _seeded_session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    seed_db(s, generate())
    s.commit()
    return s


def test_build_tracks_segments_and_descriptors() -> None:
    with _seeded_session() as s:
        n = build_tracks(s)
        s.commit()
        assert n > 0
        tracks = list(s.scalars(select(Track)))
        cfg = get_settings()
        for t in tracks:
            assert t.point_count >= cfg.track_min_points
            assert t.sequence is not None and len(t.sequence) == cfg.track_resample_points - 1
            assert len(t.sequence[0]) == 5
            assert t.features is not None
            assert len(t.features) == (cfg.track_resample_points - 1) * 5


def test_dark_aircraft_splits_into_two_segments() -> None:
    # 20 min of silence far exceeds the 15-min segmentation gap -> two tracks.
    with _seeded_session() as s:
        build_tracks(s)
        s.commit()
        dark = list(s.scalars(select(Track).where(Track.icao24 == "d00001")))
        assert len(dark) == 2


def test_rebuild_is_idempotent() -> None:
    with _seeded_session() as s:
        first = build_tracks(s)
        s.commit()
        second = build_tracks(s)
        s.commit()
        assert first == second
        assert len(list(s.scalars(select(Track)))) == second


def test_track_region_comes_from_its_own_segment_not_the_aircraft() -> None:
    """One aircraft can appear in two regions; each track must keep its own.

    Stamping every track with whichever region was seen last for that aircraft mislabelled
    tracks in a multi-region database, which then made a region-scoped rebuild
    (`delete(Track).where(Track.region == region)`) delete the wrong rows.
    """
    from datetime import UTC, datetime, timedelta

    from horus.db.models import Aircraft, Position

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Aircraft(icao24="abc123"))
        base = datetime(2026, 7, 1, tzinfo=UTC)
        # 20 points in "historical", then a 2-hour break, then 20 in "sg-live".
        for i in range(20):
            s.add(
                Position(
                    icao24="abc123",
                    ts=base + timedelta(seconds=30 * i),
                    lat=1.3 + 0.01 * i,
                    lon=103.8,
                    alt_baro_ft=35000.0,
                    region="historical",
                )
            )
        for i in range(20):
            s.add(
                Position(
                    icao24="abc123",
                    ts=base + timedelta(hours=2, seconds=30 * i),
                    lat=1.3 + 0.01 * i,
                    lon=104.2,
                    alt_baro_ft=35000.0,
                    region="sg-live",
                )
            )
        s.commit()

        build_tracks(s)
        s.commit()
        by_region = {t.region for t in s.scalars(select(Track))}
        assert by_region == {"historical", "sg-live"}, (
            "each segment must carry its own region, not the aircraft's last-seen one"
        )
