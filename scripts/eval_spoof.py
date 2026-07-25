"""Characterize the real implied-speed tail behind the spoof detector.

    HORUS_DATABASE_URL=sqlite:///data/sg-live.db \
        python -m scripts.eval_spoof --region sg-live --write

The detector requires three consecutive-fix violations above 1,400 kt.  Zero incidents can
mean either that the ceiling is needlessly high or that repeated kinematic impossibilities
are genuinely absent.  This read-only audit reports the complete speed distribution,
threshold sensitivity, and benign-tail clues before making that decision.
"""

from __future__ import annotations

import argparse
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from textwrap import fill

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from horus.db.base import session_scope
from horus.db.models import Position
from horus.geo import haversine_km, implied_speed_kn
from horus.logging import configure_logging, get_logger
from horus.timeutil import utc_naive

log = get_logger(__name__)

_EVAL_MD = Path(__file__).resolve().parents[1] / "docs" / "EVAL.md"
_HISTOGRAM_EDGES = (0.0, 250.0, 500.0, 750.0, 1_000.0, 1_200.0, 1_400.0, 2_000.0, 5_000.0)
_SENSITIVITY_THRESHOLDS = (750.0, 1_000.0, 1_200.0, 1_400.0)
_MIN_VIOLATIONS = 3


@dataclass(frozen=True)
class SpeedStep:
    icao24: str
    speed_kt: float
    seconds: float
    distance_km: float
    reported_speed_kt: float | None


@dataclass(frozen=True)
class ThresholdImpact:
    threshold_kt: float
    steps: int
    aircraft: int
    repeated_candidates: int


@dataclass(frozen=True)
class SpoofStudy:
    steps: list[SpeedStep]
    reports: int
    aircraft: int
    started_at: datetime
    ended_at: datetime

    @property
    def span_hours(self) -> float:
        return (self.ended_at - self.started_at).total_seconds() / 3600.0


def measure_steps(positions: list[Position]) -> list[SpeedStep]:
    """Return all positive-time consecutive-fix speeds, never spanning aircraft."""
    steps: list[SpeedStep] = []
    for previous, current in pairwise(positions):
        if previous.icao24 != current.icao24:
            continue
        seconds = (utc_naive(current.ts) - utc_naive(previous.ts)).total_seconds()
        if seconds <= 0:
            continue
        distance = haversine_km(previous.lat, previous.lon, current.lat, current.lon)
        reported = max(
            (speed for speed in (previous.gs_kt, current.gs_kt) if speed is not None),
            default=None,
        )
        steps.append(
            SpeedStep(
                icao24=current.icao24,
                speed_kt=implied_speed_kn(
                    previous.lat,
                    previous.lon,
                    current.lat,
                    current.lon,
                    seconds,
                ),
                seconds=seconds,
                distance_km=distance,
                reported_speed_kt=reported,
            )
        )
    return steps


def threshold_impact(steps: list[SpeedStep], threshold: float) -> ThresholdImpact:
    violations = [step for step in steps if step.speed_kt > threshold]
    by_aircraft = Counter(step.icao24 for step in violations)
    return ThresholdImpact(
        threshold_kt=threshold,
        steps=len(violations),
        aircraft=len(by_aircraft),
        repeated_candidates=sum(count >= _MIN_VIOLATIONS for count in by_aircraft.values()),
    )


def evaluate(session: Session, region: str) -> SpoofStudy | None:
    positions = list(
        session.scalars(
            select(Position).where(Position.region == region).order_by(Position.icao24, Position.ts)
        )
    )
    if not positions:
        return None
    lo = min(utc_naive(position.ts) for position in positions)
    hi = max(utc_naive(position.ts) for position in positions)
    aircraft = session.scalar(
        select(func.count(func.distinct(Position.icao24))).where(Position.region == region)
    )
    return SpoofStudy(
        steps=measure_steps(positions),
        reports=len(positions),
        aircraft=int(aircraft or 0),
        started_at=lo,
        ended_at=hi,
    )


def _histogram(steps: list[SpeedStep]) -> list[tuple[str, int]]:
    values = np.asarray([step.speed_kt for step in steps], dtype=np.float64)
    counts, _ = np.histogram(values, bins=(*_HISTOGRAM_EDGES, math.inf))
    labels = [f"{start:,.0f}-{end:,.0f}" for start, end in pairwise(_HISTOGRAM_EDGES)] + [
        f"≥{_HISTOGRAM_EDGES[-1]:,.0f}"
    ]
    return list(zip(labels, (int(count) for count in counts), strict=True))


