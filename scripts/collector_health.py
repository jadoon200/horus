"""One-shot health glance at the continuous live lane.

    HORUS_DATABASE_URL=sqlite:///data/sg-live.db python -m scripts.collector_health

Prints what actually matters day to day on a laptop-hosted collector: whether it is still
receiving, how much sky is missing (open outages), the observed rate, and the database
footprint against the retention budget. Read-only — safe to run any time.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from horus.db.base import session_scope
from horus.db.models import Aircraft, CollectorRun, CoverageOutage, Position
from horus.ingest.retention import database_bytes
from horus.logging import configure_logging
from horus.timeutil import utc_naive


def main() -> None:
    configure_logging()
    with session_scope() as s:
        positions = s.scalar(select(func.count()).select_from(Position)) or 0
        aircraft = s.scalar(select(func.count()).select_from(Aircraft)) or 0
        first_ts = s.scalar(select(func.min(Position.ts)))
        last_ts = s.scalar(select(func.max(Position.ts)))
        runs = list(s.scalars(select(CollectorRun).order_by(CollectorRun.id.desc()).limit(3)))
        open_outages = list(
            s.scalars(select(CoverageOutage).where(CoverageOutage.closed_at.is_(None)))
        )
        outage_total = s.scalar(select(func.count()).select_from(CoverageOutage)) or 0
        size_mb = database_bytes(s) / 1_048_576

        print(f"positions      {positions:,}")
        print(f"aircraft       {aircraft:,}")
        if first_ts and last_ts:
            span_h = (utc_naive(last_ts) - utc_naive(first_ts)).total_seconds() / 3600.0
            print(f"window         {utc_naive(first_ts)} -> {utc_naive(last_ts)}  ({span_h:.1f} h)")
            if span_h > 0:
                print(f"rate           {positions / span_h:,.0f} reports/h")
            age_min = (
                datetime.now(UTC) - utc_naive(last_ts).replace(tzinfo=UTC)
            ).total_seconds() / 60
            state = "RECEIVING" if age_min < 5 else "STALE"
            print(f"freshness      last report {age_min:.1f} min ago  [{state}]")
        print(f"database       {size_mb:,.1f} MB")
        print(f"outages        {outage_total} total, {len(open_outages)} OPEN")
        for o in open_outages:
            print(f"  ! open since {utc_naive(o.opened_at)}  ({o.reason})")
        print("recent runs:")
        for r in runs:
            print(
                f"  #{r.id}  {r.status:<8} started {utc_naive(r.started_at)}  "
                f"reports={r.report_count}  stop={r.stop_reason or '-'}"
            )


if __name__ == "__main__":
    main()
