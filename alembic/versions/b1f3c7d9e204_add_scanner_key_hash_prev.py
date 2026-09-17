"""keep the previous scanner key hash during key rotation

Revision ID: b1f3c7d9e204
Revises: c7a1e5b9d2f3
Create Date: 2026-09-18 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b1f3c7d9e204"
down_revision: Union[str, None] = "c7a1e5b9d2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the rotation grace column so old device keys keep working briefly."""
    op.add_column(
        "Restaurant",
        sa.Column("scanner_key_hash_prev", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """Drop the rotation grace column."""
    op.drop_column("Restaurant", "scanner_key_hash_prev")
