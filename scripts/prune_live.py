"""Retention pass over the continuous live database.

    HORUS_DATABASE_URL=sqlite:///data/sg-live.db python -m scripts.prune_live [--days N]

Deletes positions older than the window, commits, then checkpoints the WAL so the freed
pages are actually reclaimed. Tracks and incidents are never pruned — they are the
analytic product and are tiny next to the raw position stream.
"""

from __future__ import annotations

import argparse

from horus.db.base import session_scope
from horus.ingest.retention import checkpoint_wal, database_bytes, prune_positions
from horus.logging import configure_logging, get_logger

log = get_logger(__name__)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Prune old positions from the live DB")
    parser.add_argument("--days", type=float, default=21.0, help="retention window in days")
    args = parser.parse_args()
    with session_scope() as session:
        before_mb = database_bytes(session) / 1_048_576
        removed = prune_positions(session, retain_days=args.days)
        session.commit()
        checkpointed = checkpoint_wal(session)
        after_mb = database_bytes(session) / 1_048_576
    log.info(
        "prune_complete",
        removed=removed,
        checkpointed=checkpointed,
        before_mb=round(before_mb, 1),
        after_mb=round(after_mb, 1),
    )


if __name__ == "__main__":
    main()
