"""Decide the flagship trajectory-anomaly model on REAL tracks.

    python -m scripts.benchmark_anomaly --train data/sg-live.db --transfer data/baltic-control.db

The synthetic gold set cannot settle this. There, two perfectly circular injected orbits are
a linearly-separable caricature of "anomaly", and linear PCA beats the GRU (AUC 1.00 vs
0.976) — a result recorded honestly in docs/EVAL.md but worthless as a model decision.

Real tracks have no labels, so there is no AUC to compute and none is claimed. What can be
measured without labels:

1. **Rank stability across seeds.** A model whose top-K anomalies reshuffle with the random
   seed is not measuring the data, it is measuring its own initialization. Reported as mean
   pairwise Spearman correlation across seeds, and as top-K set overlap (Jaccard).
2. **Cross-region transfer.** Train on one region's pattern of life, score another's. The
   descriptors are translation-invariant per-step deltas, so a model that learned *shape*
   should still rank sensibly on unseen sky; one that memorized a region should not. This is
   the maritime sibling's methodology transplanted, and the reason both corpora exist.
3. **Reconstruction separation.** The ratio of the top-decile error to the median error —
   how far the model pushes its outliers from the population. Flat separation means the
   model cannot discriminate regardless of what it ranks first.

None of these is accuracy. Together they answer a narrower, honest question: which model is
*stable, transferable and discriminating* on real traffic. Interpretability of the actual
top-K tracks is a separate human-review step (see docs/EVAL.md).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field

import numpy as np
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from horus.db.models import Track
from horus.detect.anomaly import iforest_scores, pca_scores
from horus.detect.seq_anomaly import train_model
from horus.logging import configure_logging, get_logger

log = get_logger(__name__)


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    """Spearman rank correlation = Pearson on ranks.

    Implemented here rather than pulled from scipy: scipy is only a *transitive* dependency
    of scikit-learn, and depending on something we never declared is how builds break later.
    """
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    ra -= ra.mean()
    rb -= rb.mean()
    denom = float(np.sqrt((ra**2).sum() * (rb**2).sum()))
    return float((ra * rb).sum() / denom) if denom else float("nan")


@dataclass
class ModelScores:
    name: str
    per_seed: list[np.ndarray] = field(default_factory=list)
    transfer: list[np.ndarray] = field(default_factory=list)

    def stability(self) -> float:
        """Mean pairwise Spearman correlation between seeds (1.0 = identical ranking)."""
        if len(self.per_seed) < 2:
            return float("nan")
        rhos = [
            spearman(self.per_seed[i], self.per_seed[j])
            for i in range(len(self.per_seed))
            for j in range(i + 1, len(self.per_seed))
        ]
        return float(np.mean(rhos))

    def topk_overlap(self, k: int = 10) -> float:
        """Mean Jaccard overlap of the top-k sets across seeds."""
        if len(self.per_seed) < 2:
            return float("nan")
        tops = [set(np.argsort(-s)[:k].tolist()) for s in self.per_seed]
        js = [
            len(tops[i] & tops[j]) / len(tops[i] | tops[j])
            for i in range(len(tops))
            for j in range(i + 1, len(tops))
        ]
        return float(np.mean(js))

    def separation(self) -> float:
        """Top-decile error / median error, averaged over seeds (1.0 = no separation)."""
        vals = []
        for s in self.per_seed:
            med = float(np.median(s))
            if med > 0:
                vals.append(float(np.quantile(s, 0.9) / med))
        return float(np.mean(vals)) if vals else float("nan")

    def transfer_stability(self) -> float:
        if len(self.transfer) < 2:
            return float("nan")
        rhos = [
            spearman(self.transfer[i], self.transfer[j])
            for i in range(len(self.transfer))
            for j in range(i + 1, len(self.transfer))
        ]
        return float(np.mean(rhos))


def load_sequences(db_url: str, region: str | None = None) -> list[list[list[float]]]:
    engine = create_engine(db_url)
    with Session(engine) as s:
        q = select(Track).where(Track.sequence.is_not(None))
        if region:
            q = q.where(Track.region == region)
        return [t.sequence for t in s.scalars(q) if t.sequence]


def benchmark(
    train_seqs: list[list[list[float]]],
    transfer_seqs: list[list[list[float]]],
    *,
    seeds: tuple[int, ...] = (7, 13, 29, 41, 97),
    hidden: int = 8,
    epochs: int = 60,
) -> dict[str, ModelScores]:
    results = {
        "GRU": ModelScores("GRU sequence autoencoder"),
        "IsolationForest": ModelScores("Isolation Forest"),
        "PCA": ModelScores("linear PCA"),
    }
    train_feats = [list(np.asarray(s, dtype=float).reshape(-1)) for s in train_seqs]
    transfer_feats = [list(np.asarray(s, dtype=float).reshape(-1)) for s in transfer_seqs]

    for seed in seeds:
        model = train_model(train_seqs, hidden=hidden, epochs=epochs, seed=seed)
        results["GRU"].per_seed.append(model.errors(train_seqs))
        if transfer_seqs:
            results["GRU"].transfer.append(model.errors(transfer_seqs))

        results["IsolationForest"].per_seed.append(iforest_scores(train_feats, seed=seed))
        if transfer_feats:
            results["IsolationForest"].transfer.append(iforest_scores(transfer_feats, seed=seed))

        # PCA is deterministic; recording it per seed keeps the table shape uniform and
        # makes its perfect "stability" legible as determinism rather than as a win.
        results["PCA"].per_seed.append(pca_scores(train_feats))
        if transfer_feats:
            results["PCA"].transfer.append(pca_scores(transfer_feats))
        log.info("benchmark_seed_done", seed=seed)
    return results


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Decide the flagship model on real tracks")
    parser.add_argument("--train", required=True, help="SQLite path for the training region")
    parser.add_argument("--transfer", default=None, help="SQLite path for the transfer region")
    parser.add_argument("--epochs", type=int, default=60)
    args = parser.parse_args()

    train_seqs = load_sequences(f"sqlite:///{args.train}")
    transfer_seqs = load_sequences(f"sqlite:///{args.transfer}") if args.transfer else []
    print(f"\ntrain tracks: {len(train_seqs)}   transfer tracks: {len(transfer_seqs)}")
    if len(train_seqs) < 20:
        print("too few real tracks to decide anything; collect more first")
        return

    results = benchmark(train_seqs, transfer_seqs, epochs=args.epochs)
    print()
    header = (
        "| Model | Rank stability (Spearman) | Top-10 overlap | "
        "Separation (p90/median) | Transfer stability |"
    )
    print(header)
    print("|---|---:|---:|---:|---:|")
    for key in ("GRU", "IsolationForest", "PCA"):
        r = results[key]
        print(
            f"| {r.name} | {r.stability():.4f} | {r.topk_overlap():.2f} | "
            f"{r.separation():.2f} | {r.transfer_stability():.4f} |"
        )
    print()
    print(
        "No AUC: real tracks carry no anomaly labels, so accuracy is not measurable here and\n"
        "is not claimed. These measure whether a model is stable, transferable and\n"
        "discriminating — necessary conditions for trusting a ranking, not sufficient ones.\n"
        "PCA is deterministic, so its stability of 1.0 is a property of the algorithm rather\n"
        "than evidence of quality; read it against its separation."
    )


if __name__ == "__main__":
    main()
