from datetime import UTC, datetime

from scripts.eval_opensky import compare_coverage, render

from horus.ingest.adsb import AdsbSample


def _sample(icao24: str, lat: float, lon: float) -> AdsbSample:
    return AdsbSample(
        icao24=icao24,
        ts=datetime(2026, 7, 25, tzinfo=UTC),
        lat=lat,
        lon=lon,
        callsign=None,
        registration=None,
        type_code=None,
        category=None,
        alt_baro_ft=10_000,
        alt_geom_ft=None,
        gs_kt=300,
        track_deg=90,
        baro_rate_fpm=0,
        on_ground=False,
        squawk=None,
        nic=None,
        nac_p=None,
        sil=None,
        rssi=None,
    )


def test_coverage_union_is_reported_as_potential_not_integrity_evidence() -> None:
    adsb = [_sample("a00001", 1.1, 103.1), _sample("a00002", 1.2, 103.2)]
    opensky = [
        _sample("a00002", 1.2, 103.2),
        _sample("a00003", 1.3, 103.3),
        _sample("a00004", 1.4, 103.4),
    ]

    result = compare_coverage(adsb, opensky, cell_deg=0.5, min_aircraft=4)
    report = render(result, cell_deg=0.5, min_aircraft=4)

    assert result.shared_aircraft == 1
    assert result.adsb_scoreable_cells == 0
    assert result.density_gain_cells == 1
    assert "coverage potential only" in report
    assert "cannot make a HORUS jamming cell scoreable" in report
