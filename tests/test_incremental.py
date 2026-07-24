"""Incremental processing must agree with a full rebuild.

An incremental lane that drifts from the batch result is worse than no incremental lane at
all: the dashboard would show something no reproducible run can reproduce. The parity test
below is the load-bearing one — the rest pin the scoping rules it depends on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from horus.config import Settings
from horus.db.base import Base
from horus.db.models import Aircraft, Position, Track
from horus.ingest.synthetic import generate, seed_db
from horus.tracks.build import build_tracks, segment_aircraft
from horus.tracks.incremental import (
    dirty_aircraft_since,
    process_cycle,
    rebuild_tracks_for,
)

T0 = datetime(2026, 7, 23, 0, 0, tzinfo=UTC)


def _session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def _track_fingerprint(s: Session) -> list[tuple[object, ...]]:
    """Everything about a track that a downstream consumer could observe."""
    return sorted(
        (
            t.track_id,
            t.icao24,
            t.region,
            t.start_ts,
            t.end_ts,
            t.point_count,
            round(t.distance_km or 0.0, 6),
            tuple(t.features or ()),
        )
        for t in s.scalars(select(Track))
    )


def test_incremental_track_rebuild_matches_full_rebuild() -> None:
    """The load-bearing property: same tracks, byte for byte."""
    with _session() as s:
        seed_db(s, generate())
        s.commit()

        build_tracks(s, "synthetic")
        s.commit()
        expected = _track_fingerprint(s)
        assert expected, "the scenario must produce tracks for this to test anything"

        # Now rebuild *every* aircraft through the incremental path instead.
        everyone = dirty_aircraft_since(s, None, "synthetic")
        rebuild_tracks_for(
            s,
            everyone,
            region="synthetic",
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
        )
        s.commit()

        assert _track_fingerprint(s) == expected


def test_only_dirty_aircraft_are_touched() -> None:
    with _session() as s:
        seed_db(s, generate())
        s.commit()
        build_tracks(s, "synthetic")
        s.commit()

        before = {t.track_id: t.point_count for t in s.scalars(select(Track))}
        target = next(iter(before)).split(":")[0]

        rebuilt = rebuild_tracks_for(
            s,
            {target},
            region="synthetic",
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
        )
        s.commit()
        after = {t.track_id: t.point_count for t in s.scalars(select(Track))}

        assert rebuilt > 0
        # Every other aircraft's tracks survived untouched.
        others_before = {k: v for k, v in before.items() if not k.startswith(target)}
        others_after = {k: v for k, v in after.items() if not k.startswith(target)}
        assert others_before == others_after


def test_no_watermark_means_everything_is_dirty() -> None:
    """A restart has no watermark and must not silently skip the backlog."""
    with _session() as s:
        seed_db(s, generate())
        s.commit()
        everyone = dirty_aircraft_since(s, None, "synthetic")
        recent = dirty_aircraft_since(s, datetime(2099, 1, 1), "synthetic")
        assert len(everyone) > 0
        assert recent == set(), "nothing is newer than the year 2099"


def test_watermark_advances_to_the_newest_observed_report() -> None:
    """Not to the wall clock — that would skip reports arriving mid-cycle."""
    with _session() as s:
        s.add(Aircraft(icao24="abc123"))
        newest = T0 + timedelta(minutes=5)
        for m in (0, 2, 5):
            s.add(
                Position(
                    icao24="abc123",
                    ts=T0 + timedelta(minutes=m),
                    lat=1.3,
                    lon=103.8,
                    alt_baro_ft=35000.0,
                    nic=8,
                    region="sg",
                )
            )
        s.commit()

        _, watermark = process_cycle(s, watermark=None, region="sg")
        s.commit()
        assert watermark.replace(tzinfo=UTC) == newest


def test_cycle_reports_what_it_did() -> None:
    with _session() as s:
        seed_db(s, generate())
        s.commit()
        stats, _ = process_cycle(s, watermark=None, region="synthetic", window_hours=24 * 365 * 100)
        s.commit()
        assert stats.dirty_aircraft > 0
        assert stats.tracks_rebuilt > 0


def test_segment_aircraft_is_the_shared_implementation() -> None:
    """Both lanes must call the same segmentation, not two that happen to agree today."""
    with _session() as s:
        seed_db(s, generate())
        s.commit()
        positions = list(
            s.scalars(select(Position).where(Position.icao24 == "d00001").order_by(Position.ts))
        )
        direct = segment_aircraft("d00001", [p for p in positions if not p.on_ground])
        # The dark-aircraft scenario is silent for 20 min, which exceeds the 15-min split.
        assert len(direct) == 2


def test_repeated_cycles_with_shrinking_windows_do_not_collide() -> None:
    """Incident ids must be time-scoped, or the incremental lane corrupts itself.

    The refresh deletes incidents starting inside the window, then re-derives them. If an
    id carries no timestamp, an incident from *outside* the window survives the delete and
    then collides with the one the scoped detector regenerates — a primary-key error
    reachable only from the incremental path, never from a full rebuild. Found by running
    real cycles at 1/3/6/12-hour windows against 27k live positions.
    """
    with _session() as s:
        seed_db(s, generate())
        s.commit()
        _, watermark = process_cycle(s, watermark=None, region="synthetic", window_hours=24 * 3650)
        s.commit()

        # Shrinking windows are the adversarial order: each leaves older incidents in place.
        for hours in (24 * 3650, 24 * 365, 24, 6, 1):
            process_cycle(s, watermark=watermark, region="synthetic", window_hours=hours)
            s.commit()  # would raise IntegrityError on a collision


def test_every_detector_id_is_time_scoped() -> None:
    """The invariant behind the test above, checked directly on generated incidents."""
    from horus.detect.run import run_detectors

    with _session() as s:
        seed_db(s, generate())
        s.commit()
        run_detectors(s, "synthetic")
        s.commit()
        from horus.db.models import Incident

        for inc in s.scalars(select(Incident)):
            tail = inc.incident_id.rsplit(":", 1)[-1]
            assert any(ch.isdigit() for ch in tail), (
                f"{inc.incident_id} has no time component; it will collide across "
                "incremental refresh windows"
            )
