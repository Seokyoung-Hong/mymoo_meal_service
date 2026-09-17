"""add restaurant reader registrations

Revision ID: c9d2f4a7b103
Revises: b1f3c7d9e204
Create Date: 2026-09-18 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c9d2f4a7b103"
down_revision: Union[str, None] = "b1f3c7d9e204"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the restaurant reader registration table."""
    op.create_table(
        "restaurant_reader",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("bt_mac", sa.String(length=12), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column(
            "last_config_ctr",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("last_config_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["Restaurant.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bt_mac"),
    )
    op.create_index(
        "restaurant_reader_restaurant_index",
        "restaurant_reader",
        ["restaurant_id"],
    )


def downgrade() -> None:
    """Drop the restaurant reader registration table."""
    op.drop_index(
        "restaurant_reader_restaurant_index",
        table_name="restaurant_reader",
    )
    op.drop_table("restaurant_reader")
