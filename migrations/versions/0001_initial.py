"""initial schema: aircraft, positions, tracks, zones, incidents, collector ledger

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirror horus.db.models.JsonType: JSONB on Postgres, plain JSON elsewhere — so the
# migration produces the same column type the ORM maps to (no json/jsonb drift on PG).
_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "aircraft",
        sa.Column("icao24", sa.String(16), primary_key=True),
        sa.Column("callsign", sa.String(16), nullable=True),
        sa.Column("registration", sa.String(16), nullable=True),
        sa.Column("type_code", sa.String(8), nullable=True),
        sa.Column("category", sa.String(4), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "zones",
        sa.Column("zone_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("country", sa.String(64), nullable=True),
        sa.Column("polygon", _JSON, nullable=False),
        sa.Column("sensitive", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index("ix_zones_kind", "zones", ["kind"])

    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("icao24", sa.String(16), sa.ForeignKey("aircraft.icao24"), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lon", sa.Float(), nullable=False),
        sa.Column("alt_baro_ft", sa.Float(), nullable=True),
        sa.Column("alt_geom_ft", sa.Float(), nullable=True),
        sa.Column("gs_kt", sa.Float(), nullable=True),
        sa.Column("track_deg", sa.Float(), nullable=True),
        sa.Column("baro_rate_fpm", sa.Float(), nullable=True),
        sa.Column("on_ground", sa.Boolean(), nullable=False),
        sa.Column("squawk", sa.String(8), nullable=True),
        sa.Column("nic", sa.Integer(), nullable=True),
        sa.Column("nac_p", sa.Integer(), nullable=True),
        sa.Column("sil", sa.Integer(), nullable=True),
        sa.Column("rssi", sa.Float(), nullable=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("region", sa.String(64), nullable=True),
        sa.Column("raw", _JSON, nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_positions_icao24", "positions", ["icao24"])
    op.create_index("ix_positions_ts", "positions", ["ts"])
    op.create_index("ix_positions_source", "positions", ["source"])
    op.create_index("ix_positions_region", "positions", ["region"])
    op.create_index("ix_positions_icao24_ts", "positions", ["icao24", "ts"])

    op.create_table(
        "collector_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_reason", sa.String(255), nullable=True),
        sa.Column("report_count", sa.Integer(), nullable=False),
        sa.Column("aircraft_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
    )
    op.create_index("ix_collector_runs_started_at", "collector_runs", ["started_at"])
    op.create_index("ix_collector_runs_status", "collector_runs", ["status"])

    op.create_table(
        "coverage_outages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column(
            "run_id", sa.Integer(), sa.ForeignKey("collector_runs.id"), nullable=False
        ),
    )
    op.create_index("ix_coverage_outages_opened_at", "coverage_outages", ["opened_at"])
    op.create_index("ix_coverage_outages_run_id", "coverage_outages", ["run_id"])

    op.create_table(
        "tracks",
        sa.Column("track_id", sa.String(96), primary_key=True),
        sa.Column("icao24", sa.String(16), sa.ForeignKey("aircraft.icao24"), nullable=False),
        sa.Column("region", sa.String(64), nullable=True),
        sa.Column("start_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("point_count", sa.Integer(), nullable=False),
        sa.Column("distance_km", sa.Float(), nullable=True),
        sa.Column("start_lat", sa.Float(), nullable=True),
        sa.Column("start_lon", sa.Float(), nullable=True),
        sa.Column("end_lat", sa.Float(), nullable=True),
        sa.Column("end_lon", sa.Float(), nullable=True),
        sa.Column("features", _JSON, nullable=True),
        sa.Column("sequence", _JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tracks_icao24", "tracks", ["icao24"])
    op.create_index("ix_tracks_region", "tracks", ["region"])
    op.create_index("ix_tracks_start_ts", "tracks", ["start_ts"])

    op.create_table(
        "incidents",
        sa.Column("incident_id", sa.String(96), primary_key=True),
        sa.Column("icao24", sa.String(16), sa.ForeignKey("aircraft.icao24"), nullable=True),
        sa.Column("track_id", sa.String(96), sa.ForeignKey("tracks.track_id"), nullable=True),
        sa.Column("detector", sa.String(32), nullable=False),
        sa.Column("incident_type", sa.String(64), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("reliability", sa.String(1), nullable=False),
        sa.Column("ts_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ts_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("zone_id", sa.String(64), sa.ForeignKey("zones.zone_id"), nullable=True),
        sa.Column("affected_count", sa.Integer(), nullable=True),
        sa.Column("techniques", _JSON, nullable=True),
        sa.Column("evidence", _JSON, nullable=True),
        sa.Column("region", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_incidents_icao24", "incidents", ["icao24"])
    op.create_index("ix_incidents_detector", "incidents", ["detector"])
    op.create_index("ix_incidents_ts_start", "incidents", ["ts_start"])
    op.create_index("ix_incidents_region", "incidents", ["region"])


def downgrade() -> None:
    op.drop_table("incidents")
    op.drop_table("tracks")
    op.drop_table("coverage_outages")
    op.drop_table("collector_runs")
    op.drop_table("positions")
    op.drop_table("zones")
    op.drop_table("aircraft")
