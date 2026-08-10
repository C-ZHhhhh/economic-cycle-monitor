"""Add source-neutral mapping request parameters for BEA.

Revision ID: 0004_stage2b_bea
Revises: 0003_stage2b_official_sources
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_stage2b_bea"
down_revision: str | None = "0003_stage2b_official_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add one provider-neutral JSON object for table-oriented source parameters."""
    op.add_column(
        "indicator_source_mappings",
        sa.Column(
            "source_parameters_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Remove only the Stage 2B-2 source-parameter column."""
    op.drop_column("indicator_source_mappings", "source_parameters_json")
