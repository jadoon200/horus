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


@pytest.fixture(autouse=True)
def _fresh_rate_limit() -> Iterator[None]:
    """Give every test the full request budget.

    The limiter is a module-level singleton keyed by client host, and TestClient presents one
    host for the whole file — so the budget was shared across all tests and the module sat a
    couple of requests below the 30/min cap. Adding any new API test tipped the ones after it
    into 429s, which surface as a KeyError on the parsed body rather than anything that names
    the real cause.
    """
    from horus.api.app import _rate_limiter

    _rate_limiter._hits.clear()
    yield


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
    props = track["properties"]
    assert len(props["ts_series"]) == props["n"]
    assert len(props["nic_series"]) == props["n"]
    assert len(props["nac_p_series"]) == props["n"]
    assert client.get("/aircraft/zzzzzz/track").status_code == 404


def test_tracks_do_not_merge_positions_across_regions() -> None:
    """One ICAO24 can appear under two regions (a live slice and an OpenSky-research one).

    The track was gap-split within its own region, so its rendered geometry must contain only
    that region's fixes. Without the region filter the LineString merged both regions and
    zig-zagged across the map — the exact provenance mistake tracks/build.py warns about.

    Self-contained (its own DB): the shared fixture seeds synthetic data, and this needs one
    aircraft deliberately present in two regions at the same instants.
    """
    from datetime import UTC, datetime, timedelta

    from horus.db.models import Aircraft, Position, Track

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    base = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
    with Session(engine) as s:
        s.add(Aircraft(icao24="beef01"))
        for m in range(5):
            s.add(
                Position(
                    icao24="beef01",
                    ts=base + timedelta(minutes=m),
                    lat=1.30,
                    lon=103.80 + 0.01 * m,
                    alt_baro_ft=35000.0,
                    region="sg-live",
                )
            )
            s.add(  # same aircraft, same instants, a different region far away
                Position(
                    icao24="beef01",
                    ts=base + timedelta(minutes=m),
                    lat=54.90,
                    lon=20.50 + 0.01 * m,
                    alt_baro_ft=35000.0,
                    region="sg-opensky-research",
                )
            )
        s.add(
            Track(
                track_id="beef01:merge-test",
                icao24="beef01",
                region="sg-live",
                start_ts=base,
                end_ts=base + timedelta(minutes=4),
                point_count=5,
            )
        )
        s.commit()

    def _override() -> Iterator[Session]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as c:
            fc = c.get("/tracks").json()
    finally:
        # Restore, never blindly clear: the module-scoped `client` fixture's override must
        # survive for the other tests in this file.
        if previous is not None:
            app.dependency_overrides[get_db] = previous
        else:
            app.dependency_overrides.pop(get_db, None)

    feature = next(f for f in fc["features"] if f["properties"]["track_id"] == "beef01:merge-test")
    coords = feature["geometry"]["coordinates"]
    assert len(coords) == 5, "only the track's own region's fixes belong to it"
    assert all(lat < 10 for _lon, lat in coords), "no Baltic fixes may leak into an SG track"


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


