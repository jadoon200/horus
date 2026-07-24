from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from horus.db.base import Base
from horus.db.models import Incident
from horus.detect.ensemble import air_picture
from horus.detect.run import run_detectors
from horus.ingest.synthetic import JAM_CENTER, generate, seed_db


def _detected(session: Session) -> list[Incident]:
    return list(session.scalars(select(Incident)))


def _seeded_session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    seed_db(s, generate())
    s.commit()
    return s


def test_detector_battery_on_gold_scenario() -> None:
    with _seeded_session() as s:
        stats = run_detectors(s)
        s.commit()
        incidents = _detected(s)

        # --- jamming: the labelled cell fires, area-level, near the centre --------------
        jams = [i for i in incidents if i.detector == "jamming"]
        assert jams, "the jamming cell must be detected"
        assert all(i.icao24 is None for i in jams)
        near = [
            i
            for i in jams
            if i.lat is not None
            and abs(i.lat - JAM_CENTER[0]) < 0.8
            and abs((i.lon or 0) - JAM_CENTER[1]) < 0.8
        ]
        assert near, "jamming incidents must localize to the injected cell"
        best = max(near, key=lambda i: i.score)
        assert best.evidence is not None
        assert best.evidence["aircraft_degraded"] >= 3
        # The lone benign NIC dip must not appear in any jamming incident.
        for i in jams:
            assert "b00d1f" not in (i.evidence or {}).get("worst_nic_by_aircraft", {})
        # Small-sample honesty is exercised: some cells were unscoreable.
        assert stats.cells_unscoreable > 0

        # --- dark aircraft: both injected, no low-altitude confounder ------------------
        gaps = {i.icao24 for i in incidents if i.detector == "gap"}
        assert {"d00001", "d00002"} <= gaps
        assert not any(x and x.startswith("c") for x in gaps), (
            "low-altitude coverage dropouts must not be flagged dark"
        )

        # --- incursion: the low-level border track, not the cruise population ----------
        inc = {i.icao24 for i in incidents if i.detector == "incursion"}
        assert "e00001" in inc
        assert not any(x and x.startswith("a") for x in inc)

        # --- spoof: exactly the teleporting identity -----------------------------------
        spoofs = {i.icao24 for i in incidents if i.detector == "spoof"}
        assert spoofs == {"f00bad"}


def test_air_picture_rollup_shape() -> None:
    with _seeded_session() as s:
        run_detectors(s)
        s.commit()
        picture = air_picture(s)
        assert picture["areas"], "area-level jamming incidents belong in the picture"
        assert picture["aircraft"], "per-aircraft rollups must exist"
        top = picture["aircraft"][0]
        # Transparent risk arithmetic: the breakdown must reconstruct the headline number.
        b = top["risk_breakdown"]
        assert (
            abs(
                min(
                    1.0,
                    b["best_incident_score"] + b["agreement_bonus"] + b["sensitive_zone_bonus"],
                )
                - top["risk"]
            )
            < 1e-6
        )
        risks = [row["risk"] for row in picture["aircraft"]]
        assert risks == sorted(risks, reverse=True)


def test_rerun_is_idempotent() -> None:
    with _seeded_session() as s:
        run_detectors(s)
        s.commit()
        n1 = len(_detected(s))
        run_detectors(s)
        s.commit()
        assert len(_detected(s)) == n1


def test_jamming_ids_do_not_depend_on_what_else_is_in_the_corpus() -> None:
    """Time buckets must be anchored to a fixed epoch, not the corpus minimum.

    Anchoring to "the earliest report we happen to hold" made bucket boundaries — and so
    incident ids and even the incident COUNT — shift whenever earlier data arrived. A live
    collector accumulates continuously, so that produced unstable ids and duplicated
    incidents run over run. Reproduced before the fix: one unrelated report 7 minutes
    earlier turned 3 incidents into 2 and renumbered every window.
    """
    from datetime import timedelta

    from horus.detect.jamming import detect_jamming
    from horus.ingest.adsb import AdsbSample
    from horus.ingest.synthetic import SyntheticData

    def ids_with(extra_earlier: bool) -> list[str]:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            data = generate()
            samples = list(data.samples)
            if extra_earlier:
                first = samples[0]
                samples.insert(
                    0,
                    AdsbSample(
                        **{
                            **first.__dict__,
                            "icao24": "ffff01",
                            "ts": first.ts - timedelta(minutes=7),
                        }
                    ),
                )
            seed_db(s, SyntheticData(samples=samples, labels=data.labels))
            s.commit()
            incidents, _ = detect_jamming(s)
            return sorted(i.incident_id for i in incidents)

    baseline = ids_with(False)
    assert baseline, "the scenario must produce jamming incidents for this to test anything"
    assert ids_with(True) == baseline


