"""Positive/negative control evaluation for the GNSS-interference detector.

    python -m scripts.eval_control \
        --positive data/baltic-control.db --negative data/sg-live.db

The honest-evaluation problem with a jamming detector is that a quiet sky proves nothing:
zero incidents over Singapore is equally consistent with "the detector works and there is
no interference" and "the detector never fires". The answer is a control pair — run the
*same* detector with the *same* parameters over a region with documented, ongoing GNSS
interference and over a clean one, and report both.

Neither side is a labelled ground truth: the positive region has no per-cell truth mask,
so this yields a *contrast*, not precision/recall against labels. That is stated in the
output rather than dressed up.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from horus.db.models import Position
from horus.detect.jamming import detect_jamming
from horus.logging import configure_logging
from horus.timeutil import utc_naive


@dataclass
class LaneResult:
    label: str
    reports: int
    aircraft: int
    span_minutes: float
    degraded_reports: int
    hard_loss_reports: int
    cells_seen: int
    cells_unscoreable: int
    incidents: int
    top_fraction: float
    nic_histogram: dict[int, int]

    @property
    def degraded_pct(self) -> float:
        return 100.0 * self.degraded_reports / self.reports if self.reports else 0.0

    @property
    def unscoreable_pct(self) -> float:
        return 100.0 * self.cells_unscoreable / self.cells_seen if self.cells_seen else 0.0


def lane_span(db_url: str) -> tuple[datetime, datetime] | None:
    """Wall-clock span of a lane's NIC-bearing reports."""
    engine = create_engine(db_url)
    with Session(engine) as s:
        lo = s.scalar(select(func.min(Position.ts)).where(Position.nic.is_not(None)))
        hi = s.scalar(select(func.max(Position.ts)).where(Position.nic.is_not(None)))
    return (utc_naive(lo), utc_naive(hi)) if lo and hi else None


def run_lane(
    db_url: str,
    label: str,
    *,
    bad_nic_max: int = 5,
    window: tuple[datetime, datetime] | None = None,
) -> LaneResult:
    engine = create_engine(db_url)
    with Session(engine) as s:
        q = select(Position).where(Position.nic.is_not(None))
        if window is not None:
            q = q.where(Position.ts >= window[0], Position.ts <= window[1])
        positions = list(s.scalars(q))
        reports = len(positions)
        aircraft = len({p.icao24 for p in positions})
        ts = [p.ts for p in positions]
        span = (max(ts) - min(ts)).total_seconds() / 60.0 if ts else 0.0
        degraded = sum(1 for p in positions if p.nic is not None and p.nic <= bad_nic_max)
        hard = sum(
            1
            for p in positions
            if p.nic is not None and p.nic == 0 and (p.nac_p is None or p.nac_p == 0)
        )
        hist = dict(sorted(Counter(p.nic for p in positions if p.nic is not None).items()))
        incidents, stats = detect_jamming(s)  # detector sees the lane's own DB
        top = (
            max((i.evidence or {}).get("degraded_fraction", 0.0) for i in incidents)
            if incidents
            else 0.0
        )
        total_pos = s.scalar(select(func.count()).select_from(Position)) or 0
        assert total_pos >= reports
        return LaneResult(
            label=label,
            reports=reports,
            aircraft=aircraft,
            span_minutes=span,
            degraded_reports=degraded,
            hard_loss_reports=hard,
            cells_seen=stats.cells_seen,
            cells_unscoreable=stats.cells_unscoreable,
            incidents=stats.incidents,
            top_fraction=float(top),
            nic_histogram=hist,
        )


def _row(name: str, pos: Any, neg: Any) -> str:
    return f"| {name} | {pos} | {neg} |"


def report(positive: LaneResult, negative: LaneResult) -> str:
    lines = [
        f"| Measure | {positive.label} (positive control) | {negative.label} (negative control) |",
        "|---|---:|---:|",
        _row(
            "Collection span (min)", f"{positive.span_minutes:.1f}", f"{negative.span_minutes:.1f}"
        ),
        _row("Reports with NIC", f"{positive.reports:,}", f"{negative.reports:,}"),
        _row("Distinct aircraft", f"{positive.aircraft:,}", f"{negative.aircraft:,}"),
        _row(
            "Reports degraded (NIC ≤ 5)",
            f"{positive.degraded_reports:,} ({positive.degraded_pct:.1f}%)",
            f"{negative.degraded_reports:,} ({negative.degraded_pct:.1f}%)",
        ),
        _row(
            "Reports in hard loss (NIC 0 + NACp 0)",
            f"{positive.hard_loss_reports:,}",
            f"{negative.hard_loss_reports:,}",
        ),
        _row("Cells observed", positive.cells_seen, negative.cells_seen),
        _row(
            "Cells unscoreable",
            f"{positive.cells_unscoreable} ({positive.unscoreable_pct:.1f}%)",
            f"{negative.cells_unscoreable} ({negative.unscoreable_pct:.1f}%)",
        ),
        _row("**Incidents raised**", f"**{positive.incidents}**", f"**{negative.incidents}**"),
        _row(
            "Highest cell degraded-fraction",
            f"{positive.top_fraction:.2f}",
            f"{negative.top_fraction:.2f}",
        ),
    ]
    return "\n".join(lines)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Controlled jamming-detector evaluation")
    parser.add_argument("--positive", required=True, help="SQLite path for the interference region")
    parser.add_argument("--negative", required=True, help="SQLite path for the clean region")
    parser.add_argument("--positive-label", default="Baltic")
    parser.add_argument("--negative-label", default="Singapore")
    parser.add_argument(
        "--overlap",
        action="store_true",
        help="restrict both lanes to the wall-clock window they were both collecting",
    )
    args = parser.parse_args()

    pos_url = f"sqlite:///{args.positive}"
    neg_url = f"sqlite:///{args.negative}"
    window = None
    if args.overlap:
        # The strongest available comparison: same interval, same detector, different sky.
        spans = [lane_span(pos_url), lane_span(neg_url)]
        if all(sp is not None for sp in spans):
            lo = max(sp[0] for sp in spans if sp)
            hi = min(sp[1] for sp in spans if sp)
            if lo < hi:
                window = (lo, hi)
                print(f"\nrestricted to the overlapping window {lo} -> {hi}")
            else:
                print("\nlanes do not overlap in time; reporting full spans")

    pos = run_lane(pos_url, args.positive_label, window=window)
    neg = run_lane(neg_url, args.negative_label, window=window)
    print()
    print(report(pos, neg))
    print()
    print(f"{args.positive_label} NIC histogram: {pos.nic_histogram}")
    print(f"{args.negative_label} NIC histogram: {neg.nic_histogram}")
    print()
    print(
        "Contrast, not precision: the positive region has no per-cell ground-truth mask, so\n"
        "this shows the detector separating a known-interference sky from a clean one under\n"
        "identical parameters — it does not license a precision or recall figure."
    )


if __name__ == "__main__":
    main()
