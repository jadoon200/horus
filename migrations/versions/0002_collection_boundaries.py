"""record the collection boundary on each collector run

Revision ID: 0002_collection_boundaries
Revises: 0001_initial
Create Date: 2026-07-25

Historical runs remain NULL deliberately: backfilling every old run with Singapore's
default circle would assign false provenance to the Baltic control lane.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_collection_boundaries"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("collector_runs", sa.Column("region", sa.String(64), nullable=True))
    op.add_column("collector_runs", sa.Column("center_lat", sa.Float(), nullable=True))
    op.add_column("collector_runs", sa.Column("center_lon", sa.Float(), nullable=True))
    op.add_column("collector_runs", sa.Column("radius_nm", sa.Integer(), nullable=True))
    op.create_index("ix_collector_runs_region", "collector_runs", ["region"])


def downgrade() -> None:
    op.drop_index("ix_collector_runs_region", table_name="collector_runs")
    op.drop_column("collector_runs", "radius_nm")
    op.drop_column("collector_runs", "center_lon")
    op.drop_column("collector_runs", "center_lat")
    op.drop_column("collector_runs", "region")
