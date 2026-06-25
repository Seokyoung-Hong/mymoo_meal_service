"""add restaurant is active

Revision ID: e3b1c9d4a6f2
Revises: d8b8f1f2c3a4
Create Date: 2026-06-24 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e3b1c9d4a6f2"
down_revision: Union[str, None] = "d8b8f1f2c3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add non-null active status to existing and future restaurants."""
    op.add_column(
        "Restaurant",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.execute(
        sa.text('UPDATE "Restaurant" SET is_active = TRUE WHERE is_active IS NULL')
    )
    op.create_index("restaurant_is_active_index", "Restaurant", ["is_active"])


def downgrade() -> None:
    """Remove restaurant active status."""
    op.drop_index("restaurant_is_active_index", table_name="Restaurant")
    op.drop_column("Restaurant", "is_active")
