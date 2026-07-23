from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from horus.db.base import Base
from horus.db.models import Aircraft, Incident, Position


def _session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_position_roundtrip_with_integrity_fields() -> None:
    with _session() as s:
        s.add(Aircraft(icao24="76ceef", callsign="SIA321", type_code="A359"))
        s.add(
            Position(
                icao24="76ceef",
                ts=datetime(2026, 7, 23, 4, 0, tzinfo=UTC),
                lat=1.31,
                lon=103.95,
                alt_baro_ft=12000.0,
                gs_kt=310.0,
                nic=8,
                nac_p=9,
                sil=3,
                source="adsb-lol",
                region="sg",
                raw={"hex": "76ceef"},
            )
        )
        s.commit()
        row = s.scalars(select(Position)).one()
        assert (row.nic, row.nac_p, row.sil) == (8, 9, 3)
        assert row.raw == {"hex": "76ceef"}
        assert row.on_ground is False


def test_area_level_incident_needs_no_aircraft() -> None:
    # GNSS-interference incidents are area-level: icao24 stays NULL, the evidence carries
    # the affected aircraft. The schema must allow that.
    with _session() as s:
        s.add(
            Incident(
                incident_id="jam-cell-1",
                icao24=None,
                detector="jamming",
                incident_type="GNSS interference",
                score=0.8,
                severity="high",
                reliability="C",
                ts_start=datetime(2026, 7, 23, 4, 0, tzinfo=UTC),
                lat=1.2,
                lon=103.8,
                affected_count=7,
                evidence={"bad_fraction": 0.7, "aircraft": ["76ceef", "76cf01"]},
                techniques=["AT-JAM"],
                region="sg",
            )
        )
        s.commit()
        row = s.scalars(select(Incident)).one()
        assert row.icao24 is None and row.affected_count == 7
        assert row.evidence is not None and row.evidence["bad_fraction"] == 0.7
