"""add user fields

Revision ID: 133210f0f0b5
Revises: 7b5fd919d7c8
Create Date: 2026-01-18 22:40:24.449620

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "133210f0f0b5"
down_revision: Union[str, None] = "7b5fd919d7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
