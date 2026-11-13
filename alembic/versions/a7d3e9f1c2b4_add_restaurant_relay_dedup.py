"""add restaurant relay dedup window

Revision ID: a7d3e9f1c2b4
Revises: f2b6d8e4a175
Create Date: 2026-11-14 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a7d3e9f1c2b4"
down_revision: Union[str, None] = "f2b6d8e4a175"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the per-restaurant re-payment block window (seconds, NULL = default)."""
    op.add_column(
        "Restaurant", sa.Column("relay_dedup_seconds", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    """Drop the per-restaurant re-payment block window."""
    op.drop_column("Restaurant", "relay_dedup_seconds")
