"""Diurnal evaluation over the continuous Singapore lane — read-only.

    HORUS_DATABASE_URL=sqlite:///data/sg-live.db python -m scripts.eval_multiday

The first 12.5-minute window bounded false positives but said nothing about behaviour over
a day: traffic density — and therefore the unscoreable fraction — swings with the clock, and
a single afternoon's 24%-unscoreable figure is not a standing property. This walks the
collected span hour by hour and reports, per hour: reports, aircraft, the scoreable/
unscoreable split, and incidents by detector.

Honesty constraints baked in:
- **Read-only.** The deterministic detectors are called directly (they return lists; no DB
  mutation), so running the eval never rewrites the live picture.
- **Outages are shown, not spanned.** Ledgered collector downtime (host sleep) is printed as
  its own rows; an hour that is mostly outage is flagged, because a low incident count there
  is our silence, not a quiet sky.
- **Cycle readiness is measured.** Before 48 hours the output stays a dated hour-by-hour
  trace. At 48 hours it becomes a Singapore-clock cross-day mean ± population spread, with
  the sample count visible and partial/outage-distorted buckets excluded.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import fmean, pstdev

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from horus.db.base import session_scope
from horus.db.models import CollectorRun, CoverageOutage, Incident, Position
from horus.detect.gaps import detect_gaps
from horus.detect.incursion import detect_incursions
from horus.detect.jamming import detect_jamming
from horus.detect.spoof import detect_spoof
from horus.logging import configure_logging, get_logger
from horus.timeutil import utc_naive
from horus.tracks.build import build_tracks

log = get_logger(__name__)

_EVAL_MD = Path(__file__).resolve().parents[1] / "docs" / "EVAL.md"
_DETECTORS = ("jamming", "gap", "incursion", "spoof")
_SINGAPORE_OFFSET = timedelta(hours=8)


@dataclass
class HourStats:
    hour: datetime  # start of the UTC hour bucket
    reports: int = 0
    aircraft: int = 0
    cell_windows: int = 0
    unscoreable: int = 0
    incidents: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_DETECTORS, 0))
    outage_minutes: float = 0.0

    @property
    def unscoreable_pct(self) -> float:
        return 100.0 * self.unscoreable / self.cell_windows if self.cell_windows else 0.0


@dataclass(frozen=True)
class ClockHourStats:
    """Cross-day distribution for one Singapore local clock hour."""

    local_hour: int
    samples: int
    reports_mean: float
    reports_spread: float
    unscoreable_mean: float
    unscoreable_spread: float


def _hour_floor(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


def _hour_ceil(ts: datetime) -> datetime:
    floor = _hour_floor(ts)
    return floor if ts == floor else floor + timedelta(hours=1)


def merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Return the union of possibly overlapping closed-open time ranges."""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def outage_intervals(
    session: Session,
    region: str,
    start: datetime,
    end: datetime,
) -> tuple[list[tuple[datetime, datetime]], int]:
    """Return region-scoped, clipped outage union and the source-ledger row count."""
    outages = list(
        session.scalars(
            select(CoverageOutage)
            .join(CollectorRun, CoverageOutage.run_id == CollectorRun.id)
            .where(CollectorRun.region == region)
        )
    )
    clipped: list[tuple[datetime, datetime]] = []
    overlapping_count = 0
    for outage in outages:
        opened_at = max(utc_naive(outage.opened_at), start)
        closed_at = min(
            utc_naive(outage.closed_at) if outage.closed_at else end,
            end,
        )
        if closed_at > opened_at:
            overlapping_count += 1
            clipped.append((opened_at, closed_at))
    return merge_intervals(clipped), overlapping_count