def test_gnss_coverage_reports_three_states(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unscoreable sky must be served as its own state, not omitted or implied clear."""
    from horus.config import get_settings

    # Read the whole snapshot rather than a wall-clock window. The synthetic scenario is
    # anchored at a literal T0, so it eventually ages past even the endpoint's maximum
    # window (720 h) and the request returns nothing — which failed this test for a reason
    # unrelated to what it checks. Demo mode is the supported window-independent read.
    get_settings.cache_clear()
    monkeypatch.setenv("HORUS_DEMO_MODE", "true")
    data = client.get("/gnss-coverage").json()
    get_settings.cache_clear()
    monkeypatch.delenv("HORUS_DEMO_MODE", raising=False)

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


def test_demo_mode_serves_the_whole_snapshot(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """A baked demo has no rolling window; demo mode must show the whole snapshot.

    The synthetic seed's timestamps are weeks in the past, so a `now - hours` filter would
    return an empty map on the deployed demo. In demo mode /gnss-coverage ignores the window
    and /stats advertises demo_mode so the UI shows a snapshot banner rather than a staleness
    warning.
    """
    from horus.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("HORUS_DEMO_MODE", "true")

    # Live-mode window excludes the weeks-old synthetic seed entirely...
    get_settings.cache_clear()
    monkeypatch.delenv("HORUS_DEMO_MODE", raising=False)
    live = client.get("/gnss-coverage", params={"hours": 6}).json()

    get_settings.cache_clear()
    monkeypatch.setenv("HORUS_DEMO_MODE", "true")
    demo = client.get("/gnss-coverage", params={"hours": 6}).json()
    assert client.get("/stats").json()["demo_mode"] is True

    get_settings.cache_clear()
    monkeypatch.delenv("HORUS_DEMO_MODE", raising=False)

    # ...but demo mode shows it regardless of the window.
    assert demo["cells_total"] > 0
    assert demo["cells_total"] >= live["cells_total"]

    # And it says so. Echoing the requested `hours` back had the dashboard captioning the
    # baked snapshot "worst over 6 h" — an aggregation the demo never performed.
    assert demo["window_hours"] is None
    assert live["window_hours"] == 6


def _demo_track_points():  # type: ignore[no-untyped-def]
    """A short straight demo track as score-track input points."""
    from datetime import UTC, datetime, timedelta

    base = datetime(2026, 7, 24, tzinfo=UTC)
    return [
        {
            "lat": 1.30 + 0.01 * k,
            "lon": 103.8,
            "alt_ft": 35000.0,
            "ts": (base + timedelta(seconds=30 * k)).timestamp(),
        }
        for k in range(30)
    ]


def test_model_info_reports_source(client: TestClient) -> None:
    """/model must say whether a frozen artifact is served or it's a runtime fallback."""
    info = client.get("/model").json()
    assert info["flagship"] == "GRU sequence autoencoder"
    assert info["model_source"] in ("trained-artifact", "runtime-fallback")


def test_score_track_cors_preflight_allows_post(client: TestClient) -> None:
    response = client.options(
        "/score-track",
        headers={
            "origin": "http://localhost:5199",
            "access-control-request-method": "POST",
        },
    )
    assert response.status_code == 200
    assert "POST" in response.headers["access-control-allow-methods"]


def test_score_track_needs_a_frozen_model_and_is_deterministic(
    client: TestClient,
    tmp_path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """With a frozen artifact, the same track scores identically every call.

    Determinism is the whole point of freezing: a served model that retrained per request
    would give a different verdict each time. The route loads the pinned artifact and must
    reproduce its score exactly.
    """
    # Train a tiny artifact into a temp path and point the module at it.
    from datetime import UTC, datetime, timedelta

    from horus.detect import seq_anomaly
    from horus.detect.seq_anomaly import train_model
    from horus.tracks.build import sequence_from_points

    base = datetime(2026, 7, 24, tzinfo=UTC)
    seqs = [
        sequence_from_points(
            [
                (base + timedelta(seconds=30 * k), 1.3 + 0.01 * k + 0.001 * j, 103.8, 35000.0)
                for k in range(30)
            ]
        )
        for j in range(8)
    ]
    model = train_model(seqs, epochs=15)
    artifact = tmp_path / "model.pt"
    model.save(artifact)
    # The served path comes from settings so the deploy can point at its baked synthetic
    # model without the demo seed overwriting a real-data freeze at a shared filename.
    from horus.config import get_settings

    monkeypatch.setenv("HORUS_ANOMALY_ARTIFACT_PATH", str(artifact))
    get_settings.cache_clear()

    body = {"points": _demo_track_points()}
    r1 = client.post("/score-track", json=body).json()
    r2 = client.post("/score-track", json=body).json()
    assert r1["scored"] is True and r1["model_source"] == "trained-artifact"
    assert r1["reconstruction_error"] == r2["reconstruction_error"]  # deterministic
    assert "anomalous" in r1 and "population_threshold" in r1
    assert r1["artifact_sha256"] == seq_anomaly.artifact_sha256(artifact)

    # A configured pin is an enforcement control, not decorative metadata.
    from horus.config import get_settings

    monkeypatch.setenv("HORUS_ANOMALY_ARTIFACT_SHA256", "0" * 64)
    get_settings.cache_clear()
    try:
        blocked = client.post("/score-track", json=body).json()
        assert blocked["scored"] is False
        assert "does not match" in blocked["detail"]
        assert client.get("/model").json()["sha_matches_pin"] is False
    finally:
        monkeypatch.delenv("HORUS_ANOMALY_ARTIFACT_SHA256")
        monkeypatch.delenv("HORUS_ANOMALY_ARTIFACT_PATH", raising=False)
        get_settings.cache_clear()


def test_score_track_reports_fallback_without_an_artifact(
    client: TestClient,
    tmp_path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """No frozen model → say so explicitly, never fabricate a score."""
    from horus.config import get_settings

    monkeypatch.setenv("HORUS_ANOMALY_ARTIFACT_PATH", str(tmp_path / "absent.pt"))
    get_settings.cache_clear()
    try:
        r = client.post("/score-track", json={"points": _demo_track_points()}).json()
        assert r["scored"] is False and r["model_source"] == "runtime-fallback"
    finally:
        monkeypatch.delenv("HORUS_ANOMALY_ARTIFACT_PATH")
        get_settings.cache_clear()


def test_score_track_rejects_degenerate_input(client: TestClient) -> None:
    assert client.post("/score-track", json={"points": []}).status_code == 422
    one = {"points": [{"lat": 1.3, "lon": 103.8, "alt_ft": 35000.0, "ts": 0.0}]}
    assert client.post("/score-track", json=one).status_code == 422
    duplicate = {"points": [one["points"][0], one["points"][0]]}
    assert client.post("/score-track", json=duplicate).status_code == 422
    out_of_bounds = {"points": [{**p, "lat": 91.0} for p in _demo_track_points()[:2]]}
    assert client.post("/score-track", json=out_of_bounds).status_code == 422


def test_score_track_rejects_non_finite_coordinates_cleanly(client: TestClient) -> None:
    """A pasted track with an inf/NaN coordinate must be a clean 422, never a 500.

    The model already rejects non-finite floats (`allow_inf_nan=False`), but FastAPI's default
    validation-error body echoes the offending value — and inf/NaN aren't JSON-serializable, so
    the 422 itself failed to encode and surfaced as a 500. Sent as raw content because a JSON
    client can't serialize inf either.
    """
    for bad in ("1e999", "NaN", "-1e999"):
        body = (
            f'{{"points":[{{"lat":{bad},"lon":103.8,"ts":0}},{{"lat":1.4,"lon":103.9,"ts":30}}]}}'
        )
        resp = client.post(
            "/score-track", content=body, headers={"content-type": "application/json"}
        )
        assert resp.status_code == 422, f"{bad} lat should be a clean 422, got {resp.status_code}"
        # The 422 body itself must be valid JSON (the whole point of the fix).
        assert "detail" in resp.json()


def test_aircraft_track_does_not_merge_two_regions() -> None:
    """Two regions holding the same ICAO24 must not become one zig-zagging track.

    /tracks already guarded against this; its per-aircraft sibling did not, so a live slice
    and a backfilled or OpenSky-research slice of the same airframe were concatenated into
    one LineString with their NIC/NACp series interleaved — an integrity history that never
    happened.
    """
    from datetime import UTC, datetime, timedelta

    from horus.db.models import Aircraft, Position

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    base = datetime(2026, 7, 24, tzinfo=UTC)
    with Session(engine) as s:
        s.add(Aircraft(icao24="abc123"))
        for k in range(4):  # the live lane, over Singapore
            s.add(
                Position(
                    icao24="abc123",
                    ts=base + timedelta(seconds=30 * k),
                    lat=1.3,
                    lon=103.8,
                    nic=8,
                    nac_p=9,
                    region="sg-live",
                )
            )
        for k in range(3):  # a research slice of the same airframe, half a world away
            s.add(
                Position(
                    icao24="abc123",
                    ts=base + timedelta(seconds=15 * k),
                    lat=54.9,
                    lon=20.5,
                    nic=0,
                    nac_p=0,
                    region="baltic-research",
                )
            )
        s.commit()

    def _override() -> Iterator[Session]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    # The module-scoped `client` fixture owns this override; borrow it and give it back,
    # rather than clearing and stranding every later test in the file.
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _override
    try:
        c = TestClient(app)
        newest = c.get("/aircraft/abc123/track").json()
        assert newest["properties"]["region"] == "sg-live", "defaults to the newest region"
        assert newest["properties"]["n"] == 4
        assert all(lat == 1.3 for _lon, lat, _alt in newest["geometry"]["coordinates"])
        assert set(newest["properties"]["nic_series"]) == {8}

        other = c.get("/aircraft/abc123/track", params={"region": "baltic-research"}).json()
        assert other["properties"]["n"] == 3
        assert set(other["properties"]["nic_series"]) == {0}
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous
