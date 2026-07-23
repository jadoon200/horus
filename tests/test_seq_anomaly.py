import math

import numpy as np

from horus.detect.anomaly import iforest_scores, pca_scores
from horus.detect.seq_anomaly import load_model, train_model


def _straight(n: int = 63, step: float = 5.0, jitter_seed: int = 0) -> list[list[float]]:
    rng = np.random.default_rng(jitter_seed)
    return [[step + float(rng.normal(0, 0.05)), 0.1, step, 0.0, 0.0] for _ in range(n)]


def _orbit(n: int = 63, radius_km: float = 9.0) -> list[list[float]]:
    seq = []
    for i in range(n):
        ang = 0.35 * i
        seq.append(
            [
                radius_km * 0.35 * math.cos(ang),
                -radius_km * 0.35 * math.sin(ang),
                radius_km * 0.35,
                0.35,
                0.0,
            ]
        )
    return seq


def test_gru_ranks_orbit_above_straight_population(tmp_path) -> None:  # type: ignore[no-untyped-def]
    population = [_straight(jitter_seed=i) for i in range(20)]
    orbit = _orbit()
    model = train_model(population, epochs=30, seed=7)
    errors = model.errors([*population[:5], orbit])
    assert errors[-1] > max(errors[:5]), "the orbit must reconstruct worse than the population"

    # Artifact round-trip: saved and loaded models must score identically.
    path = tmp_path / "model.pt"
    model.save(path)
    loaded = load_model(path)
    assert loaded is not None
    assert np.allclose(loaded.errors([orbit]), model.errors([orbit]))
    assert loaded.threshold == model.threshold


def test_baselines_run_on_flattened_features() -> None:
    feats = [list(np.asarray(_straight(jitter_seed=i)).reshape(-1)) for i in range(10)]
    feats.append(list(np.asarray(_orbit()).reshape(-1)))
    if_scores = iforest_scores(feats)
    assert len(if_scores) == 11
    assert if_scores[-1] == max(if_scores), "IF must isolate the orbit"
    # PCA is the honest *negative* baseline: a dominant outlier drags the principal
    # components toward itself, so linear PCA reconstructs it WELL and misses it —
    # the same recorded failure as PHAROS's maritime PCA baseline. Assert the failure
    # mode explicitly so a future "fix" that silently changes it gets noticed.
    pca = pca_scores(feats)
    assert len(pca) == 11
    assert pca[-1] < max(pca), "PCA absorbing the outlier is the documented failure mode"