def evaluate(
    session: Session, region: str
) -> tuple[list[HourStats], int, datetime | None, datetime | None]:
    lo = session.scalar(select(func.min(Position.ts)).where(Position.region == region))
    hi = session.scalar(select(func.max(Position.ts)).where(Position.region == region))
    if lo is None or hi is None:
        return [], 0, None, None
    lo, hi = utc_naive(lo), utc_naive(hi)

    # Detectors are called directly — they return lists and never touch the DB — so the eval
    # is read-only. Tracks must exist for downstream consumers, but the deterministic
    # detectors here read positions, so no rebuild is forced on the live DB.
    all_incidents: list[Incident] = []
    all_incidents += detect_jamming(session, region)[0]
    all_incidents += detect_gaps(session, region)
    all_incidents += detect_incursions(session, region)
    all_incidents += detect_spoof(session, region)

    hours: dict[datetime, HourStats] = {}
    h = _hour_floor(lo)
    while h <= hi:
        hours[h] = HourStats(hour=h)
        h += timedelta(hours=1)

    # Per-hour reports + aircraft.
    seen_aircraft: dict[datetime, set[str]] = defaultdict(set)
    for p in session.scalars(select(Position).where(Position.region == region)):
        key = _hour_floor(utc_naive(p.ts))
        if key in hours:
            hours[key].reports += 1
            seen_aircraft[key].add(p.icao24)
    for key, aircraft in seen_aircraft.items():
        hours[key].aircraft = len(aircraft)

    # Per-hour scoreability: bound the detector to each hour and take its cell tallies.
    for key, hs in hours.items():
        _, stats = detect_jamming(session, region, since=key, until=key + timedelta(hours=1))
        hs.cell_windows = stats.cells_seen
        hs.unscoreable = stats.cells_unscoreable

    # Bucket incidents by the hour they start.
    for inc in all_incidents:
        key = _hour_floor(utc_naive(inc.ts_start))
        if key in hours and inc.detector in hours[key].incidents:
            hours[key].incidents[inc.detector] += 1

    # Attribute ledgered outage minutes to the hours they fall in.
    merged_outages, outage_count = outage_intervals(session, region, lo, hi)
    for start, end in merged_outages:
        cur = _hour_floor(start)
        while cur <= end:
            nxt = cur + timedelta(hours=1)
            overlap = (min(end, nxt) - max(start, cur)).total_seconds() / 60.0
            if cur in hours and overlap > 0:
                hours[cur].outage_minutes += overlap
            cur = nxt

    return [hours[k] for k in sorted(hours)], outage_count, lo, hi


def aggregate_clock_hours(
    rows: list[HourStats], start: datetime, end: datetime
) -> list[ClockHourStats]:
    """Aggregate complete, non-outage UTC buckets by Singapore local clock hour.

    The first and last buckets are often partial because collection began at an arbitrary
    minute. Hours with at least five minutes of ledgered outage are also removed: averaging
    collector silence into a traffic trough would make the diurnal curve look cleaner than
    the evidence permits.
    """
    complete_start = _hour_ceil(start)
    by_hour: dict[int, list[HourStats]] = defaultdict(list)
    for row in rows:
        if row.hour < complete_start or row.hour + timedelta(hours=1) > end:
            continue
        if row.outage_minutes >= 5 or row.cell_windows < 5:
            continue
        local_hour = (row.hour + _SINGAPORE_OFFSET).hour
        by_hour[local_hour].append(row)

    result: list[ClockHourStats] = []
    for local_hour in sorted(by_hour):
        samples = by_hour[local_hour]
        reports = [float(row.reports) for row in samples]
        unscoreable = [row.unscoreable_pct for row in samples]
        result.append(
            ClockHourStats(
                local_hour=local_hour,
                samples=len(samples),
                reports_mean=fmean(reports),
                reports_spread=pstdev(reports),
                unscoreable_mean=fmean(unscoreable),
                unscoreable_spread=pstdev(unscoreable),
            )
        )
    return result


def _detector_mix(rows: list[HourStats], span_h: float) -> list[str]:
    totals = {d: sum(r.incidents[d] for r in rows) for d in _DETECTORS}
    top = max(totals, key=lambda d: totals[d])
    if totals[top] == 0:
        return []
    days = max(span_h / 24.0, 1 / 24)
    return [
        "",
        "**Detector mix over the span:** "
        + ", ".join(f"{d} {totals[d]}" for d in _DETECTORS)
        + ". Review-call rates per 24 h: "
        + ", ".join(f"{d} {totals[d] / days:.1f}" for d in _DETECTORS)
        + f". The **{top}** detector is the largest class. These are review calls, not "
        "confirmed hostile events; detector-specific plausibility audits elsewhere in this "
        "report decide whether a class is over-firing.",
    ]


