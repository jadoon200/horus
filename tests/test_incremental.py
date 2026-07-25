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


def test_incremental_detects_a_gap_straddling_the_refresh_boundary() -> None:
    """A silence that opens before `since` but closes inside the window must be found.

    The reappearance is the first moment a gap is detectable at all, so the window scan
    (`ts >= since`) has to reach back for the last pre-boundary report to form the pair.
    Before the seed, a silence longer than the refresh window — the longest, most
    interesting kind — was invisible to the incremental lane, because its opening report
    was never loaded. The re-derived id matches the full scan's, so successive refreshes
    update the same row instead of duplicating it.
    """
    from horus.detect.gaps import detect_gaps

    with _session() as s:
        since = datetime(2026, 7, 23, 6, 0, tzinfo=UTC)
        s.add(Aircraft(icao24="d00099"))
        # Slow and central: the aircraft could not have left the 250 nm coverage circle in
        # a 13-minute gap, so coverage never explains this silence away.
        s.add(
            Position(
                icao24="d00099",
                ts=since - timedelta(minutes=4),  # last seen BEFORE the window
                lat=1.35,
                lon=103.82,
                alt_baro_ft=37000.0,
                gs_kt=120.0,
                baro_rate_fpm=0.0,
                region="r",
            )
        )
        s.add(
            Position(
                icao24="d00099",
                ts=since + timedelta(minutes=9),  # reappears INSIDE the window, 60 km east
                lat=1.35,
                lon=104.36,
                alt_baro_ft=37000.0,
                gs_kt=120.0,
                baro_rate_fpm=0.0,
                region="r",
            )
        )
        s.commit()

        full = [i.incident_id for i in detect_gaps(s, "r")]
        windowed = [i.incident_id for i in detect_gaps(s, "r", since=since.replace(tzinfo=None))]
        assert full, "the full scan must see the gap for this to test anything"
        assert windowed == full, "the boundary-straddling gap must survive the window scope"


def test_incremental_does_not_duplicate_events_straddling_the_boundary() -> None:
    """A spoof streak or incursion visit crossing `since` must update its row, not clone it.

    Both events carry a stable, time-scoped id anchored to their first sample. Deriving from
    the window alone re-anchored that id to whatever fell inside the rolling window, minting
    a fresh id beside the stored one — one duplicate every time the boundary swept across the
    event. The scoped detectors now re-derive from full history for any aircraft active in the
    window, and the refresh replaces by id, so the incremental result equals a full rebuild.
    """
    from sqlalchemy import delete

    from horus.db.models import Incident
    from horus.detect.run import run_detectors
    from horus.tracks.incremental import refresh_recent_incidents

    with _session() as s:
        t0 = datetime(2026, 7, 23, 11, 50, tzinfo=UTC)
        s.add(Aircraft(icao24="f00bad"))
        s.add(Aircraft(icao24="e00001"))
        for k in range(20):
            s.add(  # teleporting identity: every consecutive fix is impossible
                Position(
                    icao24="f00bad",
                    ts=t0 + timedelta(minutes=k),
                    lat=1.2 if k % 2 == 0 else 3.9,
                    lon=103.6 if k % 2 == 0 else 104.4,
                    alt_baro_ft=35000.0,
                    region="r",
                )
            )
            s.add(  # sustained low, level flight inside the Riau border box
                Position(
                    icao24="e00001",
                    ts=t0 + timedelta(minutes=k),
                    lat=0.9,
                    lon=104.2,
                    alt_baro_ft=1800.0,
                    baro_rate_fpm=0.0,
                    region="r",
                )
            )
        s.commit()

        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        run_detectors(s, "r")
        s.commit()
        full = sorted(i.incident_id for i in s.scalars(select(Incident)))
        s.execute(delete(Incident))
        s.commit()

        # Shrinking windows are the adversarial order: each sweeps the boundary back across
        # the still-open events.
        anchor = t0 + timedelta(minutes=25)
        for minutes_back in (10_000, 60, 30, 20, 10, 5):
            since = (anchor - timedelta(minutes=minutes_back)).replace(tzinfo=None)
            refresh_recent_incidents(s, since=since, region="r", settings=settings)
            s.commit()  # an unstable id would raise IntegrityError here

        incremental = sorted(i.incident_id for i in s.scalars(select(Incident)))
        assert incremental == full, "the incremental lane must match a full rebuild exactly"


def test_refresh_survives_a_bucket_straddling_its_boundary() -> None:
    """A jamming bucket straddling `since` must not collide with its stored incident.

    Reproduced before the fix: the stored incident starts before an unaligned `since`
    (so the delete misses it) while the scoped detector regenerates the same epoch-anchored
    bucket id from the partial evidence — IntegrityError on commit. `since` is now snapped
    down to the bucket boundary, which both covers the delete and keeps any re-scored
    bucket fully visible rather than partially.
    """
    from horus.tracks.incremental import refresh_recent_incidents

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    bucket_start = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    with Session(engine) as s:
        # One bucket [T, T+10): six aircraft, four in hard loss — a firing cell.
        for k in range(6):
            icao = f"aa{k:04x}"
            s.add(Aircraft(icao24=icao))
            for minute in (1, 4, 8):
                s.add(
                    Position(
                        icao24=icao,
                        ts=bucket_start + timedelta(minutes=minute),
                        lat=54.9,
                        lon=20.5,
                        alt_baro_ft=35000.0,
                        nic=0 if k < 4 else 8,
                        nac_p=0 if k < 4 else 9,
                        region="baltic",
                    )
                )
        s.commit()
        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        full = refresh_recent_incidents(
            s,
            since=bucket_start.replace(tzinfo=None) - timedelta(days=1),
            region="baltic",
            settings=settings,
        )
        s.commit()
        assert full == 1

        # since falls MID-bucket — the straddle. Must re-derive cleanly, not collide.
        again = refresh_recent_incidents(
            s,
            since=bucket_start.replace(tzinfo=None) + timedelta(minutes=5),
            region="baltic",
            settings=settings,
        )
        s.commit()  # raised IntegrityError before the alignment fix
        assert again == 1  # same bucket, fully re-derived, not duplicated
