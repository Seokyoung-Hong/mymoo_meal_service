"""add restaurant pricing policy

Revision ID: f4c2b8a1d9e0
Revises: e3b1c9d4a6f2
Create Date: 2026-06-24 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f4c2b8a1d9e0"
down_revision: Union[str, None] = "e3b1c9d4a6f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create flexible restaurant pricing policy storage."""
    op.create_table(
        "restaurant_pricing_policy",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("policy_type", sa.String(length=32), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("meal_type_id", sa.Integer(), nullable=True),
        sa.Column("served_date", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("price > 0", name="pricing_policy_price_positive_check"),
        sa.CheckConstraint(
            "policy_type IN ('restaurant_fixed', 'meal_type_fixed', 'date_specific')",
            name="pricing_policy_type_check",
        ),
        sa.CheckConstraint(
            "(policy_type = 'restaurant_fixed' AND meal_type_id IS NULL AND served_date IS NULL) OR "
            "(policy_type = 'meal_type_fixed' AND meal_type_id IS NOT NULL AND served_date IS NULL) OR "
            "(policy_type = 'date_specific' AND served_date IS NOT NULL)",
            name="pricing_policy_scope_check",
        ),
        sa.ForeignKeyConstraint(
            ["restaurant_id"],
            ["Restaurant.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["meal_type_id"], ["meal_type.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "pricing_policy_restaurant_index",
        "restaurant_pricing_policy",
        ["restaurant_id"],
    )
    op.create_index(
        "pricing_policy_meal_type_index",
        "restaurant_pricing_policy",
        ["meal_type_id"],
    )
    op.create_index(
        "pricing_policy_served_date_index",
        "restaurant_pricing_policy",
        ["served_date"],
    )
    op.create_index(
        "pricing_active_restaurant_fixed_unique",
        "restaurant_pricing_policy",
        ["restaurant_id"],
        unique=True,
        postgresql_where=sa.text(
            "is_active = true AND policy_type = 'restaurant_fixed'"
        ),
        sqlite_where=sa.text("is_active = 1 AND policy_type = 'restaurant_fixed'"),
    )
    op.create_index(
        "pricing_active_meal_type_fixed_unique",
        "restaurant_pricing_policy",
        ["restaurant_id", "meal_type_id"],
        unique=True,
        postgresql_where=sa.text(
            "is_active = true AND policy_type = 'meal_type_fixed'"
        ),
        sqlite_where=sa.text("is_active = 1 AND policy_type = 'meal_type_fixed'"),
    )
    op.create_index(
        "pricing_active_date_type_unique",
        "restaurant_pricing_policy",
        ["restaurant_id", "meal_type_id", "served_date"],
        unique=True,
        postgresql_where=sa.text(
            "is_active = true AND policy_type = 'date_specific' AND meal_type_id IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "is_active = 1 AND policy_type = 'date_specific' AND meal_type_id IS NOT NULL"
        ),
    )
    op.create_index(
        "pricing_active_date_unique",
        "restaurant_pricing_policy",
        ["restaurant_id", "served_date"],
        unique=True,
        postgresql_where=sa.text(
            "is_active = true AND policy_type = 'date_specific' AND meal_type_id IS NULL"
        ),
        sqlite_where=sa.text(
            "is_active = 1 AND policy_type = 'date_specific' AND meal_type_id IS NULL"
        ),
    )


def downgrade() -> None:
    """Drop flexible restaurant pricing policy storage."""
    op.drop_index("pricing_active_date_unique", table_name="restaurant_pricing_policy")
    op.drop_index(
        "pricing_active_date_type_unique", table_name="restaurant_pricing_policy"
    )
    op.drop_index(
        "pricing_active_meal_type_fixed_unique", table_name="restaurant_pricing_policy"
    )
    op.drop_index(
        "pricing_active_restaurant_fixed_unique", table_name="restaurant_pricing_policy"
    )
    op.drop_index(
        "pricing_policy_served_date_index", table_name="restaurant_pricing_policy"
    )
    op.drop_index(
        "pricing_policy_meal_type_index", table_name="restaurant_pricing_policy"
    )
    op.drop_index(
        "pricing_policy_restaurant_index", table_name="restaurant_pricing_policy"
    )
    op.drop_table("restaurant_pricing_policy")
