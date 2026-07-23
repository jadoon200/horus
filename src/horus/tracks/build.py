"""Per-aircraft track building: gap-split segmentation, kinematics, shape descriptors.

    python -m horus.tracks.build [--region LABEL]

A *track* is one contiguous flight segment — a run of positions with no silence longer
than `track_gap_minutes`. Each stored track carries two cached descriptors for the anomaly
models: `features` (a fixed-length flattened vector for the Isolation-Forest/PCA
baselines) and `sequence` (an ordered per-step [dx_km, dy_km, step_len_km, turn_rad,
dalt_kft] descriptor for the flagship GRU autoencoder). Both are translation-invariant
(deltas, not absolute coordinates) so models learn *shape*, not location.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from horus.config import get_settings
from horus.db.base import session_scope
from horus.db.models import Position, Track
from horus.geo import haversine_series_km
from horus.logging import configure_logging, get_logger
from horus.timeutil import utc_naive

log = get_logger(__name__)


@dataclass
class _Point:
    ts: datetime
    lat: float
    lon: float
    alt_ft: float | None
    # Carried per point, not per aircraft: one aircraft can appear in more than one region
    # (a live slice and a backfilled historical one), and stamping every track with
    # whichever region happened to be seen last would mislabel tracks — which then makes a
    # region-scoped rebuild delete the wrong rows.
    region: str | None = None


def _segments(points: list[_Point], gap_minutes: float) -> list[list[_Point]]:
    """Split one aircraft's ordered points into contiguous segments at silences."""
    if not points:
        return []
    out: list[list[_Point]] = [[points[0]]]
    for prev, cur in pairwise(points):
        dt = (utc_naive(cur.ts) - utc_naive(prev.ts)).total_seconds()
        if dt > gap_minutes * 60.0:
            out.append([cur])
        else:
            out[-1].append(cur)
    return out


def _resample(points: list[_Point], n: int) -> tuple[np.ndarray, np.ndarray]:
    """Time-uniform resample to `n` points: (n,2) lat/lon and (n,) altitude (kft)."""
    ts = np.array([utc_naive(p.ts).timestamp() for p in points])
    lat = np.array([p.lat for p in points])
    lon = np.array([p.lon for p in points])
    alt = np.array([(p.alt_ft if p.alt_ft is not None else 0.0) / 1000.0 for p in points])
    grid = np.linspace(ts[0], ts[-1], n)
    return (
        np.stack([np.interp(grid, ts, lat), np.interp(grid, ts, lon)], axis=1),
        np.interp(grid, ts, alt),
    )


def track_sequence(points: list[_Point], n: int) -> list[list[float]]:
    """The GRU's per-step descriptor over the resampled path ((n-1) x 5).

    Steps are local-plane deltas in km plus a turn angle and an altitude delta —
    heading of the first step is NOT removed (unlike a heading-invariant variant), so
    holding patterns and hard turns stay visible as sequential structure.
    """
    coords, alt = _resample(points, n)
    lat = coords[:, 0]
    lon = coords[:, 1]
    dlat_km = np.diff(lat) * 111.0
    dlon_km = np.diff(lon) * 111.0 * np.cos(np.radians((lat[:-1] + lat[1:]) / 2.0))
    step_len = np.hypot(dlat_km, dlon_km)
    headings = np.arctan2(dlon_km, dlat_km)
    turns = np.diff(headings, prepend=headings[0])
    turns = np.arctan2(np.sin(turns), np.cos(turns))  # wrap to [-pi, pi]
    dalt = np.diff(alt)
    seq = np.stack([dlat_km, dlon_km, step_len, turns, dalt], axis=1)
    return [[float(v) for v in row] for row in seq]


def track_features(points: list[_Point], n: int) -> list[float]:
    """Flattened fixed-length vector for the baseline models (same information)."""
    seq = np.asarray(track_sequence(points, n), dtype=float)
    return [float(v) for v in seq.reshape(-1)]


def build_tracks(session: Session, region: str | None = None) -> int:
    """Full rebuild of the tracks table (for the region, when given); returns count."""
    s = get_settings()
    del_q = delete(Track)
    pos_q = select(Position).order_by(Position.icao24, Position.ts)
    if region:
        del_q = del_q.where(Track.region == region)
        pos_q = pos_q.where(Position.region == region)
    session.execute(del_q)

    by_aircraft: dict[str, list[_Point]] = {}
    for p in session.scalars(pos_q):
        if p.on_ground:
            continue  # taxiing is not flight shape
        by_aircraft.setdefault(p.icao24, []).append(
            _Point(p.ts, p.lat, p.lon, p.alt_baro_ft, p.region)
        )

    n_tracks = 0
    for icao24, points in by_aircraft.items():
        for seg in _segments(points, s.track_gap_minutes):
            if len(seg) < s.track_min_points:
                continue
            lats = np.array([p.lat for p in seg])
            lons = np.array([p.lon for p in seg])
            distance = float(np.sum(haversine_series_km(lats, lons)))
            session.add(
                Track(
                    track_id=f"{icao24}:{utc_naive(seg[0].ts).isoformat()}",
                    icao24=icao24,
                    region=seg[0].region,  # this segment's own region, not the aircraft's last
                    start_ts=seg[0].ts,
                    end_ts=seg[-1].ts,
                    point_count=len(seg),
                    distance_km=distance,
                    start_lat=seg[0].lat,
                    start_lon=seg[0].lon,
                    end_lat=seg[-1].lat,
                    end_lon=seg[-1].lon,
                    features=track_features(seg, s.track_resample_points),
                    sequence=track_sequence(seg, s.track_resample_points),
                )
            )
            n_tracks += 1
    log.info("build_tracks", aircraft=len(by_aircraft), tracks=n_tracks)
    return n_tracks


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Build per-aircraft flight segments")
    parser.add_argument("--region", default=None)
    args = parser.parse_args()
    with session_scope() as session:
        build_tracks(session, args.region)


if __name__ == "__main__":
    main()
