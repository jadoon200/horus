"""Seed a self-contained demo database from the synthetic scenario.

    HORUS_DATABASE_URL=sqlite:///data/demo.db python -m scripts.seed_demo

Generates the labelled gold scenario, builds tracks, runs the deterministic detectors and
the GRU anomaly pass — the same demo seed the single-container deploy bakes in.
"""

from __future__ import annotations

from horus.db.base import init_sqlite_schema, session_scope
from horus.detect.anomaly import detect_anomalies
from horus.detect.run import run_detectors
from horus.detect.seq_anomaly import artifact_sha256
from horus.ingest.synthetic import generate, seed_db
from horus.logging import configure_logging, get_logger
from horus.tracks.build import build_tracks

log = get_logger(__name__)


def main() -> None:
    configure_logging()
    init_sqlite_schema()
    with session_scope() as session:
        inserted = seed_db(session, generate())
        tracks = build_tracks(session)
        run_detectors(session)
        incidents, model = detect_anomalies(session, epochs=40)
        # Freeze the trained GRU into the image so /score-track serves the SAME model that
        # produced the demo's anomaly incidents — no train-on-first-request, no drift.
        if model is not None:
            model.save()
        log.info(
            "demo_seeded",
            positions=inserted,
            tracks=tracks,
            anomaly_incidents=len(incidents),
            artifact_sha256=artifact_sha256(),
        )


if __name__ == "__main__":
    main()
