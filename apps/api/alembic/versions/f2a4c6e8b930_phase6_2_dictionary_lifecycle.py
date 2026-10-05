"""Phase 6.2 drug dictionary candidate lifecycle

Revision ID: f2a4c6e8b930
Revises: d9f0b2c4e617
Create Date: 2026-08-30

Turns the drug dictionary into a gradually completed master-data store:
missing canonical mappings create/reuse PENDING candidates (with usage and
evidence) instead of blocking ingestion; VERIFIED entries resolve; DISABLED
entries never resolve. Existing seed rows default to VERIFIED.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2a4c6e8b930"
down_revision: str | Sequence[str] | None = "d9f0b2c4e617"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "drug_dictionary",
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="VERIFIED"
        ),
    )
    op.add_column(
        "drug_dictionary", sa.Column("candidate_raw_name", sa.String(length=160), nullable=True)
    )
    op.add_column(
        "drug_dictionary", sa.Column("representative_evidence", sa.Text(), nullable=True)
    )
    op.add_column(
        "drug_dictionary", sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "drug_dictionary", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "drug_dictionary",
        sa.Column(
            "usage_count", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "drug_dictionary",
        sa.Column(
            "source_document_ids",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.create_index(
        "ix_drug_dictionary_status", "drug_dictionary", ["status"]
    )
    op.create_index(
        "ix_drug_dictionary_candidate_raw", "drug_dictionary", ["candidate_raw_name"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_drug_dictionary_candidate_raw", table_name="drug_dictionary")
    op.drop_index("ix_drug_dictionary_status", table_name="drug_dictionary")
    op.drop_column("drug_dictionary", "source_document_ids")
    op.drop_column("drug_dictionary", "usage_count")
    op.drop_column("drug_dictionary", "last_seen_at")
    op.drop_column("drug_dictionary", "first_seen_at")
    op.drop_column("drug_dictionary", "representative_evidence")
    op.drop_column("drug_dictionary", "candidate_raw_name")
    op.drop_column("drug_dictionary", "status")
