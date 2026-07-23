"""Deterministic detector runner — rebuild all rule-based incidents.

    python -m horus.detect.run [--region LABEL]

Rebuilds jamming / gap / incursion / spoof incidents (the GRU anomaly path has its own
driver in `anomaly.detect_anomalies`, which persists under detector="anomaly").
"""

from __future__ import annotations

import argparse

from sqlalchemy import delete
from sqlalchemy.orm import Session

from horus.db.base import session_scope
from horus.db.models import Incident
from horus.detect.gaps import detect_gaps
from horus.detect.incursion import detect_incursions
from horus.detect.jamming import JammingRunStats, detect_jamming
from horus.detect.spoof import detect_spoof
from horus.logging import configure_logging, get_logger

log = get_logger(__name__)

_DETERMINISTIC = ("jamming", "gap", "incursion", "spoof")


def run_detectors(session: Session, region: str | None = None) -> JammingRunStats:
    """Full rebuild of the deterministic detectors' incidents; returns jamming stats."""
    session.execute(delete(Incident).where(Incident.detector.in_(_DETERMINISTIC)))
    jam_incidents, jam_stats = detect_jamming(session, region)
    session.add_all(jam_incidents)
    session.add_all(detect_gaps(session, region))
    session.add_all(detect_incursions(session, region))
    session.add_all(detect_spoof(session, region))
    return jam_stats


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Rebuild deterministic incidents")
    parser.add_argument("--region", default=None)
    args = parser.parse_args()
    with session_scope() as session:
        run_detectors(session, args.region)


if __name__ == "__main__":
    main()
