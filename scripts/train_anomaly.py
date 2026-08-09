"""Freeze the flagship GRU anomaly model into a versioned, SHA-pinned artifact.

    HORUS_DATABASE_URL=sqlite:///data/sg-live.db python -m scripts.train_anomaly

Trains the V7-selected model (8 hidden units, the benchmarked config) on the real tracks in
the given database, saves the artifact, and prints its SHA-256 — the freeze record that goes
in docs/EVAL.md (e.g. SG-AIR-v1). The served `/score-track` route loads this exact artifact,
so the SHA is what proves the served model is the benchmarked one, not a silently-retrained
drift (the maritime sibling's discipline).

The real training database stays local (it holds real aircraft identities); only the SHA and
counts are published. The deployed demo bakes a *separate* artifact trained on the synthetic
seed at image-build time.
"""

from __future__ import annotations

import argparse

from sqlalchemy import select

from horus.db.base import session_scope
from horus.db.models import Track
from horus.detect.seq_anomaly import artifact_path, artifact_sha256, train_model
from horus.logging import configure_logging, get_logger

log = get_logger(__name__)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Freeze the GRU anomaly artifact")
    parser.add_argument("--region", default=None, help="restrict to one region label")
    parser.add_argument("--hidden", type=int, default=8, help="V7-selected capacity")
    parser.add_argument("--epochs", type=int, default=60)
    args = parser.parse_args()

    with session_scope() as session:
        q = select(Track).where(Track.sequence.is_not(None)).order_by(Track.track_id)
        if args.region:
            q = q.where(Track.region == args.region)
        sequences = [t.sequence for t in session.scalars(q) if t.sequence]

    if len(sequences) < 20:
        raise SystemExit(
            f"only {len(sequences)} tracks — collect more before freezing a real model"
        )

    model = train_model(sequences, hidden=args.hidden, epochs=args.epochs)
    model.save()
    sha = artifact_sha256()
    print(f"\nfroze {len(sequences)} real tracks -> {artifact_path()}")
    print(f"artifact SHA-256: {sha}")
    print(f"threshold (p99 train reconstruction error): {model.threshold:.6f}")
    print("\nrecord this SHA in docs/EVAL.md and set HORUS_ANOMALY_ARTIFACT_SHA256 to pin it.")
    log.info("anomaly_frozen", tracks=len(sequences), sha256=sha, threshold=model.threshold)


if __name__ == "__main__":
    main()
