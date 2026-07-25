from datetime import timedelta

import pytest
import respx
from httpx import Response
from scripts.correlate_air_sea import fetch_lane, render_markdown

from horus.fusion import correlate_air_sea, parse_evidence


def _row(
    doc_id: str,
    *,
    published: str = "2026-07-25T10:00:00Z",
    lat: object = 1.2,
    lon: object = 103.7,
) -> dict[str, object]:
    return {
        "doc_id": doc_id,
        "title": f"Evidence {doc_id}",
        "source": "test lane",
        "reliability": "C",
        "credibility": 3,
        "summary": "Human-review evidence.",
        "published": published,
        "url": f"/incidents/{doc_id}",
        "detector": "gap",
        "lat": lat,
        "lon": lon,
    }


def test_parse_and_correlate_same_coarse_space_time_cell() -> None:
    air = parse_evidence(_row("air-1"), "air", "http://air.test")
    sea = parse_evidence(
        _row("sea-1", published="2026-07-25T11:15:00+00:00", lat=1.3, lon=103.9),
        "sea",
        "http://sea.test",
    )
    assert air is not None and sea is not None
    assert air.url == "http://air.test/incidents/air-1"

    matches = correlate_air_sea([air], [sea], time_window=timedelta(hours=2))

    assert len(matches) == 1
    assert matches[0].delta_minutes == 75
    assert matches[0].distance_km > 0


def test_correlation_excludes_wrong_cell_time_and_bounds() -> None:
    air = parse_evidence(_row("air"), "air", "http://air.test")
    adjacent = parse_evidence(_row("adjacent", lon=104.1), "sea", "http://sea.test")
    late = parse_evidence(
        _row("late", published="2026-07-25T14:00:01Z"),
        "sea",
        "http://sea.test",
    )
    outside = parse_evidence(_row("outside", lat=2.5), "sea", "http://sea.test")
    assert air is not None and adjacent is not None and late is not None and outside is not None

    assert correlate_air_sea([air], [adjacent, late, outside]) == []


@pytest.mark.parametrize(
    "change",
    [
        {"doc_id": ""},
        {"published": "not-a-date"},
        {"lat": None},
        {"lon": float("nan")},
        {"lat": 91},
    ],
)
def test_parse_evidence_skips_uncorrelatable_rows(change: dict[str, object]) -> None:
    row = _row("bad")
    row.update(change)
    assert parse_evidence(row, "air", "http://air.test") is None


@respx.mock
def test_unavailable_lane_degrades_to_visible_negative() -> None:
    respx.get("http://air.test/geoint/evidence").mock(return_value=Response(503))
    respx.get("http://sea.test/geoint/evidence").mock(
        return_value=Response(200, json=[_row("sea")])
    )

    air = fetch_lane("air", "http://air.test")
    sea = fetch_lane("sea", "http://sea.test")
    rendered = render_markdown(air, sea, [], cell_deg=0.5, window_hours=3, limit=20)

    assert air.usable == []
    assert air.status.startswith("unavailable:")
    assert "No air/sea pairs" in rendered
    assert "unavailable" in rendered
