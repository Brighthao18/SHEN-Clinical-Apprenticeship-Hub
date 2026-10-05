"""phase5.2 unresolved ingestion policy

Revision ID: a52c9d31f7b8
Revises: 91b3c7e2a6d4
Create Date: 2026-08-29 00:00:00

Adds the UNRESOLVED terminal status, the unresolved-ingestion retry ledger,
review-item policy classification columns, and the import-batch unresolved
counter.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a52c9d31f7b8"
down_revision: str | Sequence[str] | None = "91b3c7e2a6d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROCESSING_STATUS_VALUES = (
    "UPLOADED",
    "QUEUED",
    "OCR_RUNNING",
    "OCR_COMPLETED",
    "TEMPLATE_DETECTED",
    "PARSING",
    "STRUCTURING",
    "PATIENT_MATCHING",
    "NORMALIZING",
    "VALIDATING",
    "REVIEW_REQUIRED",
    "ACCEPTED",
    "UNRESOLVED",
    "FAILED",
)
_STATUS_LITERAL = "(" + ", ".join(f"'{value}'" for value in PROCESSING_STATUS_VALUES) + ")"

# (table, column, check-constraint name) for every ProcessingStatus enum.
_PROCESSING_STATUS_CONSTRAINTS = (
    ("documents", "processing_status", "document_processing_status_enum"),
    ("import_batches", "status", "processing_status_enum"),
    ("import_items", "status", "import_item_status_enum"),
    ("processing_jobs", "stage", "processing_job_stage_enum"),
)


def _swap_processing_status_constraints() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table, column, name in _PROCESSING_STATUS_CONSTRAINTS:
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
            op.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} "
                f"CHECK ({column} IN {_STATUS_LITERAL})"
            )
        return
    # SQLite (test backend): rebuild from current model metadata so the
    # reflected CHECK constraints pick up the new enum value.
    from app.models.domain import Base

    for table, _, _ in _PROCESSING_STATUS_CONSTRAINTS:
        with op.batch_alter_table(
            table, copy_from=Base.metadata.tables[table], recreate="always"
        ):
            pass


def upgrade() -> None:
    with op.batch_alter_table("review_items") as batch_op:
        batch_op.add_column(sa.Column("reason_category", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("criticality", sa.String(length=20), nullable=True))
        batch_op.add_column(
            sa.Column(
                "blocking",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
    with op.batch_alter_table("import_batches") as batch_op:
        batch_op.add_column(
            sa.Column(
                "unresolved_items",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )

    op.create_table(
        "unresolved_ingestions",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("template", sa.String(length=100), nullable=False),
        sa.Column(
            "failure_stage",
            sa.Enum(
                *PROCESSING_STATUS_VALUES,
                name="processing_job_stage_enum",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("failure_reason_code", sa.String(length=80), nullable=False),
        sa.Column("reason_category", sa.String(length=40), nullable=False),
        sa.Column("critical_fields_affected", sa.JSON(), nullable=False),
        sa.Column("parser_name", sa.String(length=100), nullable=True),
        sa.Column("parser_version", sa.String(length=32), nullable=True),
        sa.Column("normalizer_version", sa.String(length=32), nullable=True),
        sa.Column("validation_policy_version", sa.String(length=32), nullable=False),
        sa.Column("ocr_provider", sa.String(length=80), nullable=True),
        sa.Column("ocr_version", sa.String(length=80), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.String(length=40), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", name="uq_unresolved_ingestions_document"),
    )
    op.create_index(
        "ix_unresolved_ingestions_template", "unresolved_ingestions", ["template"], unique=False
    )
    op.create_index(
        "ix_unresolved_ingestions_last_attempt",
        "unresolved_ingestions",
        ["last_attempt_at"],
        unique=False,
    )

    _swap_processing_status_constraints()


def downgrade() -> None:
    bind = op.get_bind()
    unresolved = bind.execute(
        sa.text(
            "SELECT count(*) FROM unresolved_ingestions "
            "WHERE resolved_at IS NULL OR resolution IS NOT NULL"
        )
    ).scalar_one()
    if unresolved:
        raise RuntimeError(
            "Cannot safely downgrade: unresolved-ingestion ledger rows would lose information"
        )
    unresolved_documents = bind.execute(
        sa.text("SELECT count(*) FROM documents WHERE processing_status = 'UNRESOLVED'")
    ).scalar_one()
    if unresolved_documents:
        raise RuntimeError("Cannot safely downgrade: documents are still UNRESOLVED")

    old_values = tuple(value for value in PROCESSING_STATUS_VALUES if value != "UNRESOLVED")
    old_literal = "(" + ", ".join(f"'{value}'" for value in old_values) + ")"
    if bind.dialect.name == "postgresql":
        for table, column, name in _PROCESSING_STATUS_CONSTRAINTS:
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
            op.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} "
                f"CHECK ({column} IN {old_literal})"
            )
    else:
        from app.models.domain import Base

        # Rebuilding from current metadata keeps the 5.2 constraint text on
        # the SQLite test backend; only PostgreSQL restores it exactly.
        for table, _, _ in _PROCESSING_STATUS_CONSTRAINTS:
            with op.batch_alter_table(
                table, copy_from=Base.metadata.tables[table], recreate="always"
            ):
                pass

    op.drop_index(
        "ix_unresolved_ingestions_last_attempt", table_name="unresolved_ingestions"
    )
    op.drop_index("ix_unresolved_ingestions_template", table_name="unresolved_ingestions")
    op.drop_table("unresolved_ingestions")
    with op.batch_alter_table("import_batches") as batch_op:
        batch_op.drop_column("unresolved_items")
    with op.batch_alter_table("review_items") as batch_op:
        batch_op.drop_column("blocking")
        batch_op.drop_column("criticality")
        batch_op.drop_column("reason_category")
