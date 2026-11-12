"""add reader setup QR counter

Revision ID: f2b6d8e4a175
Revises: e8a4c2d6f913
Create Date: 2026-11-12 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f2b6d8e4a175"
down_revision: Union[str, None] = "e8a4c2d6f913"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the per-reader setup QR counter."""
    op.add_column(
        "restaurant_reader",
        sa.Column("setup_ctr", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    """Drop the per-reader setup QR counter."""
    op.drop_column("restaurant_reader", "setup_ctr")
