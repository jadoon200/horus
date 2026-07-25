"""One-shot adsb.lol/OpenSky receiver-coverage cross-check.

This script is intentionally not a collector. OpenSky's current terms require a written
agreement for operational REST integration, and its state vectors do not expose the
NIC/NACp/SIL fields required by HORUS's jamming detector. The comparison therefore answers
one narrow research question: how many aircraft and spatial cells does the independent
receiver network see in the same latest-state window?

Running requires explicit terms acknowledgement:

    python -m scripts.eval_opensky --acknowledge-opensky-terms

Anonymous latest-state access is sufficient. OAuth client credentials are optional
`HORUS_OPENSKY_CLIENT_ID` / `HORUS_OPENSKY_CLIENT_SECRET` settings.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path
from textwrap import fill

from horus.config import get_settings
from horus.db.base import session_scope
from horus.ingest.adsb import AdsbSample, fetch_point
from horus.ingest.opensky import fetch_bbox
from horus.ingest.persist import persist_samples
from horus.logging import configure_logging, get_logger

log = get_logger(__name__)

_EVAL_MD = Path(__file__).resolve().parents[1] / "docs" / "EVAL.md"


@dataclass(frozen=True)
class CoverageComparison:
    adsb_aircraft: int
    opensky_aircraft: int
    shared_aircraft: int
    adsb_cells: int
    opensky_cells: int
    shared_cells: int
    adsb_scoreable_cells: int
    opensky_dense_cells: int
    union_dense_cells: int
    density_gain_cells: int


def _cells(samples: list[AdsbSample], cell_deg: float) -> dict[tuple[int, int], set[str]]:
    cells: dict[tuple[int, int], set[str]] = {}
    for sample in samples:
        key = (math.floor(sample.lat / cell_deg), math.floor(sample.lon / cell_deg))
        cells.setdefault(key, set()).add(sample.icao24)
    return cells


def compare_coverage(
    adsb: list[AdsbSample],
    opensky: list[AdsbSample],
    *,
    cell_deg: float,
    min_aircraft: int,
) -> CoverageComparison:
    """Compare receiver density; union counts are potential, not jamming scoreability."""
    adsb_cells = _cells(adsb, cell_deg)
    opensky_cells = _cells(opensky, cell_deg)
    keys = set(adsb_cells) | set(opensky_cells)
    adsb_icaos = {sample.icao24 for sample in adsb}
    opensky_icaos = {sample.icao24 for sample in opensky}
    return CoverageComparison(
        adsb_aircraft=len(adsb_icaos),
        opensky_aircraft=len(opensky_icaos),
        shared_aircraft=len(adsb_icaos & opensky_icaos),
        adsb_cells=len(adsb_cells),
        opensky_cells=len(opensky_cells),
        shared_cells=len(set(adsb_cells) & set(opensky_cells)),
        adsb_scoreable_cells=sum(len(aircraft) >= min_aircraft for aircraft in adsb_cells.values()),
        opensky_dense_cells=sum(
            len(aircraft) >= min_aircraft for aircraft in opensky_cells.values()
        ),
        union_dense_cells=sum(
            len(adsb_cells.get(key, set()) | opensky_cells.get(key, set())) >= min_aircraft
            for key in keys
        ),
        density_gain_cells=sum(
            len(adsb_cells.get(key, set())) < min_aircraft
            and len(adsb_cells.get(key, set()) | opensky_cells.get(key, set())) >= min_aircraft
            for key in keys
        ),
    )


def render(result: CoverageComparison, *, cell_deg: float, min_aircraft: int) -> str:
    return "\n".join(
        [
            "## Optional OpenSky receiver-coverage cross-check",
            "",
            fill(
                "One contemporaneous latest-state snapshot from each network, over the same "
                "Singapore bounding box. This measures receiver coverage only: OpenSky state "
                "vectors do not expose NIC/NACp/SIL, so their aircraft cannot make a HORUS "
                "jamming cell scoreable or corroborate an integrity collapse.",
                width=100,
            ),
            "",
            "| Measure | adsb.lol | OpenSky |",
            "|---|---:|---:|",
            f"| Aircraft | {result.adsb_aircraft} | {result.opensky_aircraft} |",
            f"| {cell_deg:g}° cells with aircraft | {result.adsb_cells} | {result.opensky_cells} |",
            f"| Cells with ≥{min_aircraft} aircraft | {result.adsb_scoreable_cells} | "
            f"{result.opensky_dense_cells} |",
            "",
            f"Shared aircraft: **{result.shared_aircraft}**; shared occupied cells: "
            f"**{result.shared_cells}**. A hypothetical identity union reaches the "
            f"{min_aircraft}-aircraft density floor in **{result.union_dense_cells}** cells, "
            f"including **{result.density_gain_cells}** that adsb.lol alone did not. Those "
            "last figures are coverage potential only, not additional GNSS-integrity evidence.",
        ]
    )


def write_eval_md(block: str) -> None:
    marker = "## Optional OpenSky receiver-coverage cross-check"
    text = _EVAL_MD.read_text() if _EVAL_MD.exists() else "# HORUS evaluation\n\n"
    if marker in text:
        head, _, rest = text.partition(marker)
        after = rest.split("\n## ", 1)
        tail = ("\n\n## " + after[1]) if len(after) > 1 else "\n"
        text = head + block + tail
    else:
        anchor = "## Incursion-airway source spike"
        text = (
            text.replace(anchor, block + "\n\n" + anchor, 1)
            if anchor in text
            else text + "\n\n" + block + "\n"
        )
    _EVAL_MD.write_text(text)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="One-shot OpenSky coverage cross-check")
    parser.add_argument("--lamin", type=float, default=0.0)
    parser.add_argument("--lomin", type=float, default=102.5)
    parser.add_argument("--lamax", type=float, default=2.5)
    parser.add_argument("--lomax", type=float, default=105.0)
    parser.add_argument(
        "--acknowledge-opensky-terms",
        action="store_true",
        help="confirm this one-shot research use complies with OpenSky's current terms",
    )
    parser.add_argument("--persist", action="store_true", help="persist the OpenSky snapshot")
    parser.add_argument("--region", default="sg-opensky-research")
    parser.add_argument("--write", action="store_true", help="update docs/EVAL.md")
    args = parser.parse_args()
    if not args.acknowledge_opensky_terms:
        parser.error("--acknowledge-opensky-terms is required before contacting OpenSky")

    settings = get_settings()
    adsb = [
        sample
        for sample in fetch_point()
        if args.lamin <= sample.lat <= args.lamax and args.lomin <= sample.lon <= args.lomax
    ]
    opensky = fetch_bbox(args.lamin, args.lomin, args.lamax, args.lomax)
    result = compare_coverage(
        adsb,
        opensky,
        cell_deg=settings.gnss_cell_deg,
        min_aircraft=settings.gnss_min_aircraft,
    )
    block = render(
        result,
        cell_deg=settings.gnss_cell_deg,
        min_aircraft=settings.gnss_min_aircraft,
    )
    print("\n" + block)

    if args.persist:
        with session_scope() as session:
            inserted = persist_samples(
                session,
                opensky,
                source=settings.opensky_source_label,
                region=args.region,
            )
        log.info("opensky_snapshot_persisted", inserted=inserted, region=args.region)
    if args.write:
        write_eval_md(block)
        log.info("eval_opensky_written")


if __name__ == "__main__":
    main()
