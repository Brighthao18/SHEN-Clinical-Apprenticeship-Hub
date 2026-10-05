"""Phase 6.1 runtime AI provider settings

Revision ID: c7e4a91b2d50
Revises: a52c9d31f7b8
Create Date: 2026-08-29

Adds a single-slot runtime configuration table for the structured-output AI
provider used by the ClinicalAIExtractor. The API key is stored with the
same field-level encryption as patient identity (EncryptedText) and is never
returned by any API response.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c7e4a91b2d50"
down_revision: str | Sequence[str] | None = "a52c9d31f7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_settings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slot", sa.String(length=40), nullable=False, unique=True),
        sa.Column("adapter", sa.String(length=40), nullable=False),
        sa.Column("base_url", sa.String(length=255), nullable=False),
        sa.Column("model", sa.String(length=80), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "confirmed_identifiable_upload", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("provider_settings")
