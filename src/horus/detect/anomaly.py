"""Anomaly detection over stored tracks: flagship GRU + fair baselines + DB driver.

The baselines exist for the honest comparison the eval reports: a nonlinear Isolation
Forest and a linear PCA reconstruction over the same flattened features, under the same
unsupervised setup — the flagship must beat fair alternatives or lose its job (the
model-adoption discipline shared across the portfolio).
"""

from __future__ import annotations

import numpy as np
from sklearn.decomposition import PCA
from sklearn.ensemble import IsolationForest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from horus.db.models import Incident, Track
from horus.detect.base import TECH_ANOMALY, make_incident
from horus.detect.seq_anomaly import AnomalyModel, train_model
from horus.logging import get_logger

log = get_logger(__name__)

# Below this the train/val split has no meaningful validation partition; `train_model`
# enforces the same floor, so keep the two in step.
_MIN_TRACKS_TO_TRAIN = 5


def iforest_scores(features: list[list[float]], seed: int = 7) -> np.ndarray:
    """Isolation-Forest anomaly scores (higher = more anomalous)."""
    x = np.asarray(features, dtype=float)
    model = IsolationForest(random_state=seed, n_estimators=200)
    model.fit(x)
    return np.asarray(-model.score_samples(x))


def pca_scores(features: list[list[float]], n_components: int = 4) -> np.ndarray:
    """Linear PCA reconstruction error (higher = more anomalous)."""
    x = np.asarray(features, dtype=float)
    mean = x.mean(axis=0)
    std = np.maximum(x.std(axis=0), 1e-9)
    xn = (x - mean) / std
    pca = PCA(n_components=min(n_components, xn.shape[1], max(1, xn.shape[0] - 1)))
    z = pca.fit_transform(xn)
    recon = pca.inverse_transform(z)
    return np.asarray(((xn - recon) ** 2).mean(axis=1))


def detect_anomalies(
    session: Session, region: str | None = None, *, epochs: int = 60
) -> tuple[list[Incident], AnomalyModel | None]:
    """Train the flagship on all stored sequences (unsupervised), persist incidents.

    Returns `(incidents, model)`. The model is **None** when there were too few tracks to
    train on — the honest signal that no scoring happened. (This used to claim an
    `AnomalyModel` and return None behind a `type: ignore`, so a caller that touched the
    model on a thin corpus got an AttributeError instead of a type error.)
    """
    q = select(Track).where(Track.sequence.is_not(None))
    if region:
        q = q.where(Track.region == region)
    tracks = list(session.scalars(q))
    if len(tracks) < _MIN_TRACKS_TO_TRAIN:
        log.warning("detect_anomalies_skipped", tracks=len(tracks))
        return [], None

    sequences = [t.sequence for t in tracks if t.sequence is not None]
    model = train_model(sequences, epochs=epochs)
    errors = model.errors(sequences)

    session.execute(delete(Incident).where(Incident.detector == "anomaly"))
    incidents: list[Incident] = []
    for track, err in zip(tracks, errors, strict=True):
        if err <= model.threshold:
            continue
        # Score by how far past the population threshold the error lands.
        score = min(1.0, 0.5 + 0.5 * (float(err) / model.threshold - 1.0))
        incidents.append(
            make_incident(
                incident_id=f"anomaly:{track.track_id}",
                detector="anomaly",
                incident_type="trajectory anomaly",
                score=score,
                reliability="C" if track.point_count >= 30 else "D",
                ts_start=track.start_ts,
                ts_end=track.end_ts,
                lat=track.end_lat,
                lon=track.end_lon,
                icao24=track.icao24,
                track_id=track.track_id,
                techniques=[TECH_ANOMALY],
                evidence={
                    "reconstruction_error": float(err),
                    "population_threshold": model.threshold,
                    "point_count": track.point_count,
                    "caveat": "unusual shape, not a verdict — e.g. survey/patrol patterns",
                },
                region=track.region or region,
            )
        )
    session.add_all(incidents)
    log.info("detect_anomalies", tracks=len(tracks), incidents=len(incidents))
    return incidents, model
