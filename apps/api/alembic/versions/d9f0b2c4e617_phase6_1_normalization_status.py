"""Phase 6.1 normalization status on prescription items

Revision ID: d9f0b2c4e617
Revises: c7e4a91b2d50
Create Date: 2026-08-30

Splits the former blocking UNKNOWN_DRUG semantics: a traceable raw drug name
without a canonical mapping is stored as `normalization_status = 'pending'`
together with the AI's suggested canonical name (never authoritative), so a
coverage gap no longer blocks the whole encounter import.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d9f0b2c4e617"
down_revision: str | Sequence[str] | None = "c7e4a91b2d50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "prescription_items",
        sa.Column("ai_suggested_canonical_name", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "prescription_items",
        sa.Column(
            "normalization_status",
            sa.String(length=20),
            nullable=False,
            server_default="normalized",
        ),
    )
    op.create_index(
        "ix_prescription_items_normalization",
        "prescription_items",
        ["normalization_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_prescription_items_normalization", table_name="prescription_items")
    op.drop_column("prescription_items", "normalization_status")
    op.drop_column("prescription_items", "ai_suggested_canonical_name")
