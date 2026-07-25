from datetime import datetime, timedelta

from scripts.eval_multiday import HourStats, aggregate_clock_hours, render


def _row(
    hour: datetime,
    *,
    reports: int,
    unscoreable: int,
    outage_minutes: float = 0,
) -> HourStats:
    return HourStats(
        hour=hour,
        reports=reports,
        aircraft=20,
        cell_windows=100,
        unscoreable=unscoreable,
        outage_minutes=outage_minutes,
    )


def test_clock_hour_aggregate_is_cross_day_mean_and_population_spread() -> None:
    start = datetime(2026, 7, 23, 0)
    rows = [_row(start + timedelta(hours=hour), reports=1000, unscoreable=20) for hour in range(24)]
    rows += [
        _row(start + timedelta(days=1, hours=hour), reports=3000, unscoreable=40)
        for hour in range(24)
    ]

    aggregates = aggregate_clock_hours(rows, start, start + timedelta(hours=48))

    assert len(aggregates) == 24
    midnight_sgt = next(row for row in aggregates if row.local_hour == 0)
    assert midnight_sgt.samples == 2
    assert midnight_sgt.reports_mean == 2000
    assert midnight_sgt.reports_spread == 1000
    assert midnight_sgt.unscoreable_mean == 30
    assert midnight_sgt.unscoreable_spread == 10


def test_aggregate_excludes_partial_edges_and_material_outage() -> None:
    start = datetime(2026, 7, 23, 0, 20)
    rows = [
        _row(datetime(2026, 7, 23, 0), reports=10, unscoreable=90),
        _row(datetime(2026, 7, 23, 1), reports=100, unscoreable=20),
        _row(
            datetime(2026, 7, 24, 1),
            reports=10,
            unscoreable=90,
            outage_minutes=5,
        ),
        _row(datetime(2026, 7, 25, 1), reports=300, unscoreable=40),
        _row(datetime(2026, 7, 25, 2), reports=10, unscoreable=90),
    ]

    aggregates = aggregate_clock_hours(rows, start, datetime(2026, 7, 25, 2, 20))

    local_09 = next(row for row in aggregates if row.local_hour == 9)
    assert local_09.samples == 2
    assert local_09.reports_mean == 200
    assert local_09.unscoreable_mean == 30
    assert all(row.local_hour not in {8, 10} for row in aggregates)


def test_render_switches_to_multi_cycle_table_only_at_48_hours() -> None:
    start = datetime(2026, 7, 23, 0)
    rows = [
        _row(start + timedelta(hours=hour), reports=1000 + hour, unscoreable=20 + hour % 3)
        for hour in range(48)
    ]

    before = render(rows[:-1], 0, start, start + timedelta(hours=47))
    ready = render(rows, 0, start, start + timedelta(hours=48))

    assert "has **not** crossed the 48-hour" in before
    assert "crosses the **48-hour / two-cycle bar**" in ready
    assert "mean ± population standard deviation" in ready
    assert "| 00:00 | 2 |" in ready
