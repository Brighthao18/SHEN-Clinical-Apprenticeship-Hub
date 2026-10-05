"""Phase 9 evidence snapshots on apprenticeship note versions

Revision ID: e5c8a7d1b3f2
Revises: e8f1b4c7a250
Create Date: 2026-09-09

Stores each version's actually-adopted evidence set (the de-identified
writing context plus context/writing/diff versions) so a version's materials
stay rebuildable instead of surviving only as an encounter-id list and a
hash. The column is nullable on purpose: versions written before this change
keep NULL and are honestly labelled — never backfilled from current data.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e5c8a7d1b3f2"
down_revision: str | Sequence[str] | None = "e8f1b4c7a250"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "apprenticeship_note_versions",
        sa.Column("evidence_snapshot", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("apprenticeship_note_versions", "evidence_snapshot")
