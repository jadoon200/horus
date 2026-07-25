from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from horus.db.base import Base
from horus.db.models import Aircraft, Position
from horus.detect.squawk import detect_squawks

T0 = datetime(2026, 7, 25, 0, 0, tzinfo=UTC)


def _position(icao24: str, seconds: int, squawk: str) -> Position:
    return Position(
        icao24=icao24,
        ts=T0 + timedelta(seconds=seconds),
        lat=1.3,
        lon=103.8,
        alt_baro_ft=35_000.0,
        squawk=squawk,
        region="sg",
    )


def test_repeated_emergency_codes_form_time_scoped_visits() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([Aircraft(icao24="abc123"), Aircraft(icao24="normal")])
        session.add_all(
            [
                _position("abc123", 0, "7700"),
                _position("abc123", 30, "7700"),
                _position("abc123", 60, "2000"),  # ends the first visit
                _position("abc123", 90, "7700"),
                _position("abc123", 120, "7700"),  # second visit, same code
                _position("abc123", 150, "7600"),  # one sample: below confirmation floor
                _position("normal", 0, "2000"),
                _position("normal", 30, "2000"),
            ]
        )
        session.commit()

        incidents = detect_squawks(session, "sg")

    assert len(incidents) == 2
    assert len({incident.incident_id for incident in incidents}) == 2
    assert all(incident.incident_id.startswith("squawk:abc123:7700:") for incident in incidents)
    assert all(incident.reliability == "C" for incident in incidents)
    assert all(incident.severity == "moderate" for incident in incidents)
    assert all((incident.evidence or {})["samples"] == 2 for incident in incidents)


def test_incremental_scope_preserves_the_true_visit_start() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Aircraft(icao24="abc123"))
        session.add_all(
            [
                _position("abc123", 0, "7700"),
                _position("abc123", 30, "7700"),
                _position("abc123", 60, "7700"),
            ]
        )
        session.commit()

        full = detect_squawks(session, "sg")
        scoped = detect_squawks(session, "sg", since=T0 + timedelta(seconds=20))

    assert len(full) == len(scoped) == 1
    assert full[0].incident_id == scoped[0].incident_id
