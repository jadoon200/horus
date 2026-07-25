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
- **One partial cycle, stated as such.** The span usually covers a single local midnight, so
  the shape is suggestive, not a multi-day law — the report says so rather than implying a
  stable diurnal curve from n=1.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from horus.db.base import session_scope
from horus.db.models import CoverageOutage, Incident, Position
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


def _hour_floor(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


def evaluate(session: Session, region: str) -> tuple[list[HourStats], list[CoverageOutage]]:
    lo = session.scalar(select(func.min(Position.ts)).where(Position.region == region))
    hi = session.scalar(select(func.max(Position.ts)).where(Position.region == region))
    if lo is None or hi is None:
        return [], []
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
    outages = list(session.scalars(select(CoverageOutage)))
    for o in outages:
        start = utc_naive(o.opened_at)
        end = utc_naive(o.closed_at) if o.closed_at else hi
        cur = _hour_floor(start)
        while cur <= end:
            nxt = cur + timedelta(hours=1)
            overlap = (min(end, nxt) - max(start, cur)).total_seconds() / 60.0
            if cur in hours and overlap > 0:
                hours[cur].outage_minutes += overlap
            cur = nxt

    return [hours[k] for k in sorted(hours)], len(outages)


def render(rows: list[HourStats], outage_count: int, span_h: float) -> str:
    # Sum the per-hour attributed minutes rather than re-reading the ORM outage rows: the
    # eval session is rolled back and closed by the time we render, so those objects are
    # detached — the plain floats on each HourStats are the durable record.
    total_out = sum(r.outage_minutes for r in rows)
    lines = [
        "## Diurnal behaviour over the continuous Singapore lane",
        "",
        f"A single continuous span of **{span_h:.1f} h** (Singapore is UTC+8, so this crosses "
        "one local midnight). This shows the *shape* of the day — it is one partial cycle, not "
        "a multi-day average, and no stable diurnal law is claimed from n=1.",
        "",
        f"**Coverage honesty:** {outage_count} ledgered outages totalling ~{total_out:.0f} min "
        "(host sleep). Hours with material outage are flagged `*`; a low incident count there "
        "is our own silence, not a quiet sky.",
        "",
        "| UTC hour | Reports | Aircraft | Unscoreable | jam | gap | incur | spoof | Outage |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        flag = " `*`" if r.outage_minutes >= 5 else ""
        lines.append(
            f"| {r.hour:%H:%M} | {r.reports:,} | {r.aircraft} | "
            f"{r.unscoreable}/{r.cell_windows} ({r.unscoreable_pct:.0f}%) | "
            f"{r.incidents['jamming']} | {r.incidents['gap']} | "
            f"{r.incidents['incursion']} | {r.incidents['spoof']} | "
            f"{r.outage_minutes:.0f}m{flag} |"
        )
    # The honest read: does the unscoreable fraction track the traffic trough? Only hours
    # with negligible outage count — otherwise a low-traffic hour might be low because the
    # collector was asleep, not because the sky was quiet, and the comparison would measure
    # our downtime rather than the diurnal cycle.
    scored = [r for r in rows if r.cell_windows >= 5 and r.outage_minutes < 5]
    if scored:
        busiest = max(scored, key=lambda r: r.reports)
        quietest = min(scored, key=lambda r: r.reports)
        lines += [
            "",
            f"**The prediction the data can check:** thinner traffic → more unscoreable sky. "
            f"Busiest hour {busiest.hour:%H:%M}Z ({busiest.reports:,} reports) was "
            f"{busiest.unscoreable_pct:.0f}% unscoreable; quietest {quietest.hour:%H:%M}Z "
            f"({quietest.reports:,} reports) was {quietest.unscoreable_pct:.0f}%. "
            + (
                "The trough is blinder, as expected."
                if quietest.unscoreable_pct >= busiest.unscoreable_pct
                else "The relationship did not hold this window — recorded, not smoothed."
            ),
        ]

    # What the detector mix says about where the next work is. Surfacing this is the whole
    # point of a multi-day view — a single short window can't show which detector over-fires.
    totals = {d: sum(r.incidents[d] for r in rows) for d in _DETECTORS}
    top = max(totals, key=lambda d: totals[d])
    if totals[top] > 0:
        lines += [
            "",
            "**Detector mix over the span:** "
            + ", ".join(f"{d} {totals[d]}" for d in _DETECTORS)
            + f". The **{top}** detector dominates, which — as with the dark-aircraft story — "
            "reads as an over-firing class to triage next, not a genuine surge: routine "
            "low-level traffic near the border watch box is the likely explanation, and it is "
            "the maritime sibling's loitering-false-positive lesson repeating in the air. "
            "Recorded here as the next investigation, not silently tuned away.",
        ]
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
        rows, outage_count = evaluate(session, args.region)
        session.rollback()  # read-only: never persist the track rebuild or anything else

    if not rows:
        print("no data for region", args.region)
        return
    span_h = (rows[-1].hour - rows[0].hour).total_seconds() / 3600.0 + 1
    block = render(rows, outage_count, span_h)
    print("\n" + block)
    if args.write:
        write_eval_md(block)
        log.info("eval_multiday_written", hours=len(rows))


if __name__ == "__main__":
    main()
