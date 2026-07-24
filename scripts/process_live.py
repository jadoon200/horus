"""Incremental processing worker for the continuous lane.

    HORUS_DATABASE_URL=sqlite:///data/sg-live.db python -m scripts.process_live [--once]

Keeps the air picture current without reprocessing the whole corpus every cycle. The
watermark lives in the database (as the newest position already reflected in tracks), so a
restart resumes correctly rather than either redoing everything or silently skipping the
backlog.

Run it alongside the collector; it is safe to run while collection continues, because it
only ever reads positions and rewrites derived rows.
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime

from horus.db.base import session_scope
from horus.logging import configure_logging, get_logger
from horus.tracks.incremental import process_cycle

log = get_logger(__name__)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Incremental air-picture processing")
    parser.add_argument("--region", default="sg-live")
    parser.add_argument("--interval", type=float, default=120.0, help="seconds between cycles")
    parser.add_argument("--window-hours", type=float, default=6.0, help="incident refresh window")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    args = parser.parse_args()

    # No watermark on start: the first cycle treats the corpus as dirty and catches up.
    watermark: datetime | None = None
    while True:
        started = time.monotonic()
        with session_scope() as session:
            stats, watermark = process_cycle(
                session,
                watermark=watermark,
                region=args.region,
                window_hours=args.window_hours,
            )
        log.info(
            "cycle_complete",
            seconds=round(time.monotonic() - started, 2),
            dirty=stats.dirty_aircraft,
            tracks=stats.tracks_rebuilt,
            incidents=stats.incidents_refreshed,
        )
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
