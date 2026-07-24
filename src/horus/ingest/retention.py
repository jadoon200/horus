"""Retention for the continuous live lane — keep the SQLite file bounded.

A 30-second poll over ~50 aircraft writes on the order of 100k rows/day, so an unbounded
live database grows without limit on a laptop. Positions older than the retention window
are deleted after a WAL checkpoint; tracks and incidents are NOT touched, because they are
the analytic product and are far smaller than the raw position stream.

Deliberately conservative: pruning is refused while the collector could still be mid-batch
on rows in the window, and the retention floor never crosses the pilot start so the
evaluation window stays reproducible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from horus.db.models import Position
from horus.logging import get_logger

log = get_logger(__name__)


def database_bytes(session: Session) -> int:
    """Size of the SQLite database in bytes (0 on non-SQLite backends)."""
    if session.get_bind().dialect.name != "sqlite":
        return 0
    page_count = session.scalar(text("PRAGMA page_count")) or 0
    page_size = session.scalar(text("PRAGMA page_size")) or 0
    return int(page_count) * int(page_size)


def checkpoint_wal(session: Session) -> bool:
    """Fold the WAL back into the main database so freed pages become reclaimable.

    Call this **after** committing the delete — a checkpoint inside the pruning
    transaction blocks on its own uncommitted pages. A live collector may also hold a
    read lock, which makes a checkpoint fail transiently; that is normal, so the failure
    is reported rather than raised (the next pass will reclaim).
    """
    if session.get_bind().dialect.name != "sqlite":
        return False
    try:
        session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    except OperationalError as exc:  # another connection is reading — try again next pass
        log.warning("wal_checkpoint_skipped", error=str(exc))
        return False
    return True


def prune_positions(
    session: Session, *, retain_days: float, floor_ts: datetime | None = None
) -> int:
    """Delete positions older than the retention window; returns rows removed.

    `floor_ts` is the earliest timestamp that must be preserved (e.g. a pilot start):
    nothing at or after it is ever deleted, so the evaluation window stays reproducible
    even once retention would otherwise reach into it. The floor therefore *reduces* what
    is pruned — it pulls the cutoff back, never forward.
    """
    cutoff = datetime.now(UTC) - timedelta(days=retain_days)
    if floor_ts is not None:
        cutoff = min(cutoff, floor_ts)
    before = session.scalar(select(func.count()).select_from(Position)) or 0
    session.execute(delete(Position).where(Position.ts < cutoff))
    after = session.scalar(select(func.count()).select_from(Position)) or 0
    removed = before - after
    log.info("prune_positions", cutoff=cutoff.isoformat(), removed=removed, remaining=after)
    return removed
