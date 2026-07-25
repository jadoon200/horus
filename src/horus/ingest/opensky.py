"""Optional OpenSky state-vector client for one-shot research cross-checks.

OpenSky is a useful independent receiver network, but it is not a drop-in second GNSS
integrity source: state vectors expose position and kinematics, not NIC/NACp/SIL. Current
OpenSky terms also require a written agreement for any operational REST integration.
Consequently this module is not wired into HORUS's collector, runtime API, or deployment.
It exists for an explicitly invoked, terms-acknowledged coverage experiment only.

The latest state-vector endpoint supports anonymous access. Optional OAuth2 client
credentials only change the research quota; they are never required by the keyless core.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from horus.config import Settings, get_settings
from horus.ingest.adsb import AdsbSample
from horus.logging import get_logger

log = get_logger(__name__)

_METRES_TO_FEET = 3.280839895
_MPS_TO_KNOTS = 1.943844492
_MPS_TO_FPM = 196.8503937


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_state_vector(
    vector: list[Any],
    snapshot_ts: datetime,
    *,
    max_position_age_seconds: float,
) -> AdsbSample | None:
    """Normalize one OpenSky state vector, rejecting absent or stale positions."""
    if len(vector) < 17:
        return None
    icao24 = str(vector[0] or "").strip().lower()
    position_epoch = _number(vector[3])
    lon = _number(vector[5])
    lat = _number(vector[6])
    if not icao24 or position_epoch is None or lat is None or lon is None:
        return None
    ts = datetime.fromtimestamp(position_epoch, tz=UTC)
    age = (snapshot_ts - ts).total_seconds()
    if age < -5 or age > max_position_age_seconds:
        return None

    baro_m = _number(vector[7])
    velocity_mps = _number(vector[9])
    vertical_mps = _number(vector[11])
    geom_m = _number(vector[13])
    category = vector[17] if len(vector) > 17 else None
    return AdsbSample(
        icao24=icao24,
        ts=ts,
        lat=lat,
        lon=lon,
        callsign=str(vector[1] or "").strip() or None,
        registration=None,
        type_code=None,
        category=f"OS{category}" if isinstance(category, int) else None,
        alt_baro_ft=baro_m * _METRES_TO_FEET if baro_m is not None else None,
        alt_geom_ft=geom_m * _METRES_TO_FEET if geom_m is not None else None,
        gs_kt=velocity_mps * _MPS_TO_KNOTS if velocity_mps is not None else None,
        track_deg=_number(vector[10]),
        baro_rate_fpm=vertical_mps * _MPS_TO_FPM if vertical_mps is not None else None,
        on_ground=bool(vector[8]),
        squawk=str(vector[14] or "").strip() or None,
        # OpenSky state vectors do not expose these fields. They must remain missing rather
        # than borrowing integrity observations from a different source.
        nic=None,
        nac_p=None,
        sil=None,
        rssi=None,
    )


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
def _post_token(settings: Settings) -> str:
    response = httpx.post(
        settings.opensky_token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": settings.opensky_client_id,
            "client_secret": settings.opensky_client_secret,
        },
        timeout=settings.http_timeout_seconds,
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError("OpenSky token response did not include access_token")
    return token


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
def _get_states(
    settings: Settings,
    params: dict[str, float],
    headers: dict[str, str],
) -> dict[str, Any]:
    response = httpx.get(
        f"{settings.opensky_api_url.rstrip('/')}/states/all",
        params=params,
        headers=headers,
        timeout=settings.http_timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def fetch_bbox(
    lamin: float,
    lomin: float,
    lamax: float,
    lomax: float,
) -> list[AdsbSample]:
    """Fetch one latest-state snapshot for a small WGS-84 bounding box."""
    if lamin >= lamax or lomin >= lomax:
        raise ValueError("OpenSky bounding box minimums must be below maximums")
    settings = get_settings()
    has_id = bool(settings.opensky_client_id)
    has_secret = bool(settings.opensky_client_secret)
    if has_id != has_secret:
        raise ValueError("both OpenSky OAuth client id and secret must be configured")
    headers: dict[str, str] = {}
    if has_id:
        headers["Authorization"] = f"Bearer {_post_token(settings)}"
    payload = _get_states(
        settings,
        {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax},
        headers,
    )
    snapshot_epoch = _number(payload.get("time"))
    snapshot_ts = (
        datetime.fromtimestamp(snapshot_epoch, tz=UTC)
        if snapshot_epoch is not None
        else datetime.now(UTC)
    )
    vectors = payload.get("states") or []
    samples = [
        parsed
        for vector in vectors
        if isinstance(vector, list)
        and (
            parsed := parse_state_vector(
                vector,
                snapshot_ts,
                max_position_age_seconds=settings.opensky_max_position_age_seconds,
            )
        )
        is not None
    ]
    log.info(
        "opensky_fetch",
        raw=len(vectors),
        fresh=len(samples),
        authenticated=has_id,
    )
    return samples
