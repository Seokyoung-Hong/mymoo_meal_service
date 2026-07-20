"""add generic image upload tracking table

Revision ID: b6e4d7a2c9f1
Revises: a8c9e1d4b7f6
Create Date: 2026-07-20 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b6e4d7a2c9f1"
down_revision: Union[str, None] = "a8c9e1d4b7f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the image tracking table."""
    op.create_table(
        "image",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("stored_name", sa.String(length=64), nullable=False),
        sa.Column("image_type", sa.String(length=32), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=True),
        sa.Column("uploader_id", sa.Integer(), nullable=True),
        sa.Column("original_name", sa.String(length=64), nullable=False),
        sa.Column("original_format", sa.String(length=16), nullable=False),
        sa.Column("original_bytes", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("public_url", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["restaurant_id"], ["Restaurant.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["uploader_id"], ["User.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stored_name"),
    )
    op.create_index("image_image_type_index", "image", ["image_type"])
    op.create_index("image_restaurant_id_index", "image", ["restaurant_id"])
    op.create_index("image_created_at_index", "image", ["created_at"])


def downgrade() -> None:
    """Drop the image tracking table."""
    op.drop_index("image_created_at_index", table_name="image")
    op.drop_index("image_restaurant_id_index", table_name="image")
    op.drop_index("image_image_type_index", table_name="image")
    op.drop_table("image")
