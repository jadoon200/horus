from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from horus.db.base import Base
from horus.db.models import Position
from horus.ingest.synthetic import JAM_WINDOW, generate, seed_db
from horus.zones import zone_by_id


def test_generation_is_deterministic() -> None:
    a, b = generate(seed=7), generate(seed=7)
    assert a.samples == b.samples and a.labels == b.labels
    assert generate(seed=8).samples != a.samples


def test_scenario_contains_every_label_kind_and_confounders() -> None:
    data = generate()
    kinds = {label.kind for label in data.labels}
    assert kinds == {"jamming", "gap", "incursion", "spoof", "anomaly"}

    # The jamming window really degrades a *cluster* of aircraft inside the cell...
    jam = next(label for label in data.labels if label.kind == "jamming")
    degraded = {
        s.icao24
        for s in data.samples
        if s.nic is not None
        and s.nic <= 4
        and jam.ts_start <= s.ts < jam.ts_end
        and not s.icao24.startswith(("b", "f"))
    }
    assert len(degraded) >= 3, "the jam cell must affect several transiting aircraft"

    # ...while the lone benign NIC dip stays a single sample of a single aircraft.
    dip = [s for s in data.samples if s.icao24 == "b00d1f" and s.nic is not None and s.nic <= 4]
    assert len(dip) == 1

    # Low-altitude coverage-dropout confounder: silent stretch below the altitude floor.
    low = [s for s in data.samples if s.icao24 == "c00000"]
    assert all((s.alt_baro_ft or 0) < 10_000 for s in low)
    steps = {int((s.ts - low[0].ts).total_seconds() / 30) for s in low}
    assert not steps & set(range(50, 70)), "the dropout window must be silent"

    # Dark aircraft: silent through the labelled window, at cruise altitude.
    dark = [s for s in data.samples if s.icao24 == "d00001"]
    gap = next(x for x in data.labels if x.kind == "gap" and x.icao24 == "d00001")
    assert all(not (gap.ts_start <= s.ts < gap.ts_end) for s in dark)
    assert all((s.alt_baro_ft or 0) > 10_000 for s in dark)


def test_jam_cell_sits_inside_watch_corridor() -> None:
    # The scenario is built so the jamming incident lands in a sensitive zone.
    from horus.ingest.synthetic import JAM_CENTER
    from horus.zones import zone_for

    zone = zone_for(*JAM_CENTER)
    assert zone is not None and zone.zone_id == "sg-strait-corridor"
    assert zone_by_id("sg-strait-corridor") is not None
    assert JAM_WINDOW[0] < JAM_WINDOW[1]


def test_seed_db_persists_through_normal_ingest_path() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        data = generate()
        inserted = seed_db(db, data)
        db.commit()
        assert inserted == len(data.samples)
        assert db.scalar(select(func.count()).select_from(Position)) == inserted
        # Re-seeding is idempotent (the dedup path).
        assert seed_db(db, data) == 0
