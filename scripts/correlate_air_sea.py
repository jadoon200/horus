"""Build a read-only joint air + sea review table from the sibling APIs.

Examples:
    python -m scripts.correlate_air_sea
    python -m scripts.correlate_air_sea --horus-url http://127.0.0.1:8010 \
        --pharos-url http://127.0.0.1:8011 --limit 20

The output is triage, not a fused verdict.  A pair means only that two independently
generated evidence items began in the same coarse cell within the configured time window.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx

from horus.fusion import (
    AirSeaCorrelation,
    Bounds,
    FusionEvidence,
    cell_label,
    correlate_air_sea,
    parse_evidence,
)


@dataclass(frozen=True)
class LaneResult:
    lane: str
    api_url: str
    status: str
    received: int
    usable: list[FusionEvidence]


def _evidence_url(api_url: str) -> str:
    return f"{api_url.rstrip('/')}/geoint/evidence"


def fetch_lane(lane: str, api_url: str, timeout_seconds: float = 10.0) -> LaneResult:
    """Fetch one lane; malformed/unreachable siblings become an explicit status, not a crash."""
    try:
        response = httpx.get(
            _evidence_url(api_url),
            params={"limit": 500},
            timeout=timeout_seconds,
            follow_redirects=True,
        )
        response.raise_for_status()
        payload: Any = response.json()
        if not isinstance(payload, list):
            raise ValueError("expected a JSON list")
    except (httpx.HTTPError, ValueError) as exc:
        return LaneResult(lane, api_url, f"unavailable: {exc}", 0, [])

    rows = [row for row in payload if isinstance(row, dict)]
    usable = [
        parsed
        for row in rows
        if (parsed := parse_evidence(row, lane=lane, api_url=api_url)) is not None
    ]
    status = "ok"
    if len(usable) != len(rows):
        status = f"ok; skipped {len(rows) - len(usable)} malformed"
    return LaneResult(lane, api_url, status, len(rows), usable)


def _clean(value: str) -> str:
    return " ".join(value.replace("|", "/").split())


def _citation(item: FusionEvidence) -> str:
    label = _clean(item.doc_id)
    return f"[{label}]({item.url})" if item.url else label


def render_markdown(
    air: LaneResult,
    sea: LaneResult,
    matches: list[AirSeaCorrelation],
    *,
    cell_deg: float,
    window_hours: float,
    limit: int,
) -> str:
    shown = matches[:limit]
    lines = [
        "# Joint air + sea review",
        "",
        "> **Analytic contract:** co-location is not causation. This table makes no attribution, "
        "does not geolocate an emitter, and does not link an aircraft event to a vessel event. "
        "It is human-review triage only.",
        "",
        "## Source status",
        "",
        "| Lane | API | Status | Rows | Geolocated + timed |",
        "|---|---|---|---:|---:|",
        f"| Air | `{air.api_url}` | {_clean(air.status)} | {air.received} | {len(air.usable)} |",
        f"| Sea | `{sea.api_url}` | {_clean(sea.status)} | {sea.received} | {len(sea.usable)} |",
        "",
        f"Method: same **{cell_deg:.1f}°** latitude/longitude cell, event starts within "
        f"**{window_hours:g} h**, bounded to the Singapore Strait analysis box. "
        "Distance is shown to expose how coarse the cell is.",
        "",
        "## Gray-zone correlation table",
        "",
    ]
    if not shown:
        lines += [
            "No air/sea pairs met the configured coarse space-time rule. This is a valid "
            "negative; unavailable lanes and malformed rows are reported above.",
        ]
        return "\n".join(lines)

    lines += [
        "| Air UTC | Cell | Air evidence | Sea evidence | Δ time | Distance |",
        "|---|---|---|---|---:|---:|",
    ]
    for match in shown:
        lines.append(
            f"| {match.air.published:%Y-%m-%d %H:%M} | "
            f"{cell_label(match.cell, cell_deg)} | "
            f"{_clean(match.air.detector)} {match.air.reliability}{match.air.credibility} "
            f"{_citation(match.air)} | "
            f"{_clean(match.sea.detector)} {match.sea.reliability}{match.sea.credibility} "
            f"{_citation(match.sea)} | "
            f"{match.delta_minutes:.0f} min | {match.distance_km:.1f} km |"
        )
    if len(matches) > len(shown):
        lines += ["", f"_Showing {len(shown)} of {len(matches)} review pairs._"]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Correlate HORUS air and PHAROS maritime evidence for human review",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--horus-url", default="http://127.0.0.1:8000")
    parser.add_argument("--pharos-url", default="http://127.0.0.1:8001")
    parser.add_argument("--cell-deg", type=float, default=0.5)
    parser.add_argument("--window-hours", type=float, default=3.0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--bounds",
        type=float,
        nargs=4,
        metavar=("MIN_LAT", "MIN_LON", "MAX_LAT", "MAX_LON"),
        default=(0.5, 102.5, 2.0, 104.8),
    )
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")

    air = fetch_lane("air", args.horus_url, args.timeout)
    sea = fetch_lane("sea", args.pharos_url, args.timeout)
    bounds: Bounds = tuple(args.bounds)
    matches = correlate_air_sea(
        air.usable,
        sea.usable,
        cell_deg=args.cell_deg,
        time_window=timedelta(hours=args.window_hours),
        bounds=bounds,
    )
    print(
        render_markdown(
            air,
            sea,
            matches,
            cell_deg=args.cell_deg,
            window_hours=args.window_hours,
            limit=args.limit,
        )
    )


if __name__ == "__main__":
    main()
