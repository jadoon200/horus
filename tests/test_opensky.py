from datetime import UTC, datetime

import httpx
import respx

from horus.config import get_settings
from horus.ingest.opensky import fetch_bbox, parse_state_vector

_NOW = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)
_VECTOR = [
    "76ceef",
    "SIA321 ",
    "Singapore",
    int(_NOW.timestamp()) - 2,
    int(_NOW.timestamp()) - 1,
    103.95,
    1.31,
    3_657.6,
    False,
    159.7,
    82.3,
    -3.25,
    None,
    3_810.0,
    "2057",
    False,
    0,
    6,
]


def test_parse_state_vector_converts_units_without_inventing_integrity() -> None:
    sample = parse_state_vector(_VECTOR, _NOW, max_position_age_seconds=60)

    assert sample is not None
    assert sample.icao24 == "76ceef" and sample.callsign == "SIA321"
    assert sample.alt_baro_ft is not None and abs(sample.alt_baro_ft - 12_000) < 1
    assert sample.gs_kt is not None and abs(sample.gs_kt - 310.4) < 0.2
    assert sample.baro_rate_fpm is not None and abs(sample.baro_rate_fpm + 639.8) < 0.2
    assert (sample.nic, sample.nac_p, sample.sil) == (None, None, None)
    assert sample.category == "OS6"


def test_parse_state_vector_rejects_absent_or_stale_positions() -> None:
    assert parse_state_vector(_VECTOR[:10], _NOW, max_position_age_seconds=60) is None
    stale = [*_VECTOR]
    stale[3] = int(_NOW.timestamp()) - 120
    assert parse_state_vector(stale, _NOW, max_position_age_seconds=60) is None
    absent = [*_VECTOR]
    absent[5] = None
    assert parse_state_vector(absent, _NOW, max_position_age_seconds=60) is None


@respx.mock
def test_fetch_bbox_supports_anonymous_latest_state(monkeypatch) -> None:
    monkeypatch.delenv("HORUS_OPENSKY_CLIENT_ID", raising=False)
    monkeypatch.delenv("HORUS_OPENSKY_CLIENT_SECRET", raising=False)
    get_settings.cache_clear()
    route = respx.get(
        "https://opensky-network.org/api/states/all",
        params={"lamin": 0.0, "lomin": 102.5, "lamax": 2.5, "lomax": 105.0},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"time": int(_NOW.timestamp()), "states": [_VECTOR]},
        )
    )

    samples = fetch_bbox(0.0, 102.5, 2.5, 105.0)

    assert route.called
    assert [sample.icao24 for sample in samples] == ["76ceef"]
    get_settings.cache_clear()


@respx.mock
def test_fetch_bbox_uses_oauth_client_credentials(monkeypatch) -> None:
    monkeypatch.setenv("HORUS_OPENSKY_CLIENT_ID", "client")
    monkeypatch.setenv("HORUS_OPENSKY_CLIENT_SECRET", "secret")
    get_settings.cache_clear()
    token = respx.post(
        "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
    ).mock(return_value=httpx.Response(200, json={"access_token": "token"}))
    states = respx.get("https://opensky-network.org/api/states/all").mock(
        return_value=httpx.Response(
            200,
            json={"time": int(_NOW.timestamp()), "states": [_VECTOR]},
        )
    )

    samples = fetch_bbox(0.0, 102.5, 2.5, 105.0)

    assert token.called and states.called
    assert states.calls.last.request.headers["authorization"] == "Bearer token"
    assert len(samples) == 1
    get_settings.cache_clear()
