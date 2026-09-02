"""meal allowance wallet: bucket balances, scanner keys

Revision ID: c7a1e5b9d2f3
Revises: b6e4d7a2c9f1
Create Date: 2026-09-02 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c7a1e5b9d2f3"
down_revision: Union[str, None] = "b6e4d7a2c9f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Turn one-use tickets into balance buckets and add restaurant scanner keys."""
    op.add_column(
        "meal_ticket",
        sa.Column("remaining_amount", sa.Integer(), nullable=False, server_default="0"),
    )
    # 1회용 대기(pending) 상태는 사라진다: 대기 중이던 식권은 잔액을 되돌려 준다.
    op.execute("UPDATE meal_ticket SET status = 'available' WHERE status = 'pending'")
    op.execute(
        "UPDATE meal_ticket SET remaining_amount = amount WHERE status = 'available'"
    )
    op.execute("DELETE FROM meal_ticket_usage_request WHERE status = 'pending'")
    with op.batch_alter_table("meal_ticket") as batch:
        batch.alter_column("remaining_amount", server_default=None)
        batch.drop_constraint("meal_ticket_status_check", type_="check")
        batch.create_check_constraint(
            "meal_ticket_status_check",
            "status IN ('available', 'used', 'expired')",
        )
        batch.create_check_constraint(
            "meal_ticket_remaining_check",
            "remaining_amount >= 0 AND remaining_amount <= amount",
        )
    with op.batch_alter_table("meal_ticket_usage_request") as batch:
        batch.alter_column("ticket_id", existing_type=sa.Integer(), nullable=True)

    op.add_column(
        "Restaurant",
        sa.Column("scanner_key_hash", sa.String(length=64), nullable=True),
    )
    op.create_unique_constraint(
        "restaurant_scanner_key_hash_unique", "Restaurant", ["scanner_key_hash"]
    )


def downgrade() -> None:
    """Revert to one-use tickets. Fails if any usage request has no ticket."""
    op.drop_constraint(
        "restaurant_scanner_key_hash_unique", "Restaurant", type_="unique"
    )
    op.drop_column("Restaurant", "scanner_key_hash")
    with op.batch_alter_table("meal_ticket_usage_request") as batch:
        batch.alter_column("ticket_id", existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table("meal_ticket") as batch:
        batch.drop_constraint("meal_ticket_remaining_check", type_="check")
        batch.drop_constraint("meal_ticket_status_check", type_="check")
        batch.create_check_constraint(
            "meal_ticket_status_check",
            "status IN ('available', 'pending', 'used', 'expired')",
        )
        batch.drop_column("remaining_amount")
