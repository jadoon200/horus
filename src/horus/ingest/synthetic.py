"""Deterministic labelled synthetic ADS-B generator — the offline gold set.

The shared source of truth for tests, the demo seed, and the eval harness: seeded flights
over the Singapore FIR neighbourhood with known injected events (a jamming cell, dark
aircraft, a border incursion, a spoofed identity, trajectory anomalies) *and benign
confounders* (low-altitude coverage dropouts, a lone benign NIC dip) that a naive detector
would false-positive on.

Honesty note (inherited from PHAROS): self-generated anomalies are separable *by
construction*, so gold-set scores are a ceiling, never a capability claim. The number that
counts comes from real collected data.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from horus.ingest.adsb import AdsbSample
from horus.ingest.persist import persist_samples

T0 = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
STEP_S = 30.0
N_STEPS = 120  # one hour at 30 s

# Jamming scenario cell (inside the sg-strait-corridor watch zone) and window.
JAM_CENTER = (1.15, 103.90)
JAM_RADIUS_DEG = 0.35
JAM_WINDOW = (40, 80)  # step indices

# ~1 degree of latitude in km (for coarse displacement math in the generator only).
_KM_PER_DEG = 111.0


@dataclass(frozen=True)
class GoldLabel:
    """One injected event the detectors should find (or honestly miss)."""

    kind: str  # jamming | gap | incursion | spoof | anomaly
    icao24: str | None  # None for the area-level jamming cell
    ts_start: datetime
    ts_end: datetime
    lat: float | None = None
    lon: float | None = None


@dataclass
class SyntheticData:
    samples: list[AdsbSample] = field(default_factory=list)
    labels: list[GoldLabel] = field(default_factory=list)


def _ts(step: int) -> datetime:
    return T0 + timedelta(seconds=STEP_S * step)


def _in_jam_cell(lat: float, lon: float) -> bool:
    return abs(lat - JAM_CENTER[0]) <= JAM_RADIUS_DEG and abs(lon - JAM_CENTER[1]) <= JAM_RADIUS_DEG


def _sample(
    icao24: str,
    step: int,
    lat: float,
    lon: float,
    *,
    alt_ft: float,
    gs_kt: float,
    track_deg: float,
    nic: int,
    nac_p: int,
) -> AdsbSample:
    return AdsbSample(
        icao24=icao24,
        ts=_ts(step),
        lat=lat,
        lon=lon,
        callsign=f"SYN{icao24[-3:].upper()}",
        registration=None,
        type_code="A320",
        category="A3",
        alt_baro_ft=alt_ft,
        alt_geom_ft=alt_ft,
        gs_kt=gs_kt,
        track_deg=track_deg,
        baro_rate_fpm=0.0,
        on_ground=False,
        squawk="2000",
        nic=nic,
        nac_p=nac_p,
        sil=3,
        rssi=-18.0,
    )


def _line_flight(
    rng: random.Random,
    icao24: str,
    *,
    lat0: float,
    lon0: float,
    heading_deg: float,
    gs_kt: float,
    alt_ft: float,
    jam_affected: bool,
    skip_steps: set[int] | None = None,
    nic_healthy: int = 8,
) -> list[AdsbSample]:
    """A straight flight with light GPS noise; optionally degraded inside the jam cell."""
    out: list[AdsbSample] = []
    km_per_step = gs_kt * 1.852 * STEP_S / 3600.0
    dlat = km_per_step / _KM_PER_DEG * math.cos(math.radians(heading_deg))
    dlon = (
        km_per_step
        / (_KM_PER_DEG * max(0.2, math.cos(math.radians(lat0))))
        * math.sin(math.radians(heading_deg))
    )
    lat, lon = lat0, lon0
    for step in range(N_STEPS):
        if skip_steps and step in skip_steps:
            lat += dlat
            lon += dlon
            continue
        jammed = jam_affected and JAM_WINDOW[0] <= step < JAM_WINDOW[1] and _in_jam_cell(lat, lon)
        nic = rng.choice([0, 1, 2, 4]) if jammed else nic_healthy
        nac_p = rng.choice([0, 2, 4]) if jammed else 9
        out.append(
            _sample(
                icao24,
                step,
                lat + rng.gauss(0.0, 0.0004),
                lon + rng.gauss(0.0, 0.0004),
                alt_ft=alt_ft,
                gs_kt=gs_kt,
                track_deg=heading_deg,
                nic=nic,
                nac_p=nac_p,
            )
        )
        lat += dlat
        lon += dlon
    return out


def generate(seed: int = 7, n_cruise: int = 24, n_low: int = 5) -> SyntheticData:
    """The full labelled scenario. Deterministic for a given seed and sizes."""
    rng = random.Random(seed)
    data = SyntheticData()

    def add_label(kind: str, icao24: str | None, s0: int, s1: int, **kw: float) -> None:
        data.labels.append(
            GoldLabel(kind=kind, icao24=icao24, ts_start=_ts(s0), ts_end=_ts(s1), **kw)
        )

    # --- benign cruise population --------------------------------------------------------
    for i in range(n_cruise):
        icao = f"a{i:05x}"
        lat0 = rng.uniform(0.6, 1.9)
        lon0 = rng.uniform(102.9, 104.7)
        heading = rng.choice([70.0, 110.0, 250.0, 290.0]) + rng.uniform(-8, 8)
        data.samples += _line_flight(
            rng,
            icao,
            lat0=lat0,
            lon0=lon0,
            heading_deg=heading,
            gs_kt=rng.uniform(420, 480),
            alt_ft=rng.uniform(30_000, 40_000),
            jam_affected=True,  # degraded only if/while actually inside the cell+window
        )

    # Staged jam-cell transits: cruise flights whose start point is back-computed so each
    # crosses the cell centre mid-window (a fast jet clears the 0.7° cell in ~5 min, so a
    # random population rarely lingers there — the cluster is guaranteed by construction).
    for k in range(6):
        heading = [70.0, 110.0, 250.0, 290.0, 90.0, 270.0][k]
        gs = 430.0 + 10.0 * k
        centre_step = 48 + 4 * k  # spread the transits through the window
        km_per_step = gs * 1.852 * STEP_S / 3600.0
        dlat = km_per_step / _KM_PER_DEG * math.cos(math.radians(heading))
        dlon = (
            km_per_step
            / (_KM_PER_DEG * max(0.2, math.cos(math.radians(JAM_CENTER[0]))))
            * math.sin(math.radians(heading))
        )
        data.samples += _line_flight(
            rng,
            f"aa{k:04x}",
            lat0=JAM_CENTER[0] - dlat * centre_step,
            lon0=JAM_CENTER[1] - dlon * centre_step,
            heading_deg=heading,
            gs_kt=gs,
            alt_ft=rng.uniform(30_000, 40_000),
            jam_affected=True,
        )

    # Benign confounder: one healthy aircraft with a single-sample NIC dip (no cluster —
    # a lone dip is not interference and must not become an incident).
    dip = _line_flight(
        rng,
        "b00d1f",
        lat0=2.0,
        lon0=103.0,
        heading_deg=120.0,
        gs_kt=450,
        alt_ft=36_000,
        jam_affected=False,
    )
    lone = dip[len(dip) // 2]
    dip[len(dip) // 2] = AdsbSample(**{**lone.__dict__, "nic": 2, "nac_p": 3})
    data.samples += dip
    add_label("jamming", None, JAM_WINDOW[0], JAM_WINDOW[1], lat=JAM_CENTER[0], lon=JAM_CENTER[1])

    # --- benign low-altitude flights with coverage dropouts (gap confounder) -----------
    # Silent below the altitude floor: normal terrain-limited reception, NOT a dark
    # aircraft. Kept north of (and heading parallel to) the Riau border box so this
    # confounder tests the gap detector's altitude floor, not the incursion ring —
    # each trap must isolate one detector's failure mode.
    for i in range(n_low):
        icao = f"c{i:05x}"
        dropout = set(range(50, 70)) if i % 2 == 0 else set()
        data.samples += _line_flight(
            rng,
            icao,
            lat0=rng.uniform(1.2, 1.45),
            lon0=rng.uniform(103.3, 104.3),
            heading_deg=rng.choice([90.0, 270.0]) + rng.uniform(-2, 2),
            gs_kt=rng.uniform(140, 220),
            alt_ft=rng.uniform(1_500, 7_000),
            jam_affected=False,
            skip_steps=dropout,
        )

    # --- dark aircraft: silent 20 min at cruise, reappearing far along track -----------
    for i, icao in enumerate(["d00001", "d00002"]):
        data.samples += _line_flight(
            rng,
            icao,
            lat0=0.7 + 0.2 * i,
            lon0=102.9,
            heading_deg=75.0,
            gs_kt=470,
            alt_ft=37_000,
            jam_affected=False,
            skip_steps=set(range(40, 80)),  # 20 min silent -> ~290 km displaced
        )
        add_label("gap", icao, 40, 80)

    # --- border incursion: low-level track dipping into the riau-border-watch box ------
    data.samples += _line_flight(
        rng,
        "e00001",
        lat0=1.35,
        lon0=104.2,
        heading_deg=185.0,  # southbound across the strait into the Riau box
        gs_kt=160,
        alt_ft=1_800,
        jam_affected=False,
    )
    add_label("incursion", "e00001", 30, N_STEPS, lat=0.9, lon=104.2)

    # --- spoof: one identity alternating between two locations ~300 km apart -----------
    spoof: list[AdsbSample] = []
    for step in range(0, N_STEPS, 2):
        here = step % 4 == 0
        spoof.append(
            _sample(
                "f00bad",
                step,
                1.2 if here else 3.9,
                103.6 if here else 104.4,
                alt_ft=35_000,
                gs_kt=450,
                track_deg=90.0,
                nic=8,
                nac_p=9,
            )
        )
    data.samples += spoof
    add_label("spoof", "f00bad", 0, N_STEPS)

    # --- trajectory anomalies: tight orbiting tracks amid a straight population --------
    for i, icao in enumerate(["ab0001", "ab0002"]):
        centre_lat, centre_lon = 1.05 + 0.1 * i, 103.6 + 0.5 * i
        for step in range(N_STEPS):
            ang = step * 0.35
            data.samples.append(
                _sample(
                    icao,
                    step,
                    centre_lat + 0.08 * math.sin(ang),
                    centre_lon + 0.08 * math.cos(ang),
                    alt_ft=12_000,
                    gs_kt=250,
                    track_deg=(math.degrees(ang) + 90.0) % 360.0,
                    nic=8,
                    nac_p=9,
                )
            )
        add_label("anomaly", icao, 0, N_STEPS)

    data.samples.sort(key=lambda s: (s.ts, s.icao24))
    return data


def seed_db(session: Session, data: SyntheticData, *, region: str = "synthetic") -> int:
    """Persist the scenario through the normal ingest path (same dedup, same schema)."""
    return persist_samples(session, data.samples, source="synthetic", region=region)
