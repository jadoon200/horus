"""adsb.lol v2 client — free, keyless ADS-B state snapshots (readsb aircraft schema).

One point query returns every tracked aircraft within a radius of a centre, including the
GNSS integrity fields (nic / nac_p / sil) the flagship jamming detector needs. The API is
community-run and keyless (zero-cost rule); the client is defensive about every field —
readsb JSON is external input where anything can be absent or the string "ground".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from horus.config import get_settings
from horus.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class AdsbSample:
    """One parsed aircraft state sample, normalized for persistence."""

    icao24: str
    ts: datetime  # aware UTC, corrected for the position's age (`seen_pos`)
    lat: float
    lon: float
    callsign: str | None
    registration: str | None
    type_code: str | None
    category: str | None
    alt_baro_ft: float | None
    alt_geom_ft: float | None
    gs_kt: float | None
    track_deg: float | None
    baro_rate_fpm: float | None
    on_ground: bool
    squawk: str | None
    nic: int | None
    nac_p: int | None
    sil: int | None
    rssi: float | None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_aircraft(
    entry: dict[str, Any], now: datetime, *, max_seen_pos_seconds: float
) -> AdsbSample | None:
    """Parse one readsb aircraft entry into a sample, or None if unusable.

    Unusable: no hex id, a non-ICAO synthetic id (TIS-B `~` prefix — no stable aircraft
    identity), no position, or a position older than `max_seen_pos_seconds` at sample time
    (a coasted estimate, not a fresh report).
    """
    hexid = str(entry.get("hex") or "").strip().lower()
    if not hexid or hexid.startswith("~"):
        return None
    lat = _as_float(entry.get("lat"))
    lon = _as_float(entry.get("lon"))
    if lat is None or lon is None:
        return None
    seen_pos = _as_float(entry.get("seen_pos"))
    if seen_pos is None or seen_pos > max_seen_pos_seconds:
        return None

    alt_baro_raw = entry.get("alt_baro")
    on_ground = alt_baro_raw == "ground"
    callsign = str(entry.get("flight") or "").strip() or None
    return AdsbSample(
        icao24=hexid,
        ts=datetime.fromtimestamp(now.timestamp() - seen_pos, tz=UTC),
        lat=lat,
        lon=lon,
        callsign=callsign,
        registration=str(entry.get("r") or "").strip() or None,
        type_code=str(entry.get("t") or "").strip() or None,
        category=str(entry.get("category") or "").strip() or None,
        alt_baro_ft=None if on_ground else _as_float(alt_baro_raw),
        alt_geom_ft=_as_float(entry.get("alt_geom")),
        gs_kt=_as_float(entry.get("gs")),
        track_deg=_as_float(entry.get("track")),
        baro_rate_fpm=_as_float(entry.get("baro_rate")),
        on_ground=on_ground,
        squawk=str(entry.get("squawk") or "").strip() or None,
        nic=_as_int(entry.get("nic")),
        nac_p=_as_int(entry.get("nac_p")),
        sil=_as_int(entry.get("sil")),
        rssi=_as_float(entry.get("rssi")),
    )


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
def _get_json(url: str, timeout: float) -> dict[str, Any]:
    resp = httpx.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def fetch_point(
    lat: float | None = None, lon: float | None = None, radius_nm: int | None = None
) -> list[AdsbSample]:
    """One snapshot of every fresh, positioned aircraft in the configured circle."""
    s = get_settings()
    lat = s.adsb_center_lat if lat is None else lat
    lon = s.adsb_center_lon if lon is None else lon
    radius_nm = s.adsb_radius_nm if radius_nm is None else radius_nm
    url = f"{s.adsb_api_url.rstrip('/')}/point/{lat}/{lon}/{radius_nm}"
    data = _get_json(url, s.http_timeout_seconds)

    # Response `now` is epoch milliseconds; fall back to the local clock if absent.
    now_ms = _as_float(data.get("now"))
    now = datetime.fromtimestamp(now_ms / 1000.0, tz=UTC) if now_ms else datetime.now(UTC)
    entries = data.get("ac") or []
    samples: list[AdsbSample] = []
    for entry in entries:
        if isinstance(entry, dict):
            sample = parse_aircraft(entry, now, max_seen_pos_seconds=s.adsb_max_seen_pos_seconds)
            if sample is not None:
                samples.append(sample)
    log.info("adsb_fetch", raw=len(entries), fresh=len(samples))
    return samples
