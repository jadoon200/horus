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
