"""phase5 safety foundation

Revision ID: 91b3c7e2a6d4
Revises: 723f1f7316ab
Create Date: 2026-08-29 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "91b3c7e2a6d4"
down_revision: str | Sequence[str] | None = "723f1f7316ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MANUAL_SOURCE_MARKER = "__SHEN_P5_SOURCE_TYPE_MANUAL_VERIFIED__:"
LEGACY_SOURCE_MARKER = "__SHEN_P5_SOURCE_TYPE_LEGACY_UNVERIFIED__:"


def upgrade() -> None:
    op.add_column(
        "hospital_drug_specifications",
        sa.Column("source_type", sa.String(length=40), nullable=True),
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "UPDATE hospital_drug_specifications SET "
            "source_type = CASE "
            f"WHEN LEFT(source, {len(MANUAL_SOURCE_MARKER)}) = "
            f"'{MANUAL_SOURCE_MARKER}' THEN 'manual_verified' "
            "ELSE 'legacy_unverified' END, "
            "source = CASE "
            f"WHEN LEFT(source, {len(MANUAL_SOURCE_MARKER)}) = "
            f"'{MANUAL_SOURCE_MARKER}' THEN SUBSTRING(source FROM "
            f"{len(MANUAL_SOURCE_MARKER) + 1}) "
            f"WHEN LEFT(source, {len(LEGACY_SOURCE_MARKER)}) = "
            f"'{LEGACY_SOURCE_MARKER}' THEN SUBSTRING(source FROM "
            f"{len(LEGACY_SOURCE_MARKER) + 1}) "
            "ELSE source END"
        )
    else:
        op.execute(
            "UPDATE hospital_drug_specifications SET "
            "source_type = CASE "
            f"WHEN SUBSTR(source, 1, {len(MANUAL_SOURCE_MARKER)}) = "
            f"'{MANUAL_SOURCE_MARKER}' THEN 'manual_verified' "
            "ELSE 'legacy_unverified' END, "
            "source = CASE "
            f"WHEN SUBSTR(source, 1, {len(MANUAL_SOURCE_MARKER)}) = "
            f"'{MANUAL_SOURCE_MARKER}' THEN SUBSTR(source, "
            f"{len(MANUAL_SOURCE_MARKER) + 1}) "
            f"WHEN SUBSTR(source, 1, {len(LEGACY_SOURCE_MARKER)}) = "
            f"'{LEGACY_SOURCE_MARKER}' THEN SUBSTR(source, "
            f"{len(LEGACY_SOURCE_MARKER) + 1}) "
            "ELSE source END"
        )
    with op.batch_alter_table("hospital_drug_specifications") as batch_op:
        batch_op.drop_constraint("uq_hospital_drug_spec_version", type_="unique")
        batch_op.drop_constraint("ck_spec_positive_dose", type_="check")
        batch_op.alter_column(
            "package_unit",
            new_column_name="normalized_package_unit",
            existing_type=sa.String(length=20),
            type_=sa.String(length=40),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "equivalent_dose_g",
            new_column_name="package_dose_g",
            existing_type=sa.Numeric(precision=10, scale=3),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "source",
            new_column_name="source_note",
            existing_type=sa.String(length=255),
            type_=sa.String(length=500),
            existing_nullable=False,
            nullable=True,
        )
        batch_op.alter_column(
            "effective_from",
            existing_type=sa.Date(),
            existing_nullable=False,
            nullable=True,
        )
        batch_op.alter_column(
            "source_type",
            existing_type=sa.String(length=40),
            existing_nullable=True,
            nullable=False,
        )
        batch_op.create_check_constraint("ck_spec_positive_dose", "package_dose_g > 0")
        batch_op.create_check_constraint(
            "ck_spec_source_type",
            "source_type IN ('manual_verified', 'legacy_unverified')",
        )
    # Keep the same-name drop/create in separate batch operations. SQLite's
    # recreate strategy otherwise elides the replacement unique constraint.
    with op.batch_alter_table("hospital_drug_specifications") as batch_op:
        batch_op.create_unique_constraint(
            "uq_hospital_drug_spec_version",
            [
                "hospital",
                "drug_dictionary_id",
                "processing_method",
                "normalized_package_unit",
                "effective_from",
                "version",
            ],
        )
    op.execute(
        "UPDATE hospital_drug_specifications SET source_note = NULL "
        "WHERE source_note = ''"
    )

    op.create_table(
        "ocr_provider_submissions",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("processing_job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("provider_request_id", sa.String(length=160), nullable=True),
        sa.Column("failure_code", sa.String(length=80), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt > 0", name="ck_ocr_provider_submission_attempt_positive"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["processing_job_id"], ["processing_jobs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "processing_job_id", "attempt", name="uq_ocr_provider_submission_attempt"
        ),
    )
    op.create_index(
        "ix_ocr_provider_submission_document",
        "ocr_provider_submissions",
        ["document_id", "submitted_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ocr_provider_submission_document",
        table_name="ocr_provider_submissions",
    )
    op.drop_table("ocr_provider_submissions")

    bind = op.get_bind()
    maximum_note_length = 255 - max(
        len(MANUAL_SOURCE_MARKER), len(LEGACY_SOURCE_MARKER)
    )
    oversized_note_count = bind.execute(
        sa.text(
            "SELECT count(*) FROM hospital_drug_specifications "
            "WHERE LENGTH(COALESCE(source_note, '')) > :maximum_note_length"
        ),
        {"maximum_note_length": maximum_note_length},
    ).scalar_one()
    if oversized_note_count:
        raise RuntimeError(
            "Cannot safely downgrade: a source note does not fit the legacy column"
        )
    if bind.dialect.name == "postgresql":
        op.execute(
            "UPDATE hospital_drug_specifications SET "
            "source_note = CASE source_type "
            f"WHEN 'manual_verified' THEN '{MANUAL_SOURCE_MARKER}' "
            f"ELSE '{LEGACY_SOURCE_MARKER}' END || COALESCE(source_note, ''), "
            "effective_from = COALESCE(effective_from, CAST(created_at AS DATE))"
        )
    else:
        op.execute(
            "UPDATE hospital_drug_specifications SET "
            "source_note = CASE source_type "
            f"WHEN 'manual_verified' THEN '{MANUAL_SOURCE_MARKER}' "
            f"ELSE '{LEGACY_SOURCE_MARKER}' END || COALESCE(source_note, ''), "
            "effective_from = COALESCE(effective_from, DATE(created_at))"
        )

    with op.batch_alter_table("hospital_drug_specifications") as batch_op:
        batch_op.drop_constraint("uq_hospital_drug_spec_version", type_="unique")
        batch_op.drop_constraint("ck_spec_positive_dose", type_="check")
        batch_op.drop_constraint("ck_spec_source_type", type_="check")
        batch_op.alter_column(
            "normalized_package_unit",
            new_column_name="package_unit",
            existing_type=sa.String(length=40),
            type_=sa.String(length=20),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "package_dose_g",
            new_column_name="equivalent_dose_g",
            existing_type=sa.Numeric(precision=10, scale=3),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "source_note",
            new_column_name="source",
            existing_type=sa.String(length=500),
            type_=sa.String(length=255),
            existing_nullable=True,
            nullable=False,
        )
        batch_op.alter_column(
            "effective_from",
            existing_type=sa.Date(),
            existing_nullable=True,
            nullable=False,
        )
        batch_op.drop_column("source_type")
        batch_op.create_check_constraint("ck_spec_positive_dose", "equivalent_dose_g > 0")
    with op.batch_alter_table("hospital_drug_specifications") as batch_op:
        batch_op.create_unique_constraint(
            "uq_hospital_drug_spec_version",
            [
                "hospital",
                "drug_dictionary_id",
                "processing_method",
                "package_unit",
                "effective_from",
                "version",
            ],
        )
