"""Flagship trajectory-anomaly model — a compact GRU sequence autoencoder.

Learns the pattern of life from *unlabeled* track sequences (the realistic setup: nobody
hand-labels live traffic) and scores each track by reconstruction error — a track whose
ordered shape the model can't reproduce is far from the population's pattern. Discipline
inherited from PHAROS's audited version: train/val split, TRAIN-ONLY normalization (the
leakage lesson), early stopping, and a persisted artifact (weights + scaler + threshold)
so batch detection and API inference score with exactly the same model.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import torch
from torch import Tensor, nn

N_FEATURES = 5  # [dlat_km, dlon_km, step_len_km, turn_rad, dalt_kft]

# Below this, a channel's training spread is indistinguishable from "it never moved".
_DEGENERATE_STD = 1e-6


def _train_scale(std: np.ndarray) -> np.ndarray:
    """Per-channel divisor for the frozen scaler, with dead channels neutralised.

    A channel that never varied in training carries no information, and the scaler is
    *persisted* — it is later applied to tracks the training population never contained.
    Clamping its divisor to a tiny epsilon does not neutralise such a channel, it turns it
    into a ~1e6 amplifier: the synthetic demo population holds altitude exactly constant, so
    the baked model divided `dalt_kft` by 1e-6 and scored an ordinary 2,000 ft descent at a
    reconstruction error of 1.9e8 against a threshold of 13 — "anomalous" by a factor of ten
    million, on the most routine thing an aircraft does.

    Unit scale is the honest divisor instead. Training data is *at* the mean on a dead
    channel, so it still normalises to zero and neither the network nor the threshold moves;
    a scored track that does vary there now contributes in its own units rather than an
    exploded one.
    """
    return np.where(std < _DEGENERATE_STD, 1.0, std)


def artifact_sha256(path: Path | None = None) -> str | None:
    """SHA-256 of the frozen artifact file, or None if it does not exist.

    The pin the deploy records so a served model is provably the benchmarked one — the
    maritime sibling's freeze discipline. A drift between the served SHA and the recorded
    one is visible rather than silent. The default path resolves at call time so an override
    is honoured.
    """
    path = path or artifact_path()
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_path() -> Path:
    """The frozen artifact this process loads and saves.

    Resolved from settings at call time rather than fixed at import, because two different
    models legitimately live in one checkout: the real-data freeze (SG-AIR-v1) and the
    synthetic one the demo bakes. They shared a hard-coded path, so seeding the demo locally
    silently overwrote the recorded freeze. The demo seeder now writes its own path and the
    deploy points `HORUS_ANOMALY_ARTIFACT_PATH` at it.
    """
    from horus.config import get_settings

    return get_settings().anomaly_artifact_path


class GRUAutoencoder(nn.Module):
    def __init__(self, n_features: int = N_FEATURES, hidden: int = 8) -> None:
        super().__init__()
        self.encoder = nn.GRU(n_features, hidden, batch_first=True)
        self.decoder = nn.GRU(hidden, hidden, batch_first=True)
        self.head = nn.Linear(hidden, n_features)

    def forward(self, x: Tensor) -> Tensor:
        _, h = self.encoder(x)  # h: (1, B, H) — the track compressed to one latent
        latent = h[-1].unsqueeze(1).expand(-1, x.shape[1], -1)  # repeat along time
        out, _ = self.decoder(latent)
        recon: Tensor = self.head(out)
        return recon


@dataclass
class AnomalyModel:
    """The persisted scoring artifact: model + train-only scaler + threshold."""

    net: GRUAutoencoder
    mean: np.ndarray
    std: np.ndarray
    threshold: float
    hidden: int

    def errors(self, sequences: list[list[list[float]]]) -> np.ndarray:
        """Per-track mean reconstruction error under the frozen scaler."""
        self.net.eval()
        out = np.zeros(len(sequences))
        with torch.no_grad():
            for i, seq in enumerate(sequences):
                x = (np.asarray(seq, dtype=np.float32) - self.mean) / self.std
                xt = torch.from_numpy(x.astype(np.float32)).unsqueeze(0)
                recon = self.net(xt)
                out[i] = float(((recon - xt) ** 2).mean())
        return out

    def save(self, path: Path | None = None) -> None:
        # Resolve the default at call time, not at def time, so a configured path
        # is honoured rather than frozen into the signature.
        path = path or artifact_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(
            {
                "state_dict": self.net.state_dict(),
                "mean": self.mean,
                "std": self.std,
                "threshold": self.threshold,
                "hidden": self.hidden,
                "n_features": N_FEATURES,
            },
            path,
        )


def load_model(path: Path | None = None) -> AnomalyModel | None:
    path = path or artifact_path()
    if not path.exists():
        return None
    blob: dict[str, Any] = torch.load(path, weights_only=False)
    net = GRUAutoencoder(int(blob["n_features"]), int(blob["hidden"]))
    net.load_state_dict(blob["state_dict"])
    return AnomalyModel(
        net=net,
        mean=np.asarray(blob["mean"]),
        std=np.asarray(blob["std"]),
        threshold=float(blob["threshold"]),
        hidden=int(blob["hidden"]),
    )


def train_model(
    sequences: list[list[list[float]]],
    *,
    hidden: int = 8,
    epochs: int = 60,
    val_frac: float = 0.2,
    seed: int = 7,
    patience: int = 8,
    lr: float = 1e-2,
) -> AnomalyModel:
    """Unsupervised training with a val split, train-only normalization, early stopping."""
    if len(sequences) < 5:
        raise ValueError("need at least 5 tracks to train")
    rng = np.random.default_rng(seed)
    torch.manual_seed(seed)
    order = rng.permutation(len(sequences))
    n_val = max(1, int(len(sequences) * val_frac))
    val_idx = set(order[:n_val].tolist())
    train = [sequences[i] for i in range(len(sequences)) if i not in val_idx]
    val = [sequences[i] for i in sorted(val_idx)]

    # Normalization fitted on the TRAIN partition only (the PHAROS leakage lesson).
    flat = np.concatenate([np.asarray(s, dtype=np.float64) for s in train])
    mean = flat.mean(axis=0)
    std = _train_scale(flat.std(axis=0))

    def to_tensor(batch: list[list[list[float]]]) -> Tensor:
        arr = np.stack([(np.asarray(s, dtype=np.float32) - mean) / std for s in batch])
        return torch.from_numpy(arr.astype(np.float32))

    net = GRUAutoencoder(N_FEATURES, hidden)
    opt = torch.optim.Adam(net.parameters(), lr=lr)
    x_train, x_val = to_tensor(train), to_tensor(val)
    best_val = float("inf")
    best_state = {k: v.clone() for k, v in net.state_dict().items()}
    since_best = 0
    for _ in range(epochs):
        net.train()
        opt.zero_grad()
        loss = ((net(x_train) - x_train) ** 2).mean()
        loss.backward()
        opt.step()
        net.eval()
        with torch.no_grad():
            val_loss = float(((net(x_val) - x_val) ** 2).mean())
        if val_loss < best_val - 1e-6:
            best_val = val_loss
            best_state = {k: v.clone() for k, v in net.state_dict().items()}
            since_best = 0
        else:
            since_best += 1
            if since_best >= patience:
                break
    net.load_state_dict(best_state)

    model = AnomalyModel(net=net, mean=mean, std=std, threshold=0.0, hidden=hidden)
    train_errors = model.errors(train)
    model.threshold = float(np.quantile(train_errors, 0.99))
    return model
