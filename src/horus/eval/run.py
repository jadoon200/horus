"""Honest eval harness — score every detector against the synthetic gold set.

    python -m horus.eval.run

Reports per-detector precision/recall against the injected labels, the confounder
outcomes (did the benign traps stay quiet?), and the anomaly-model comparison (flagship
GRU vs Isolation Forest vs linear PCA, same unsupervised setup). Writes the AUTO-EVAL
block in docs/EVAL.md.

HONESTY: the gold set is generated — injected events are separable *by construction*, so
these numbers are a CEILING, never a capability claim. The number that counts comes from
real collected data (the live Singapore lane), where the informative result is expected
to be false-positive pressure and coverage confounds, exactly as PHAROS found at sea.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from sklearn.metrics import roc_auc_score
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from horus.db.base import Base
from horus.db.models import Incident, Track
from horus.detect.anomaly import iforest_scores, pca_scores
from horus.detect.run import run_detectors
from horus.detect.seq_anomaly import train_model
from horus.geo import haversine_km
from horus.ingest.synthetic import GoldLabel, SyntheticData, generate, seed_db
from horus.logging import configure_logging, get_logger
from horus.timeutil import utc_naive
from horus.tracks.build import build_tracks

log = get_logger(__name__)

_EVAL_MD = Path(__file__).resolve().parents[3] / "docs" / "EVAL.md"
_JAM_MATCH_DEG = 0.8


def _overlaps(label: GoldLabel, inc: Incident) -> bool:
    inc_end = utc_naive(inc.ts_end) if inc.ts_end else utc_naive(inc.ts_start)
    return utc_naive(inc.ts_start) <= utc_naive(label.ts_end) and inc_end >= utc_naive(
        label.ts_start
    )


def _score_per_aircraft(
    labels: list[GoldLabel], incidents: list[Incident], kind: str
) -> dict[str, Any]:
    want = {label.icao24 for label in labels if label.kind == kind}
    got = {i.icao24 for i in incidents if i.detector == kind or i.detector == _DETECTOR[kind]}
    tp = len(want & got)
    return {
        "expected": sorted(x for x in want if x),
        "detected": sorted(x for x in got if x),
        "recall": tp / len(want) if want else None,
        "precision": tp / len(got) if got else None,
        "false_positives": sorted(x for x in (got - want) if x),
    }


_DETECTOR = {"gap": "gap", "incursion": "incursion", "spoof": "spoof", "anomaly": "anomaly"}


def evaluate(*, epochs: int = 60, seed: int = 7) -> dict[str, Any]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    results: dict[str, Any] = {}
    with Session(engine) as session:
        data: SyntheticData = generate(seed=seed)
        seed_db(session, data)
        session.commit()
        n_tracks = build_tracks(session)
        session.commit()
        jam_stats = run_detectors(session)
        session.commit()
        incidents = list(session.scalars(select(Incident)))

        # --- flagship jamming: match the labelled cell by proximity + time overlap ------
        jam_labels = [label for label in data.labels if label.kind == "jamming"]
        jam_incidents = [i for i in incidents if i.detector == "jamming"]
        matched = [
            label
            for label in jam_labels
            if any(
                i.lat is not None
                and label.lat is not None
                and haversine_km(label.lat, label.lon or 0, i.lat, i.lon or 0)
                <= _JAM_MATCH_DEG * 111.0
                and _overlaps(label, i)
                for i in jam_incidents
            )
        ]
        false_cells = [
            i.incident_id
            for i in jam_incidents
            if not any(
                label.lat is not None
                and i.lat is not None
                and haversine_km(label.lat, label.lon or 0, i.lat, i.lon or 0)
                <= _JAM_MATCH_DEG * 111.0
                and _overlaps(label, i)
                for label in jam_labels
            )
        ]
        results["jamming"] = {
            "labelled_events": len(jam_labels),
            "detected": len(matched),
            "incident_cells": len(jam_incidents),
            "cells_outside_label": false_cells,
            "cells_unscoreable": jam_stats.cells_unscoreable,
            "cells_seen": jam_stats.cells_seen,
        }

        # --- per-aircraft deterministic detectors --------------------------------------
        for kind in ("gap", "incursion", "spoof"):
            results[kind] = _score_per_aircraft(data.labels, incidents, kind)

        # --- anomaly: flagship vs fair baselines (unsupervised, ranked by AUC) ---------
        tracks = [t for t in session.scalars(select(Track)) if t.sequence is not None]
        anomalous = {label.icao24 for label in data.labels if label.kind == "anomaly"}
        y = np.array([1 if t.icao24 in anomalous else 0 for t in tracks])
        sequences = [t.sequence for t in tracks if t.sequence is not None]
        features = [t.features for t in tracks if t.features is not None]
        model = train_model(sequences, epochs=epochs, seed=seed)
        results["anomaly"] = {
            "tracks": n_tracks,
            "positives": int(y.sum()),
            "auc_gru": round(float(roc_auc_score(y, model.errors(sequences))), 4),
            "auc_iforest": round(float(roc_auc_score(y, iforest_scores(features))), 4),
            "auc_pca": round(float(roc_auc_score(y, pca_scores(features))), 4),
        }

        # --- confounder traps: the benign injections must stay quiet -------------------
        gap_fp_lowalt = [
            i.icao24
            for i in incidents
            if i.detector == "gap" and i.icao24 and i.icao24.startswith("c")
        ]
        jam_fp_lone_dip = [
            i.incident_id
            for i in jam_incidents
            if i.evidence and "b00d1f" in (i.evidence.get("worst_nic_by_aircraft") or {})
        ]
        results["confounders"] = {
            "low_altitude_dropout_flagged_as_gap": gap_fp_lowalt,  # must be []
            "lone_nic_dip_in_jamming_incident": jam_fp_lone_dip,  # must be []
        }
    return results


def _render(results: dict[str, Any]) -> str:
    jam = results["jamming"]
    anom = results["anomaly"]
    conf = results["confounders"]
    lines = [
        "<!-- AUTO-EVAL:BEGIN -->",
        "## Synthetic gold-set results (auto-generated — a CEILING, not a capability claim)",
        "",
        "Injected events are separable by construction; the informative read is the",
        "*confounder* rows (did benign traps stay quiet?) and the baseline gaps.",
        "",
        f"- **GNSS interference (flagship):** {jam['detected']}/{jam['labelled_events']} "
        f"labelled events detected; {jam['incident_cells']} incident cell-windows, "
        f"{len(jam['cells_outside_label'])} outside the labelled area; "
        f"{jam['cells_unscoreable']}/{jam['cells_seen']} cells unscoreable "
        "(too few aircraft — skipped honestly, never scored).",
    ]
    for kind in ("gap", "incursion", "spoof"):
        r = results[kind]
        lines.append(
            f"- **{kind}:** recall {r['recall']}, precision {r['precision']} "
            f"(expected {r['expected']}, detected {r['detected']}, FP {r['false_positives']})"
        )
    lines += [
        f"- **Trajectory anomaly ({anom['tracks']} tracks, {anom['positives']} injected):** "
        f"GRU AUC {anom['auc_gru']} vs Isolation Forest {anom['auc_iforest']} "
        f"vs linear PCA {anom['auc_pca']} (same unsupervised setup).",
        f"- **Confounders:** low-altitude dropouts flagged as gaps: "
        f"{conf['low_altitude_dropout_flagged_as_gap'] or 'none'}; lone NIC dip in a jamming "
        f"incident: {conf['lone_nic_dip_in_jamming_incident'] or 'none'}.",
        "<!-- AUTO-EVAL:END -->",
    ]
    return "\n".join(lines)


def write_eval_md(results: dict[str, Any]) -> None:
    block = _render(results)
    if _EVAL_MD.exists():
        text = _EVAL_MD.read_text()
        begin, end = "<!-- AUTO-EVAL:BEGIN -->", "<!-- AUTO-EVAL:END -->"
        if begin in text and end in text:
            head = text.split(begin)[0]
            tail = text.split(end)[1]
            _EVAL_MD.write_text(head + block + tail)
            return
        _EVAL_MD.write_text(text.rstrip() + "\n\n" + block + "\n")
    else:
        _EVAL_MD.write_text(
            "# HORUS evaluation\n\n"
            "Every claim here is reported on the number that survives scrutiny; the\n"
            "synthetic block below is explicitly a ceiling. Real-data results from the\n"
            "live Singapore collection lane land in this file as they are measured.\n\n"
            + block
            + "\n"
        )


def main() -> None:
    configure_logging()
    results = evaluate()
    write_eval_md(results)
    log.info("eval_complete", **{k: v for k, v in results.items() if k != "confounders"})
    print(_render(results))


if __name__ == "__main__":
    main()
