"""Collector ledger + retention: our blind spots must never look like aircraft behaviour.

During a laptop sleep every aircraft stops reporting and reappears far away — exactly the
dark-aircraft signature. Unless the downtime is ledgered as a coverage outage, a multi-day
run would manufacture a fleet of false dark aircraft on every wake. These tests pin that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from horus.db.base import Base
from horus.db.models import Aircraft, CollectorRun, CoverageOutage, Position
from horus.detect.gaps import detect_gaps
from horus.ingest.collect import (
    bridge_downtime,
    bridge_previous_run,
    pause_threshold_seconds,
)
from horus.ingest.retention import checkpoint_wal, database_bytes, prune_positions

T0 = datetime(2026, 7, 23, 0, 0, tzinfo=UTC)


def _session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_pause_threshold_scales_with_poll_interval() -> None:
    # Never below the floor, but a slow poll must not be mistaken for a pause.
    assert pause_threshold_seconds(30.0) == 120.0
    assert pause_threshold_seconds(60.0) == 240.0


def test_previous_run_downtime_is_bridged() -> None:
    with _session() as s:
        old = CollectorRun(
            started_at=T0, last_message_at=T0 + timedelta(minutes=5), status="stopped"
        )
        s.add(old)
        new = CollectorRun(started_at=datetime.now(UTC), status="running")
        s.add(new)
        s.flush()

        assert bridge_previous_run(s, new.id, poll_seconds=30.0) is True
        outage = s.scalars(select(CoverageOutage)).one()
        assert outage.reason == "collector offline between runs"
        # Closed on creation: it covers a known-finished interval, not an ongoing fault.
        assert outage.closed_at is not None
        assert outage.run_id == new.id


def test_quick_restart_is_not_an_outage() -> None:
    with _session() as s:
        now = datetime.now(UTC)
        s.add(CollectorRun(started_at=now, last_message_at=now, status="stopped"))
        new = CollectorRun(started_at=now, status="running")
        s.add(new)
        s.flush()
        assert bridge_previous_run(s, new.id, poll_seconds=30.0) is False
        assert s.scalar(select(func.count()).select_from(CoverageOutage)) == 0


def test_previous_run_bridge_is_scoped_to_the_same_region() -> None:
    with _session() as s:
        old_sg = CollectorRun(
            started_at=T0,
            last_message_at=T0 + timedelta(minutes=5),
            stopped_at=T0 + timedelta(minutes=5),
            status="stopped",
            region="sg",
        )
        newer_baltic = CollectorRun(
            started_at=T0 + timedelta(minutes=6),
            last_message_at=T0 + timedelta(minutes=7),
            stopped_at=T0 + timedelta(minutes=7),
            status="stopped",
            region="baltic",
        )
        current_sg = CollectorRun(started_at=datetime.now(UTC), status="running", region="sg")
        s.add_all([old_sg, newer_baltic, current_sg])
        s.flush()

        assert bridge_previous_run(s, current_sg.id, poll_seconds=30.0) is True
        outage = s.scalars(select(CoverageOutage)).one()
        assert outage.opened_at == old_sg.last_message_at.replace(tzinfo=None)


def test_bridged_sleep_suppresses_the_false_dark_aircraft() -> None:
    """The whole point: a host sleep must not become a dark-ship-style incident."""
    with _session() as s:
        s.add(Aircraft(icao24="abc123"))
        run = CollectorRun(started_at=T0, status="running")
        s.add(run)
        s.flush()
        # Seen at altitude, then a 3-hour silence, reappearing far away — the signature.
        s.add(
            Position(icao24="abc123", ts=T0, lat=1.30, lon=103.8, alt_baro_ft=35000.0, region="sg")
        )
        s.add(
            Position(
                icao24="abc123",
                ts=T0 + timedelta(hours=3),
                lat=3.60,
                lon=104.9,
                alt_baro_ft=35000.0,
                region="sg",
            )
        )
        s.commit()

        # Without the ledger the detector calls it dark...
        assert len(detect_gaps(s, "sg")) == 1

        # ...with the downtime recorded, the call is correctly suppressed.
        bridge_downtime(
            s,
            run.id,
            since=T0 + timedelta(minutes=1),
            until=T0 + timedelta(hours=2, minutes=59),
            reason="collector paused (host sleep or stall)",
        )
        s.commit()
        assert detect_gaps(s, "sg") == []


def test_retention_prunes_old_positions_but_respects_the_floor() -> None:
    with _session() as s:
        s.add(Aircraft(icao24="abc123"))
        now = datetime.now(UTC)
        for days_ago in (40, 30, 10, 1):
            s.add(
                Position(
                    icao24="abc123",
                    ts=now - timedelta(days=days_ago),
                    lat=1.3,
                    lon=103.8,
                    region="sg",
                )
            )
        s.commit()

        # A pilot-start floor 35 days back must PRESERVE everything newer than it, even
        # though the 21-day retention window would otherwise delete the 30-day row. The
        # floor pulls the cutoff back; it never pushes it forward.
        removed = prune_positions(s, retain_days=21, floor_ts=now - timedelta(days=35))
        s.commit()
        assert removed == 1, "only the row older than the floor may go"
        assert s.scalar(select(func.count()).select_from(Position)) == 3

        # Without a floor, the plain retention window applies and the 30-day row goes too.
        removed = prune_positions(s, retain_days=21)
        s.commit()
        assert removed == 1
        assert s.scalar(select(func.count()).select_from(Position)) == 2
        # Checkpointing belongs AFTER the commit; inside the pruning transaction it would
        # block on its own uncommitted pages.
        assert checkpoint_wal(s) is True
        assert database_bytes(s) > 0


def _cruise(
    icao: str, alt: float, rate: float | None, gs: float | None, lat: float, lon: float, mins: float
):
    from horus.db.models import Position

    return Position(
        icao24=icao,
        ts=T0 + timedelta(minutes=mins),
        lat=lat,
        lon=lon,
        alt_baro_ft=alt,
        baro_rate_fpm=rate,
        gs_kt=gs,
        region="sg",
    )


def test_descending_aircraft_is_not_called_dark() -> None:
    """A landing is not a disappearance.

    Measured on 11.7 h of real Singapore traffic: 25 of 34 gap calls were descending at the
    moment of silence — aircraft on approach that landed, sat, and departed hours later,
    reappearing displaced. That is the dark signature with a completely benign cause.
    """
    from horus.config import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    with _session() as s:
        s.add(Aircraft(icao24="desc01"))
        s.add(Aircraft(icao24="cruz01"))
        # Same silence and displacement; only the vertical rate differs. The gap is kept
        # short enough that the coverage-exit rule cannot fire, so this isolates descent.
        s.add(_cruise("desc01", 25000, -1800, 400, 1.30, 103.8, 0))
        s.add(_cruise("desc01", 25000, None, 400, 1.90, 104.4, 30))
        s.add(_cruise("cruz01", 35000, 0, 460, 1.30, 103.8, 0))
        s.add(_cruise("cruz01", 35000, None, 460, 1.90, 104.4, 30))
        s.commit()

        flagged = {i.icao24 for i in detect_gaps(s, "sg")}
        assert "cruz01" in flagged, "a level-flight aircraft going silent is the real signature"
        assert "desc01" not in flagged, "a descending aircraft was landing, not going dark"
        assert settings.gap_max_descent_fpm < 0


def test_aircraft_that_could_have_left_the_circle_is_not_called_dark() -> None:
    """The collector watches a finite circle; leaving it is our boundary, not evasion.

    An aircraft that departs, crosses the collection boundary and returns hours later
    reappears displaced after a long silence. Geometry fully explains that, so the call is
    not attributable — the air-domain analogue of the maritime coverage model.
    """
    with _session() as s:
        s.add(Aircraft(icao24="longx1"))
        s.add(Aircraft(icao24="shortx"))
        # Near the centre at cruise speed: 8 hours is ample to exit 250 nm and come back.
        s.add(_cruise("longx1", 35000, 0, 460, 1.35, 103.82, 0))
        s.add(_cruise("longx1", 35000, None, 460, 1.95, 104.4, 480))
        # Same aircraft, same place, but only 11 minutes silent — no time to leave.
        s.add(_cruise("shortx", 35000, 0, 460, 1.35, 103.82, 0))
        s.add(_cruise("shortx", 35000, None, 460, 1.95, 104.4, 11))
        s.commit()

        flagged = {i.icao24 for i in detect_gaps(s, "sg")}
        assert "shortx" in flagged, "too brief to have left coverage — a real gap"
        assert "longx1" not in flagged, "had time to exit the circle and return"


def test_gap_uses_the_boundary_recorded_for_a_non_default_region() -> None:
    """Baltic reports must use the Baltic run circle, never Singapore's config circle."""
    with _session() as s:
        s.add(Aircraft(icao24="balt01"))
        s.add(
            Position(
                icao24="balt01",
                ts=T0,
                lat=54.9,
                lon=20.5,
                alt_baro_ft=35_000,
                baro_rate_fpm=0,
                gs_kt=450,
                region="baltic",
            )
        )
        s.add(
            Position(
                icao24="balt01",
                ts=T0 + timedelta(hours=8),
                lat=55.5,
                lon=21.4,
                alt_baro_ft=35_000,
                gs_kt=450,
                region="baltic",
            )
        )
        s.commit()

        # With no historical provenance, the Singapore fallback is inapplicable and fails
        # open: the call stays for review.
        assert len(detect_gaps(s, "baltic")) == 1

        s.add(
            CollectorRun(
                started_at=T0 - timedelta(minutes=1),
                last_message_at=T0 + timedelta(hours=8, minutes=1),
                stopped_at=T0 + timedelta(hours=8, minutes=1),
                status="stopped",
                region="baltic",
                center_lat=54.9,
                center_lon=20.5,
                radius_nm=250,
            )
        )
        s.commit()

        # Eight hours was ample to leave this recorded circle and return. The silence is
        # collection geometry, not attributable aircraft behaviour.
        assert detect_gaps(s, "baltic") == []
