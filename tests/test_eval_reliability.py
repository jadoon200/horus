from datetime import datetime, timedelta

from scripts.eval_reliability import (
    CalibrationObservation,
    CalibrationStudy,
    classify_gap,
    classify_incursion,
    classify_jamming,
    render,
)

from horus.config import Settings


def test_detector_specific_plausibility_proxies_are_transparent() -> None:
    settings = Settings()

    assert (
        classify_gap({"gap_minutes": 11, "displacement_km": 300}, settings)[0] == "less plausible"
    )
    assert (
        classify_gap({"gap_minutes": 25, "displacement_km": 110}, settings)[0] == "more plausible"
    )
    assert classify_incursion("B738", "9V-ABC")[0] == "less plausible"
    assert classify_incursion("P28A", "N444KL")[0] == "more plausible"
    assert classify_jamming({"aircraft_observed": 4}, settings)[0] == "less plausible"
    assert classify_jamming({"aircraft_observed": 8}, settings)[0] == "more plausible"


def test_render_states_when_a_grade_does_not_discriminate() -> None:
    start = datetime(2026, 7, 23)
    study = CalibrationStudy(
        observations=[
            CalibrationObservation("gap", "C", "more plausible", "far above floors"),
            CalibrationObservation("gap", "D", "less plausible", "near floors"),
            CalibrationObservation("incursion", "C", "less plausible", "airliner"),
            CalibrationObservation("incursion", "C", "more plausible", "GA"),
            CalibrationObservation("jamming", "C", "less plausible", "minimum sample"),
        ],
        reports=120_000,
        aircraft=1_000,
        started_at=start,
        ended_at=start + timedelta(hours=42),
    )

    report = render(study)

    assert "| incursion | C | 1 | 0 | 1 | 2 |" in report
    assert "Incursion grades had no discriminating power" in report
    assert "not calibrated across detector classes" in report
