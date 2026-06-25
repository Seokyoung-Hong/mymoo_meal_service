"""add audit log

Revision ID: d8b8f1f2c3a4
Revises: a4f0d2c9e8b1
Create Date: 2026-06-24 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.models.meals import NonEscapedJSON


# revision identifiers, used by Alembic.
revision: str = "d8b8f1f2c3a4"
down_revision: Union[str, None] = "a4f0d2c9e8b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create transactional audit log storage."""
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("actor_user_id", sa.String(length=255), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("resource_type", sa.String(length=128), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=True),
        sa.Column("before", NonEscapedJSON(), nullable=True),
        sa.Column("after", NonEscapedJSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("audit_log_request_id_index", "audit_log", ["request_id"])
    op.create_index("audit_log_actor_user_id_index", "audit_log", ["actor_user_id"])
    op.create_index(
        "audit_log_resource_index",
        "audit_log",
        ["resource_type", "resource_id"],
    )
    op.create_index("audit_log_created_at_index", "audit_log", ["created_at"])


def downgrade() -> None:
    """Drop transactional audit log storage."""
    op.drop_index("audit_log_created_at_index", table_name="audit_log")
    op.drop_index("audit_log_resource_index", table_name="audit_log")
    op.drop_index("audit_log_actor_user_id_index", table_name="audit_log")
    op.drop_index("audit_log_request_id_index", table_name="audit_log")
    op.drop_table("audit_log")
