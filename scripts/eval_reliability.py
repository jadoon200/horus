"""Cross-check reliability grades against simple real-traffic plausibility proxies.

    HORUS_DATABASE_URL=sqlite:///data/sg-live.db \
        python -m scripts.eval_reliability --region sg-live --write

This is deliberately a calibration *audit*, not ground truth.  No labelled real incident
set exists, so each detector gets a transparent proxy:

- gaps: distance above the configured duration/displacement floors;
- incursions: likely routine airliner versus likely GA/rotorcraft by type code;
- jamming: aircraft corroborating the cell.

The gap and jamming proxies overlap the grade rules, so agreement there is not independent
validation.  The generated report says that explicitly and treats a one-grade detector as
non-discriminating rather than mistaking consistency for calibration.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from textwrap import fill
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from horus.config import Settings, get_settings
from horus.db.base import session_scope
from horus.db.models import Aircraft, Incident, Position
from horus.detect.gaps import detect_gaps
from horus.detect.incursion import detect_incursions
from horus.detect.jamming import detect_jamming
from horus.logging import configure_logging, get_logger
from horus.timeutil import utc_naive

log = get_logger(__name__)

_EVAL_MD = Path(__file__).resolve().parents[1] / "docs" / "EVAL.md"
_PLAUSIBILITY = ("more plausible", "unclear", "less plausible")
_AIRLINER_PREFIXES = (
    "A2",
    "A3",
    "AT7",
    "B3",
    "B6",
    "B7",
    "B8",
    "CRJ",
    "DH8",
    "E1",
    "E2",
    "F7",
    "MD",
)
_GA_ROTOR_PREFIXES = (
    "A139",
    "BE",
    "C1",
    "C2",
    "C3",
    "C4",
    "DA",
    "EC",
    "H",
    "P28",
    "P32",
    "PC",
    "R44",
    "R66",
    "SR",
)


@dataclass(frozen=True)
class CalibrationObservation:
    detector: str
    grade: str
    plausibility: str
    reason: str


@dataclass(frozen=True)
class CalibrationStudy:
    observations: list[CalibrationObservation]
    reports: int
    aircraft: int
    started_at: datetime
    ended_at: datetime

    @property
    def span_hours(self) -> float:
        return (self.ended_at - self.started_at).total_seconds() / 3600.0


def _number(evidence: dict[str, Any], key: str) -> float:
    value = evidence.get(key)
    if not isinstance(value, int | float):
        raise ValueError(f"incident evidence {key!r} is not numeric")
    return float(value)


def classify_gap(evidence: dict[str, Any], settings: Settings) -> tuple[str, str]:
    """Classify a gap by its weakest margin above the two detector floors."""
    duration_ratio = _number(evidence, "gap_minutes") / settings.gap_min_minutes
    distance_ratio = _number(evidence, "displacement_km") / settings.gap_min_displacement_km
    weakest_margin = min(duration_ratio, distance_ratio)
    reason = (
        f"weakest threshold margin {weakest_margin:.2f}x "
        f"(duration {duration_ratio:.2f}x, distance {distance_ratio:.2f}x)"
    )
    if weakest_margin <= 1.5:
        return "less plausible", reason
    if weakest_margin >= 2.0:
        return "more plausible", reason
    return "unclear", reason


def classify_incursion(type_code: str | None, registration: str | None) -> tuple[str, str]:
    """Use aircraft class as a proxy for routine airway traffic, never identity proof."""
    code = (type_code or "").strip().upper()
    if code.startswith(_AIRLINER_PREFIXES):
        return "less plausible", f"airliner-type proxy ({code})"
    if code.startswith(_GA_ROTOR_PREFIXES):
        return "more plausible", f"GA/rotorcraft-type proxy ({code})"
    if not code and registration and registration.upper().startswith(("N", "VH-", "9V-")):
        return "more plausible", f"registration-only private/GA proxy ({registration.upper()})"
    return "unclear", f"type proxy unavailable or unclassified ({code or 'missing'})"


def classify_jamming(evidence: dict[str, Any], settings: Settings) -> tuple[str, str]:
    """Classify a jamming cell by corroborating-aircraft count."""
    observed = int(_number(evidence, "aircraft_observed"))
    ratio = observed / settings.gnss_min_aircraft
    reason = f"{observed} aircraft ({ratio:.2f}x the {settings.gnss_min_aircraft}-aircraft floor)"
    if observed <= settings.gnss_min_aircraft:
        return "less plausible", reason
    if observed >= 2 * settings.gnss_min_aircraft:
        return "more plausible", reason
    return "unclear", reason


def _observe(
    incident: Incident,
    settings: Settings,
    aircraft: dict[str, Aircraft],
) -> CalibrationObservation:
    evidence = incident.evidence or {}
    if incident.detector == "gap":
        plausibility, reason = classify_gap(evidence, settings)
    elif incident.detector == "incursion":
        craft = aircraft.get(incident.icao24 or "")
        plausibility, reason = classify_incursion(
            craft.type_code if craft else None,
            craft.registration if craft else None,
        )
    elif incident.detector == "jamming":
        plausibility, reason = classify_jamming(evidence, settings)
    else:
        raise ValueError(f"unsupported calibration detector: {incident.detector}")
    return CalibrationObservation(
        detector=incident.detector,
        grade=incident.reliability,
        plausibility=plausibility,
        reason=reason,
    )


def evaluate(session: Session, region: str) -> CalibrationStudy | None:
    lo = session.scalar(select(func.min(Position.ts)).where(Position.region == region))
    hi = session.scalar(select(func.max(Position.ts)).where(Position.region == region))
    if lo is None or hi is None:
        return None

    incidents: list[Incident] = []
    incidents += detect_jamming(session, region)[0]
    incidents += detect_gaps(session, region)
    incidents += detect_incursions(session, region)
    craft = {row.icao24: row for row in session.scalars(select(Aircraft))}
    settings = get_settings()
    observations = [_observe(incident, settings, craft) for incident in incidents]
    reports = session.scalar(
        select(func.count()).select_from(Position).where(Position.region == region)
    )
    aircraft_count = session.scalar(
        select(func.count(func.distinct(Position.icao24))).where(Position.region == region)
    )
    return CalibrationStudy(
        observations=observations,
        reports=int(reports or 0),
        aircraft=int(aircraft_count or 0),
        started_at=utc_naive(lo),
        ended_at=utc_naive(hi),
    )


def _verdict(observations: list[CalibrationObservation]) -> str:
    by_detector = Counter((o.detector, o.grade, o.plausibility) for o in observations)
    gap_c_less = by_detector[("gap", "C", "less plausible")]
    gap_c_total = sum(by_detector[("gap", "C", p)] for p in _PLAUSIBILITY)
    gap_d_less = by_detector[("gap", "D", "less plausible")]
    gap_d_total = sum(by_detector[("gap", "D", p)] for p in _PLAUSIBILITY)
    incursion_grades = {o.grade for o in observations if o.detector == "incursion"}
    jamming_grades = {o.grade for o in observations if o.detector == "jamming"}

    gap_result = (
        f"For gaps, D-grade calls were less plausible by the margin proxy in "
        f"{gap_d_less}/{gap_d_total} cases versus {gap_c_less}/{gap_c_total} C-grade calls. "
        if gap_c_total and gap_d_total
        else "The gap sample did not contain both C and D grades, so it cannot test separation. "
    )
    incursion_result = (
        "Incursion grades had no discriminating power: every call received the same grade, "
        "even though the aircraft-class proxy separated routine airliners from GA/rotorcraft. "
        if len(incursion_grades) <= 1
        else "Incursion calls occupied multiple grades, allowing limited discrimination. "
    )
    jamming_result = (
        "Jamming grades also had no within-sample discrimination, and the corroboration proxy "
        "is the grade rule itself, so agreement would be circular. "
        if len(jamming_grades) <= 1
        else "Jamming calls occupied multiple grades, but the proxy is still circular. "
    )
    return (
        gap_result
        + incursion_result
        + jamming_result
        + "**Bottom line:** the letter is a useful evidence-quality cue for gaps, but it is "
        "not calibrated across detector classes and must not be read as a probability. For "
        "one-grade detectors it is presently descriptive rather than discriminating."
    )


def render(study: CalibrationStudy) -> str:
    counts = Counter((o.detector, o.grade, o.plausibility) for o in study.observations)
    groups = sorted({(o.detector, o.grade) for o in study.observations})
    lines = [
        "## Reliability-grade calibration against real-traffic proxies (2026-07-25)",
        "",
        fill(
            f"Across **{study.reports:,} reports**, **{study.aircraft} aircraft**, and "
            f"**{study.span_hours:.2f} hours**, the deterministic detectors produced "
            f"**{len(study.observations)} incidents** in the three classes with a usable "
            "plausibility proxy. These are proxies, not labels or operational ground truth.",
            width=100,
        ),
        "",
        "| Detector | Grade | More plausible | Unclear | Less plausible | Total |",
        "|---|:---:|---:|---:|---:|---:|",
    ]
    for detector, grade in groups:
        row = [counts[(detector, grade, p)] for p in _PLAUSIBILITY]
        lines.append(f"| {detector} | {grade} | {row[0]} | {row[1]} | {row[2]} | {sum(row)} |")
    lines += [
        "",
        "The proxies are intentionally legible:",
        "",
        fill(
            "- **Gap:** the weakest margin above the configured 10-minute / 50-km detector "
            "floors (≤1.5x less plausible, ≥2x more plausible). This partially overlaps the "
            "grade's duration rule, so it is a sanity check rather than independent validation.",
            width=100,
            subsequent_indent="  ",
        ),
        fill(
            "- **Incursion:** likely routine airliner versus likely GA/rotorcraft from the "
            "advisory type code (registration-only fallback is explicitly weaker). This tests "
            "whether the fixed C grade distinguishes the residual airway confound.",
            width=100,
            subsequent_indent="  ",
        ),
        fill(
            "- **Jamming:** aircraft corroborating the cell (minimum-count cells less plausible, "
            "≥2x the minimum more plausible). This is circular with the sample-count grade and "
            "therefore cannot independently validate it.",
            width=100,
            subsequent_indent="  ",
        ),
        "",
        fill(_verdict(study.observations), width=100),
    ]
    return "\n".join(lines)


def write_eval_md(block: str) -> None:
    marker = "## Reliability-grade calibration against real-traffic proxies"
    text = _EVAL_MD.read_text() if _EVAL_MD.exists() else "# HORUS evaluation\n\n"
    if marker in text:
        head, _, rest = text.partition(marker)
        after = rest.split("\n## ", 1)
        tail = ("\n\n## " + after[1]) if len(after) > 1 else "\n"
        text = head + block + tail
    else:
        anchor = "## Flagship anomaly model"
        text = (
            text.replace(anchor, block + "\n\n" + anchor, 1)
            if anchor in text
            else text + "\n\n" + block + "\n"
        )
    _EVAL_MD.write_text(text)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Audit reliability grades against proxies")
    parser.add_argument("--region", default="sg-live")
    parser.add_argument("--write", action="store_true", help="update docs/EVAL.md")
    args = parser.parse_args()

    with session_scope() as session:
        study = evaluate(session, args.region)
        session.rollback()  # read-only: detector calls must never rewrite the live lane

    if study is None:
        print("no data for region", args.region)
        return
    block = render(study)
    print("\n" + block)
    if args.write:
        write_eval_md(block)
        log.info("eval_reliability_written", incidents=len(study.observations))


if __name__ == "__main__":
    main()
