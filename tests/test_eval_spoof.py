from datetime import datetime, timedelta

from scripts.eval_spoof import (
    SpeedStep,
    SpoofStudy,
    render,
    threshold_impact,
)


def _step(icao24: str, speed: float) -> SpeedStep:
    return SpeedStep(
        icao24=icao24,
        speed_kt=speed,
        seconds=30,
        distance_km=speed * 30 / 1_943.84,
        reported_speed_kt=450,
    )


def test_threshold_impact_requires_repeated_violations_per_aircraft() -> None:
    steps = [_step("abc001", speed) for speed in (1_001, 1_100, 1_200)]
    steps += [_step("abc002", 1_500), _step("abc003", 1_600)]

    impact = threshold_impact(steps, 1_000)

    assert impact.steps == 5
    assert impact.aircraft == 3
    assert impact.repeated_candidates == 1


def test_render_keeps_physical_ceiling_when_lowering_adds_no_evidence() -> None:
    start = datetime(2026, 7, 23)
    study = SpoofStudy(
        steps=[_step("abc001", speed) for speed in (300, 450, 700, 800)],
        reports=5,
        aircraft=1,
        started_at=start,
        ended_at=start + timedelta(hours=42),
    )

    report = render(study)

    assert "| 750 kt | 1 | 1 | 0 |" in report
    assert "**Decision:** Keep **1,400 kt**." in report
    assert "does not measure recall" in report


def test_render_keeps_ceiling_when_all_thresholds_find_same_candidate() -> None:
    start = datetime(2026, 7, 23)
    study = SpoofStudy(
        steps=[_step("abc001", speed) for speed in (1_500, 1_600, 1_700)],
        reports=4,
        aircraft=1,
        started_at=start,
        ended_at=start + timedelta(hours=42),
    )

    report = render(study)

    assert "same repeated-aircraft candidate" in report
    assert "lowering the ceiling adds no fix pair" in report
