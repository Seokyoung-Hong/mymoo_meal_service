"""empty message

Revision ID: 7b5fd919d7c8
Revises: 26347cb2feb5
Create Date: 2026-01-18 22:15:55.347560

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7b5fd919d7c8"
down_revision: Union[str, None] = "26347cb2feb5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