def test_incidents_carry_the_data_region_not_the_query_filter() -> None:
    """An unscoped run must still attribute incidents to where the data came from.

    Detectors used to stamp `region=` from the *query filter*, so a full rebuild
    (`run_detectors(session)` with no region) produced incidents with region NULL even
    though every position was tagged — and `/air-picture?region=...` then returned nothing
    for its own data.
    """
    with _seeded_session() as s:
        run_detectors(s)
        s.commit()
        incidents = _detected(s)
        assert incidents
        assert {i.region for i in incidents} == {"synthetic"}


def test_hard_loss_tier_requires_both_integrity_channels() -> None:
    """NIC 0 corroborated by NACp 0 is the sharp signature; NIC alone is the wider band.

    Measured over real interference (Baltic, 2026-07-23): 18 of 20 degraded aircraft sat at
    exactly NIC 0 and NACp collapsed with them, while healthy traffic never reported NACp
    below 8. So the hard-loss tier must count only aircraft whose *second* channel agrees.
    """
    from datetime import UTC, datetime, timedelta

    from horus.db.models import Aircraft, Position
    from horus.detect.jamming import detect_jamming

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    base = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    with Session(engine) as s:
        # Six aircraft in one cell: three in total integrity loss (both channels at 0),
        # two merely degraded on NIC while NACp stays healthy, one clean.
        spec = [
            ("aa0001", 0, 0),
            ("aa0002", 0, 0),
            ("aa0003", 0, 0),
            ("aa0004", 4, 9),
            ("aa0005", 5, 10),
            ("aa0006", 8, 10),
        ]
        for icao, nic, nac in spec:
            s.add(Aircraft(icao24=icao))
            for k in range(3):
                s.add(
                    Position(
                        icao24=icao,
                        ts=base + timedelta(minutes=k),
                        lat=54.9,
                        lon=20.5,
                        alt_baro_ft=35000.0,
                        nic=nic,
                        nac_p=nac,
                        region="baltic",
                    )
                )
        s.commit()

        incidents, _ = detect_jamming(s, "baltic")
        assert len(incidents) == 1
        ev = incidents[0].evidence
        assert ev is not None
        # Five of six are degraded on NIC, but only the three with NACp agreement are hard loss.
        assert ev["aircraft_observed"] == 6
        assert ev["aircraft_degraded"] == 5
        assert ev["aircraft_hard_loss"] == 3
        assert ev["hard_loss_aircraft"] == ["aa0001", "aa0002", "aa0003"]
        # NIC-degraded-but-NACp-healthy aircraft must NOT be promoted to hard loss.
        assert "aa0004" not in ev["hard_loss_aircraft"]


def test_jamming_until_bounds_the_scored_interval() -> None:
    """`until` exists so an evaluation can describe ONE bounded interval.

    A controlled comparison whose report statistics cover an overlap window but whose
    incidents cover the whole database is describing two different experiments in one
    table — that inconsistency was live in scripts/eval_control.py before this parameter.
    """
    from datetime import UTC, datetime, timedelta

    from horus.db.models import Aircraft, Position
    from horus.detect.jamming import detect_jamming

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    t0 = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    with Session(engine) as s:
        # Two firing buckets an hour apart; the bound must keep only the first.
        for start_minute in (0, 60):
            for k in range(5):
                icao = f"a{start_minute:02d}{k:03x}"
                s.add(Aircraft(icao24=icao))
                for m in (1, 5, 9):
                    s.add(
                        Position(
                            icao24=icao,
                            ts=t0 + timedelta(minutes=start_minute + m),
                            lat=54.9,
                            lon=20.5,
                            alt_baro_ft=35000.0,
                            nic=0 if k < 4 else 8,
                            nac_p=0 if k < 4 else 9,
                            region="baltic",
                        )
                    )
        s.commit()

        both, _ = detect_jamming(s, "baltic")
        assert len(both) == 2
        first_only, _ = detect_jamming(
            s, "baltic", until=(t0 + timedelta(minutes=30)).replace(tzinfo=None)
        )
        assert len(first_only) == 1
        assert first_only[0].incident_id == min(i.incident_id for i in both)
