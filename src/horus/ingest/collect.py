"""Continuous ADS-B collector — poll adsb.lol into the database.

    python -m horus.ingest.collect [--cycles N] [--region LABEL]

Every cycle fetches one point-query snapshot and persists the fresh samples. The run is
ledgered in `collector_runs`; consecutive fetch failures open a `coverage_outages` row so
downstream gap detection never mistakes *our* outage for an aircraft going dark (the
lesson inherited from the PHAROS pilot ledger).
"""

from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from horus.config import get_settings
from horus.db.base import session_scope
from horus.db.models import CollectorRun, CoverageOutage
from horus.ingest.adsb import fetch_point
from horus.ingest.persist import persist_samples
from horus.logging import configure_logging, get_logger

log = get_logger(__name__)

# Fetch failures before an outage row opens (transient blips shouldn't ledger).
_OUTAGE_AFTER_FAILURES = 3


def collect_once(session: Session, *, region: str | None) -> int:
    """One poll → persist; returns inserted row count. Raises on fetch failure."""
    s = get_settings()
    samples = fetch_point()
    return persist_samples(session, samples, source=s.adsb_source_label, region=region)


def run_collector(*, cycles: int | None, region: str | None) -> None:
    """Poll until stopped (or for `cycles` polls), maintaining the run/outage ledger."""
    s = get_settings()
    with session_scope() as session:
        run = CollectorRun(started_at=datetime.now(UTC), status="running")
        session.add(run)
        session.flush()
        run_id = run.id

    failures = 0
    outage_id: int | None = None
    done = 0
    stop_reason = "cycle limit reached"
    try:
        while cycles is None or done < cycles:
            try:
                with session_scope() as session:
                    inserted = collect_once(session, region=region)
                    run_row = session.get(CollectorRun, run_id)
                    assert run_row is not None
                    run_row.last_message_at = datetime.now(UTC)
                    run_row.report_count += inserted
                    if outage_id is not None:  # feed is back — close the outage honestly
                        outage = session.get(CoverageOutage, outage_id)
                        if outage is not None:
                            outage.closed_at = datetime.now(UTC)
                        outage_id = None
                    failures = 0
            except Exception as exc:
                failures += 1
                log.warning("collect_cycle_failed", error=str(exc), consecutive=failures)
                if failures == _OUTAGE_AFTER_FAILURES:
                    with session_scope() as session:
                        outage = CoverageOutage(
                            opened_at=datetime.now(UTC),
                            reason=f"collector fetch failing ({type(exc).__name__})",
                            run_id=run_id,
                        )
                        session.add(outage)
                        session.flush()
                        outage_id = outage.id
            done += 1
            if cycles is None or done < cycles:
                time.sleep(s.adsb_poll_seconds)
    except KeyboardInterrupt:
        stop_reason = "interrupted"
    finally:
        with session_scope() as session:
            run_row = session.get(CollectorRun, run_id)
            if run_row is not None:
                run_row.stopped_at = datetime.now(UTC)
                run_row.status = "stopped"
                run_row.stop_reason = stop_reason
            if outage_id is not None:
                outage = session.get(CoverageOutage, outage_id)
                if outage is not None and outage.closed_at is None:
                    outage.closed_at = datetime.now(UTC)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Poll adsb.lol into the database")
    parser.add_argument("--cycles", type=int, default=None, help="stop after N polls")
    parser.add_argument("--region", default="sg-live", help="dataset slice label")
    args = parser.parse_args()
    run_collector(cycles=args.cycles, region=args.region)


if __name__ == "__main__":
    main()
