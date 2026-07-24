from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from horus.api.app import app, get_db
from horus.db.base import Base
from horus.detect.run import run_detectors
from horus.ingest.synthetic import generate, seed_db


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with Session(engine) as s:
        seed_db(s, generate())
        run_detectors(s)
        s.commit()

    def _override() -> Iterator[Session]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_health_and_stats(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"
    stats = client.get("/stats").json()
    assert stats["aircraft"] > 0 and stats["incidents"] > 0
    assert stats["incidents_by_detector"]["jamming"] > 0


def test_incident_listing_and_detail(client: TestClient) -> None:
    incidents = client.get("/incidents", params={"detector": "jamming"}).json()
    assert incidents and all(i["detector"] == "jamming" for i in incidents)
    assert all(i["icao24"] is None for i in incidents)  # area-level
    detail = client.get(f"/incidents/{incidents[0]['incident_id']}").json()
    assert detail["evidence"]["aircraft_degraded"] >= 3
    assert client.get("/incidents/nope").status_code == 404


def test_geojson_endpoints(client: TestClient) -> None:
    zones = client.get("/zones").json()
    assert zones["type"] == "FeatureCollection" and zones["features"]
    ring = zones["features"][0]["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1], "GeoJSON polygon rings must be closed"

    track = client.get("/aircraft/d00001/track").json()
    assert track["geometry"]["type"] == "LineString"
    assert len(track["properties"]["nic_series"]) == track["properties"]["n"]
    assert client.get("/aircraft/zzzzzz/track").status_code == 404


def test_air_picture_and_evidence_shape(client: TestClient) -> None:
    picture = client.get("/air-picture").json()
    assert picture["areas"] and picture["aircraft"]

    evidence = client.get("/geoint/evidence").json()
    assert evidence
    item = evidence[0]
    # The ARGUS EvidenceItem contract: these fields must exist with these shapes.
    for key in ("doc_id", "title", "source", "reliability", "credibility", "summary", "url"):
        assert key in item
    assert item["source"] == "HORUS air domain awareness"
    assert item["reliability"] in "ABCDEF" and 1 <= item["credibility"] <= 6
    assert "Human-review" in item["summary"]


def test_gnss_coverage_reports_three_states(client: TestClient) -> None:
    """Unscoreable sky must be served as its own state, not omitted or implied clear."""
    data = client.get("/gnss-coverage", params={"hours": 720}).json()
    assert data["cells_total"] == data["cells_scoreable"] + data["cells_unscoreable"]
    assert data["cells_unscoreable"] > 0, "the scenario has sparse sky; it must be reported"
    assert data["min_aircraft"] >= 1

    by_state = {c["scoreable"] for c in data["cells"]}
    assert by_state == {True, False}
    for c in data["cells"]:
        if c["scoreable"]:
            # A scoreable cell always carries a fraction; it is never null.
            assert c["degraded_fraction"] is not None
            assert c["aircraft_observed"] >= data["min_aircraft"]
        else:
            # An unscoreable cell must NOT report a fraction — that would invite a reader
            # to treat "no data" as "0% degraded", which is the exact lie to avoid.
            assert c["degraded_fraction"] is None
            assert c["aircraft_observed"] < data["min_aircraft"]


def test_stats_exposes_collection_freshness(client: TestClient) -> None:
    """A dashboard that looks live while the collector is dead is worse than one that admits it."""
    stats = client.get("/stats").json()
    assert stats["newest_report"] is not None
    assert stats["data_age_seconds"] is not None and stats["data_age_seconds"] >= 0