def _decision(impacts: list[ThresholdImpact], max_speed: float) -> str:
    current = impacts[-1]
    lower = impacts[:-1]
    leaking = [impact for impact in lower if impact.repeated_candidates > 0]
    if current.repeated_candidates:
        unchanged = all(
            (impact.steps, impact.aircraft, impact.repeated_candidates)
            == (current.steps, current.aircraft, current.repeated_candidates)
            for impact in lower
        )
        if unchanged:
            return (
                "Keep **1,400 kt**. The same repeated-aircraft candidate appears at every "
                "sensitivity point down to 750 kt; lowering the ceiling adds no fix pair, "
                "aircraft, or candidate and would only erode the physical-impossibility margin. "
                "The observed discontinuities are a valid review call, while shared identity "
                "and feed corruption remain explicit benign explanations."
            )
        return (
            "Keep **1,400 kt** pending review. The current ceiling already produces repeated "
            "candidates, so changing it before those calls are explained would confound "
            "threshold tuning with incident triage."
        )
    if leaking:
        first = leaking[-1]
        return (
            f"Keep **1,400 kt**. Lowering to {first.threshold_kt:,.0f} kt would create "
            f"{first.repeated_candidates} repeated-aircraft candidate(s) from the observed "
            "benign tail, while the current rule stays quiet."
        )
    return (
        f"Keep **1,400 kt**. The observed maximum is {max_speed:,.0f} kt and even the lower "
        "sensitivity points create no repeated-aircraft incident. Lowering the ceiling would "
        "therefore add no demonstrated recall on this unlabelled corpus; it would only move "
        "the rule closer to ordinary fast-flight and position-jitter tails."
    )


def render(study: SpoofStudy) -> str:
    values = np.asarray([step.speed_kt for step in study.steps], dtype=np.float64)
    if values.size == 0:
        return "## Spoof-threshold characterization on real fixes (2026-07-25)\n\nNo fix pairs."
    percentiles = {
        label: float(np.percentile(values, percentile))
        for label, percentile in (("p50", 50), ("p90", 90), ("p99", 99), ("p99.9", 99.9))
    }
    impacts = [threshold_impact(study.steps, threshold) for threshold in _SENSITIVITY_THRESHOLDS]
    tail = [step for step in study.steps if step.speed_kt > 750]
    short_jitter = sum(step.seconds <= 10 and step.distance_km <= 10 for step in tail)
    seam_jumps = sum(step.seconds <= 120 and step.distance_km >= 25 for step in tail)
    reported_conflicts = sum(
        step.reported_speed_kt is not None and step.reported_speed_kt <= 650 for step in tail
    )

    lines = [
        "## Spoof-threshold characterization on real fixes (2026-07-25)",
        "",
        fill(
            f"Across **{study.reports:,} reports**, **{study.aircraft:,} aircraft**, and "
            f"**{study.span_hours:.2f} hours**, there were **{len(study.steps):,}** valid "
            "consecutive same-aircraft fix pairs after nonpositive timestamp deltas were "
            "excluded. Real traffic has no spoof labels, so this characterizes the benign "
            "tail; it does not measure recall.",
            width=100,
        ),
        "",
        "| Implied speed (kt) | Consecutive-fix pairs |",
        "|---:|---:|",
    ]
    lines += [f"| {label} | {count:,} |" for label, count in _histogram(study.steps)]
    lines += [
        "",
        "| Tail statistic | Implied speed (kt) |",
        "|---|---:|",
        *[f"| {label} | {value:,.1f} |" for label, value in percentiles.items()],
        f"| maximum | {float(values.max()):,.1f} |",
        "",
        "| Candidate ceiling | Violating pairs | Aircraft | ≥3-pair candidates |",
        "|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {impact.threshold_kt:,.0f} kt | {impact.steps} | {impact.aircraft} | "
        f"{impact.repeated_candidates} |"
        for impact in impacts
    ]
    lines += [
        "",
        fill(
            f"**Benign-tail clues above 750 kt:** {len(tail)} pairs total; {short_jitter} combine "
            "a ≤10-second interval with ≤10 km movement (short-interval position jitter), "
            f"{seam_jumps} jump ≥25 km inside two minutes (feed/coverage-seam shape), and "
            f"{reported_conflicts} conflict with a contemporaneous reported ground speed "
            "≤650 kt. These clues can overlap and are diagnostics, not cause labels.",
            width=100,
        ),
        "",
        fill(f"**Decision:** {_decision(impacts, float(values.max()))}", width=100),
        "",
        fill(
            "The synthetic teleporting identity remains a separate labelled ceiling test; "
            "this audit does not tune against it.",
            width=100,
        ),
    ]
    return "\n".join(lines)


def write_eval_md(block: str) -> None:
    marker = "## Spoof-threshold characterization on real fixes"
    text = _EVAL_MD.read_text() if _EVAL_MD.exists() else "# HORUS evaluation\n\n"
    if marker in text:
        head, _, rest = text.partition(marker)
        after = rest.split("\n## ", 1)
        tail = ("\n\n## " + after[1]) if len(after) > 1 else "\n"
        text = head + block + tail
    else:
        anchor = "## Reliability-grade calibration"
        text = (
            text.replace(anchor, block + "\n\n" + anchor, 1)
            if anchor in text
            else text + "\n\n" + block + "\n"
        )
    _EVAL_MD.write_text(text)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Characterize the implied-speed tail")
    parser.add_argument("--region", default="sg-live")
    parser.add_argument("--write", action="store_true", help="update docs/EVAL.md")
    args = parser.parse_args()

    with session_scope() as session:
        study = evaluate(session, args.region)
        session.rollback()

    if study is None:
        print("no data for region", args.region)
        return
    block = render(study)
    print("\n" + block)
    if args.write:
        write_eval_md(block)
        log.info("eval_spoof_written", pairs=len(study.steps))


if __name__ == "__main__":
    main()
