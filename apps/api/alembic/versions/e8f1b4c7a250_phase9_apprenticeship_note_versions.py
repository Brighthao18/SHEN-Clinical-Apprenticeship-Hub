"""Phase 9 apprenticeship note versions

Revision ID: e8f1b4c7a250
Revises: b8d2f6a4c1e9
Create Date: 2026-08-30

Adds the append-only ``apprenticeship_note_versions`` table for Phase 9
师承写作 (跟师笔记 / 医案整理). Versions are grouped by ``note_id`` and
permanently retain the user_original / ai_draft / user_draft / user_final
layers; AI drafts keep provider, model, prompt version, structured output
and the source context hash.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e8f1b4c7a250"
down_revision: str | Sequence[str] | None = "b8d2f6a4c1e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "apprenticeship_note_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("note_id", sa.Uuid(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column(
            "note_type",
            sa.Enum("teacher_note", "case_record", name="apprenticeship_note_type_enum",
                    native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "version_type",
            sa.Enum("user_original", "ai_draft", "user_draft", "user_final",
                    name="note_version_type_enum", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=160), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("structured_output", sa.JSON(), nullable=True),
        sa.Column(
            "evidence_encounter_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")
        ),
        sa.Column("source_context_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("prompt_version", sa.Integer(), nullable=True),
        sa.Column("deidentification_version", sa.String(length=32), nullable=False),
        sa.Column("based_on_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["based_on_version_id"],
            ["apprenticeship_note_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"],
            ["patients.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("note_id", "version", name="uq_apprenticeship_note_version"),
    )
    op.create_index(
        "ix_note_versions_patient_created",
        "apprenticeship_note_versions",
        ["patient_id", "created_at"],
    )
    op.create_index(
        "ix_note_versions_note_version",
        "apprenticeship_note_versions",
        ["note_id", "version"],
    )


def downgrade() -> None:
    op.drop_index("ix_note_versions_note_version", table_name="apprenticeship_note_versions")
    op.drop_index("ix_note_versions_patient_created", table_name="apprenticeship_note_versions")
    op.drop_table("apprenticeship_note_versions")
