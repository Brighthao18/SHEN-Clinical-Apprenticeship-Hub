"""Phase 6.2 vision verification fallback records

Revision ID: b8d2f6a4c1e9
Revises: f2a4c6e8b930
Create Date: 2026-08-30

Adds the `vision_verifications` evidence table for the Vision Verification
Fallback: one row per constrained image re-read triggered by validator
blocking codes that mean OCR-source incompleteness. The row preserves the
image reference/hash, the exact requested fields, the complete provider
response, and the sanitized applied fields with
`source=ai_visual_verification` provenance.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8d2f6a4c1e9"
down_revision: str | Sequence[str] | None = "f2a4c6e8b930"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "vision_verifications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=80), nullable=False),
        sa.Column("image_sha256", sa.String(length=64), nullable=False),
        sa.Column("image_reference", sa.String(length=512), nullable=True),
        sa.Column("issue_codes", sa.JSON(), nullable=False),
        sa.Column("requested_fields", sa.JSON(), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("applied_fields", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_vision_verifications_document",
        "vision_verifications",
        ["document_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_vision_verifications_document", table_name="vision_verifications")
    op.drop_table("vision_verifications")
