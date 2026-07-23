from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from horus.db.base import Base

# JSONB on Postgres, plain JSON elsewhere (keeps unit tests runnable on SQLite).
# none_as_null: Python None must mean SQL NULL, not a stored JSON `null` — so an explicit
# `= None` stays invisible to `IS NULL` filters and matched by `IS NOT NULL`.
JsonType = JSON(none_as_null=True).with_variant(postgresql.JSONB(none_as_null=True), "postgresql")


def _now() -> datetime:
    # Aware UTC, not local: SQLite drops tzinfo on round-trip, so an aware local-time default
    # would persist the local clock fields and skew against every other UTC-stored timestamp.
    return datetime.now(UTC)


class Aircraft(Base):
    """An aircraft keyed by its ICAO 24-bit transponder address (hex string).

    Identity fields are advisory: ADS-B is self-reported, so `callsign`/`registration`/
    `type_code` can be absent or falsified — the spoof detector exists precisely because
    they can't be trusted at face value.
    """

    __tablename__ = "aircraft"

    icao24: Mapped[str] = mapped_column(String(16), primary_key=True)
    callsign: Mapped[str | None] = mapped_column(String(16))  # latest seen flight id
    registration: Mapped[str | None] = mapped_column(String(16))
    type_code: Mapped[str | None] = mapped_column(String(8))  # e.g. A359, B738
    category: Mapped[str | None] = mapped_column(String(4))  # ADS-B emitter category (A3, ...)
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Position(Base):
    """A single ADS-B state sample (one point on an aircraft's track).

    Carries the GNSS integrity fields the flagship detector needs: NIC (Navigation
    Integrity Category), NACp (Navigation Accuracy Category — position) and SIL (Source
    Integrity Level) are broadcast by the aircraft itself and collapse when its own GNSS
    solution degrades — the well-established open-source jamming proxy.
    """

    __tablename__ = "positions"
    __table_args__ = (Index("ix_positions_icao24_ts", "icao24", "ts"),)

    id: Mapped[int] = mapped_column(Integer(), primary_key=True, autoincrement=True)
    icao24: Mapped[str] = mapped_column(ForeignKey("aircraft.icao24"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lat: Mapped[float] = mapped_column(Float())
    lon: Mapped[float] = mapped_column(Float())
    alt_baro_ft: Mapped[float | None] = mapped_column(Float())  # barometric altitude (ft)
    alt_geom_ft: Mapped[float | None] = mapped_column(Float())  # GNSS geometric altitude (ft)
    gs_kt: Mapped[float | None] = mapped_column(Float())  # ground speed (knots)
    track_deg: Mapped[float | None] = mapped_column(Float())  # true track (degrees)
    baro_rate_fpm: Mapped[float | None] = mapped_column(Float())  # climb/descent (ft/min)
    on_ground: Mapped[bool] = mapped_column(Boolean(), default=False)
    squawk: Mapped[str | None] = mapped_column(String(8))
    # GNSS integrity (the flagship signal). None = not broadcast in this sample.
    nic: Mapped[int | None] = mapped_column(Integer())
    nac_p: Mapped[int | None] = mapped_column(Integer())
    sil: Mapped[int | None] = mapped_column(Integer())
    rssi: Mapped[float | None] = mapped_column(Float())  # receiver signal strength (dBFS)
    # Provenance of the sample, e.g. "adsb-lol" | "synthetic".
    source: Mapped[str] = mapped_column(String(32), index=True, default="adsb-lol")
    region: Mapped[str | None] = mapped_column(String(64), index=True)  # dataset slice label
    raw: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CollectorRun(Base):
    """One continuous collector process lifetime and its observed feed health."""

    __tablename__ = "collector_runs"

    id: Mapped[int] = mapped_column(Integer(), primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_reason: Mapped[str | None] = mapped_column(String(255))
    report_count: Mapped[int] = mapped_column(Integer(), default=0)
    aircraft_count: Mapped[int] = mapped_column(Integer(), default=0)
    status: Mapped[str] = mapped_column(String(32), index=True, default="running")


class CoverageOutage(Base):
    """A receiver/feed outage that must not be interpreted as aircraft silence."""

    __tablename__ = "coverage_outages"

    id: Mapped[int] = mapped_column(Integer(), primary_key=True, autoincrement=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(255))
    run_id: Mapped[int] = mapped_column(ForeignKey("collector_runs.id"), index=True)


class Track(Base):
    """A flight segment — a contiguous run of one aircraft's positions between gaps."""

    __tablename__ = "tracks"

    track_id: Mapped[str] = mapped_column(String(96), primary_key=True)  # "<icao24>:<start_ts>"
    icao24: Mapped[str] = mapped_column(ForeignKey("aircraft.icao24"), index=True)
    region: Mapped[str | None] = mapped_column(String(64), index=True)
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    point_count: Mapped[int] = mapped_column(Integer(), default=0)
    distance_km: Mapped[float | None] = mapped_column(Float())
    start_lat: Mapped[float | None] = mapped_column(Float())
    start_lon: Mapped[float | None] = mapped_column(Float())
    end_lat: Mapped[float | None] = mapped_column(Float())
    end_lon: Mapped[float | None] = mapped_column(Float())
    # Cached fixed-length resampled feature vector for the baseline anomaly models.
    features: Mapped[list[float] | None] = mapped_column(JsonType)
    # Cached per-step sequence descriptor for the flagship GRU autoencoder; stored in-DB
    # so the SQLite path needs no vector extension.
    sequence: Mapped[list[list[float]] | None] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Zone(Base):
    """An airspace reference area — a terminal area, corridor, or watch/border box.

    Geometry is a simple (lat, lon) ring in `polygon`; membership is tested with the pure
    point-in-polygon in `horus.geo` (no PostGIS dependency). See `horus.zones` — rings are
    coarse and illustrative, never authoritative airspace boundaries.
    """

    __tablename__ = "zones"

    zone_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32), index=True)  # terminal|corridor|watch|border
    country: Mapped[str | None] = mapped_column(String(64))
    polygon: Mapped[list[list[float]]] = mapped_column(JsonType)  # [[lat, lon], ...]
    sensitive: Mapped[int] = mapped_column(Integer(), default=0)  # 1 = watch zone (weights risk)
    notes: Mapped[str | None] = mapped_column(Text())


class Incident(Base):
    """A detector output — a scored, source-rated air-domain incident for human review.

    `reliability` is a NATO-Admiralty-style grade (A completely reliable … F cannot be
    judged) reflecting ADS-B confidence: a well-sampled event at altitude in dense
    coverage grades higher than a lone low-altitude dropout, where an apparent "dark
    aircraft" is usually just terrain-limited reception. GNSS-interference incidents are
    *area-level* (icao24 is NULL; the evidence lists the affected aircraft). An incident
    is decision support, NEVER an automated verdict of hostile or illicit activity.
    """

    __tablename__ = "incidents"

    incident_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    # NULL for area-level incidents (GNSS-interference cells); set for per-aircraft ones.
    icao24: Mapped[str | None] = mapped_column(ForeignKey("aircraft.icao24"), index=True)
    track_id: Mapped[str | None] = mapped_column(ForeignKey("tracks.track_id"))
    # detector: jamming | gap | incursion | spoof | anomaly
    detector: Mapped[str] = mapped_column(String(32), index=True)
    incident_type: Mapped[str] = mapped_column(String(64))  # human label, e.g. "GNSS interference"
    score: Mapped[float] = mapped_column(Float())  # detector confidence [0,1]
    severity: Mapped[str] = mapped_column(String(16), default="low")  # low|moderate|high|critical
    reliability: Mapped[str] = mapped_column(String(1), default="F")  # ADS-B confidence A..F
    ts_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ts_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lat: Mapped[float | None] = mapped_column(Float())
    lon: Mapped[float | None] = mapped_column(Float())
    zone_id: Mapped[str | None] = mapped_column(ForeignKey("zones.zone_id"))
    # Aircraft observed in an area-level incident's cell/window (jamming cluster size).
    affected_count: Mapped[int | None] = mapped_column(Integer())
    # Air-threat tags (analogue of SENTINEL's ATT&CK techniques) + the detector's
    # transparent evidence (thresholds crossed, NIC distribution, gap duration, etc.).
    techniques: Mapped[list[str] | None] = mapped_column(JsonType)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    region: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
