"""add source min_points

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-02 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sources", schema=None) as batch_op:
        batch_op.add_column(sa.Column("min_points", sa.Integer(), nullable=True))
        batch_op.create_check_constraint("ck_sources_min_points", "min_points >= 0")


def downgrade() -> None:
    with op.batch_alter_table("sources", schema=None) as batch_op:
        batch_op.drop_constraint("ck_sources_min_points", type_="check")
        batch_op.drop_column("min_points")
