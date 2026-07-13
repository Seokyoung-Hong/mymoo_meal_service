"""add worker meal tickets and cash wallet

Revision ID: a8c9e1d4b7f6
Revises: f4c2b8a1d9e0
Create Date: 2026-07-09 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a8c9e1d4b7f6"
down_revision: Union[str, None] = "f4c2b8a1d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create worker ticket and cash wallet tables."""
    op.create_table(
        "meal_ticket",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("expires_on", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="available",
            nullable=False,
        ),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount > 0", name="meal_ticket_amount_positive_check"),
        sa.CheckConstraint(
            "status IN ('available', 'pending', 'used', 'expired')",
            name="meal_ticket_status_check",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["User.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index("meal_ticket_owner_index", "meal_ticket", ["owner_id"])
    op.create_index("meal_ticket_status_index", "meal_ticket", ["status"])
    op.create_index("meal_ticket_expires_on_index", "meal_ticket", ["expires_on"])

    op.create_table(
        "cash_wallet",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("balance", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("balance >= 0", name="cash_wallet_balance_check"),
        sa.ForeignKeyConstraint(["user_id"], ["User.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="cash_wallet_user_unique"),
    )

    op.create_table(
        "meal_ticket_usage_request",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("meal_type", sa.String(length=32), nullable=True),
        sa.Column("served_date", sa.Date(), nullable=False),
        sa.Column("meal_price", sa.Integer(), nullable=False),
        sa.Column("ticket_amount_applied", sa.Integer(), nullable=False),
        sa.Column("cash_amount_required", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "meal_price > 0",
            name="ticket_request_meal_price_check",
        ),
        sa.CheckConstraint(
            "ticket_amount_applied >= 0",
            name="ticket_request_ticket_amount_check",
        ),
        sa.CheckConstraint(
            "cash_amount_required >= 0",
            name="ticket_request_cash_amount_check",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'used')",
            name="ticket_request_status_check",
        ),
        sa.ForeignKeyConstraint(["approved_by"], ["User.id"]),
        sa.ForeignKeyConstraint(["restaurant_id"], ["Restaurant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ticket_id"], ["meal_ticket.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["worker_id"], ["User.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ticket_request_worker_index",
        "meal_ticket_usage_request",
        ["worker_id"],
    )
    op.create_index(
        "ticket_request_restaurant_index",
        "meal_ticket_usage_request",
        ["restaurant_id"],
    )
    op.create_index(
        "ticket_request_status_index",
        "meal_ticket_usage_request",
        ["status"],
    )
    op.create_index(
        "ticket_request_ticket_index",
        "meal_ticket_usage_request",
        ["ticket_id"],
    )

    op.create_table(
        "cash_transaction",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("usage_request_id", sa.Integer(), nullable=True),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("transaction_type", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="succeeded",
            nullable=False,
        ),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount != 0", name="cash_transaction_amount_check"),
        sa.CheckConstraint(
            "transaction_type IN ('mock_card_charge', 'ticket_shortfall_payment')",
            name="cash_transaction_type_check",
        ),
        sa.CheckConstraint(
            "status IN ('succeeded')",
            name="cash_transaction_status_check",
        ),
        sa.ForeignKeyConstraint(
            ["usage_request_id"],
            ["meal_ticket_usage_request.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["User.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "cash_transaction_user_index",
        "cash_transaction",
        ["user_id"],
    )
    op.create_index(
        "cash_transaction_created_at_index",
        "cash_transaction",
        ["created_at"],
    )
    op.create_index(
        "cash_transaction_usage_request_index",
        "cash_transaction",
        ["usage_request_id"],
    )


def downgrade() -> None:
    """Drop worker ticket and cash wallet tables."""
    op.drop_index(
        "cash_transaction_usage_request_index",
        table_name="cash_transaction",
    )
    op.drop_index("cash_transaction_created_at_index", table_name="cash_transaction")
    op.drop_index("cash_transaction_user_index", table_name="cash_transaction")
    op.drop_table("cash_transaction")

    op.drop_index(
        "ticket_request_ticket_index",
        table_name="meal_ticket_usage_request",
    )
    op.drop_index(
        "ticket_request_status_index",
        table_name="meal_ticket_usage_request",
    )
    op.drop_index(
        "ticket_request_restaurant_index",
        table_name="meal_ticket_usage_request",
    )
    op.drop_index(
        "ticket_request_worker_index",
        table_name="meal_ticket_usage_request",
    )
    op.drop_table("meal_ticket_usage_request")

    op.drop_table("cash_wallet")

    op.drop_index("meal_ticket_expires_on_index", table_name="meal_ticket")
    op.drop_index("meal_ticket_status_index", table_name="meal_ticket")
    op.drop_index("meal_ticket_owner_index", table_name="meal_ticket")
    op.drop_table("meal_ticket")
