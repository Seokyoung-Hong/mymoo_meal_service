"""add relay v2 reader pubkey and offline records

Revision ID: d3e7a1f5c820
Revises: c9d2f4a7b103
Create Date: 2026-11-11 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d3e7a1f5c820"
down_revision: Union[str, None] = "c9d2f4a7b103"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the reader signing pubkey and the offline approval record table."""
    op.add_column(
        "restaurant_reader",
        sa.Column("pubkey", sa.String(length=64), nullable=True),
    )
    op.create_table(
        "relay_offline_record",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("reader_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("ticket_id", sa.String(length=36), nullable=False),
        sa.Column("qn", sa.String(length=16), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("usage_request_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["reader_id"],
            ["restaurant_reader.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reader_id", "seq"),
    )
    op.create_index(
        "relay_offline_record_ticket_index",
        "relay_offline_record",
        ["ticket_id", "qn"],
    )


def downgrade() -> None:
    """Drop the offline record table and the reader pubkey."""
    op.drop_index(
        "relay_offline_record_ticket_index",
        table_name="relay_offline_record",
    )
    op.drop_table("relay_offline_record")
    op.drop_column("restaurant_reader", "pubkey")