def render(
    rows: list[HourStats],
    outage_count: int,
    start: datetime,
    end: datetime,
) -> str:
    span_h = (end - start).total_seconds() / 3600.0
    # Sum the per-hour attributed minutes rather than re-reading the ORM outage rows: the
    # eval session is rolled back and closed by the time we render, so those objects are
    # detached — the plain floats on each HourStats are the durable record.
    total_out = sum(r.outage_minutes for r in rows)
    aggregates = aggregate_clock_hours(rows, start, end)
    multi_cycle = span_h >= 48 and len(aggregates) == 24
    missing_clock_hours = sorted(set(range(24)) - {row.local_hour for row in aggregates})
    lines = [
        "## Diurnal behaviour over the continuous Singapore lane",
        "",
        f"A continuous span of **{span_h:.1f} h** from `{start.isoformat()}` to "
        f"`{end.isoformat()}`.",
        "",
        f"**Coverage honesty:** {outage_count} ledgered outages totalling ~{total_out:.0f} min "
        "(host sleep). Buckets with ≥5 outage minutes and the partial first/last UTC hours are "
        "excluded from the cross-day curve; a low count during our silence is not a quiet sky.",
    ]

    if multi_cycle:
        lines += [
            "",
            "This crosses the **48-hour / two-cycle bar**. The table is a cross-day "
            "Singapore-clock mean ± population standard deviation (SD), not the earlier "
            "single partial-cycle trace. `n` is visible because outage-affected clock hours "
            "can still have only one clean observation.",
            "",
            "| SGT hour | Clean days (n) | Reports, mean ± SD | Unscoreable, mean ± SD |",
            "|---|---:|---:|---:|",
        ]
        for row in aggregates:
            lines.append(
                f"| {row.local_hour:02d}:00 | {row.samples} | "
                f"{row.reports_mean:,.0f} ± {row.reports_spread:,.0f} | "
                f"{row.unscoreable_mean:.1f}% ± {row.unscoreable_spread:.1f} pp |"
            )
        busiest = max(aggregates, key=lambda row: row.reports_mean)
        quietest = min(aggregates, key=lambda row: row.reports_mean)
        lines += [
            "",
            f"**The prediction the data can check:** thinner traffic → more unscoreable sky. "
            f"The cross-day busiest clock hour was {busiest.local_hour:02d}:00 SGT "
            f"({busiest.reports_mean:,.0f} reports/h), averaging "
            f"{busiest.unscoreable_mean:.1f}% unscoreable; the quietest was "
            f"{quietest.local_hour:02d}:00 SGT ({quietest.reports_mean:,.0f} reports/h), "
            f"averaging {quietest.unscoreable_mean:.1f}%. "
            + (
                "The traffic trough remains blinder across days."
                if quietest.unscoreable_mean >= busiest.unscoreable_mean
                else "The relationship does not hold across days — recorded, not smoothed."
            ),
        ]
    else:
        if span_h >= 48:
            readiness = (
                "The raw span has crossed **48 hours**, but the clean two-cycle coverage "
                "bar is not yet met. Every Singapore clock hour needs at least one complete "
                "bucket with <5 outage minutes and enough cell windows; missing clean hours: "
                + ", ".join(f"{hour:02d}:00" for hour in missing_clock_hours)
                + ". The dated trace remains visible and no cross-day stability claim is made."
            )
        else:
            readiness = (
                "This has **not** crossed the 48-hour / two-cycle bar. The dated trace below "
                "is suggestive only; no cross-day stability claim is made."
            )
        lines += [
            "",
            readiness,
            "",
            "| UTC date/hour | Reports | Aircraft | Unscoreable | jam | gap | incur | spoof | "
            "Outage |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for row in rows:
            flag = " `*`" if row.outage_minutes >= 5 else ""
            lines.append(
                f"| {row.hour:%Y-%m-%d %H:%M} | {row.reports:,} | {row.aircraft} | "
                f"{row.unscoreable}/{row.cell_windows} ({row.unscoreable_pct:.0f}%) | "
                f"{row.incidents['jamming']} | {row.incidents['gap']} | "
                f"{row.incidents['incursion']} | {row.incidents['spoof']} | "
                f"{row.outage_minutes:.0f}m{flag} |"
            )

    lines += _detector_mix(rows, span_h)
    return "\n".join(lines)


def write_eval_md(block: str) -> None:
    marker = "## Diurnal behaviour over the continuous Singapore lane"
    text = _EVAL_MD.read_text() if _EVAL_MD.exists() else "# HORUS evaluation\n\n"
    if marker in text:
        head, _, rest = text.partition(marker)
        # Drop the old block up to the next H2.
        after = rest.split("\n## ", 1)
        tail = ("\n## " + after[1]) if len(after) > 1 else "\n"
        text = head + block + tail
    else:
        anchor = "## Real ADS-B"
        text = (
            text.replace(anchor, block + "\n\n" + anchor, 1)
            if anchor in text
            else text + "\n\n" + block + "\n"
        )
    _EVAL_MD.write_text(text)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Diurnal evaluation over the live lane")
    parser.add_argument("--region", default="sg-live")
    parser.add_argument("--write", action="store_true", help="update docs/EVAL.md")
    args = parser.parse_args()

    with session_scope() as session:
        # Ensure tracks exist for the region (read path expects them); harmless if present.
        if not session.scalar(select(func.count()).select_from(Position)):
            print("no data")
            return
        build_tracks(session, args.region)
        session.flush()
        rows, outage_count, start, end = evaluate(session, args.region)
        session.rollback()  # read-only: never persist the track rebuild or anything else

    if not rows or start is None or end is None:
        print("no data for region", args.region)
        return
    block = render(rows, outage_count, start, end)
    print("\n" + block)
    if args.write:
        write_eval_md(block)
        log.info("eval_multiday_written", hours=len(rows))


if __name__ == "__main__":
    main()
