"""add worker device pubkey for offline token binding

Revision ID: e8a4c2d6f913
Revises: d3e7a1f5c820
Create Date: 2026-11-11 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e8a4c2d6f913"
down_revision: Union[str, None] = "d3e7a1f5c820"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the worker device signing pubkey."""
    op.add_column(
        "User",
        sa.Column("device_pubkey", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """Drop the worker device signing pubkey."""
    op.drop_column("User", "device_pubkey")
