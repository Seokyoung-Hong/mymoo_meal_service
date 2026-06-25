"""add restaurant price

Revision ID: f5f2d67168b1
Revises: 9c9a6d6a5f1d
Create Date: 2026-05-29 22:20:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f5f2d67168b1"
down_revision: Union[str, None] = "9c9a6d6a5f1d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """식당과 식당 제출 테이블에 1인분 가격 컬럼을 추가합니다."""
    op.add_column("Restaurant", sa.Column("price", sa.Integer(), nullable=True))
    op.add_column(
        "Restaurant_submission", sa.Column("price", sa.Integer(), nullable=True)
    )
    op.create_check_constraint(
        "restaurant_price_positive_check",
        "Restaurant",
        "price IS NULL OR price > 0",
    )
    op.create_check_constraint(
        "restaurant_submission_price_positive_check",
        "Restaurant_submission",
        "price IS NULL OR price > 0",
    )


def downgrade() -> None:
    """식당과 식당 제출 테이블에서 1인분 가격 컬럼을 제거합니다."""
    op.drop_constraint(
        "restaurant_submission_price_positive_check",
        "Restaurant_submission",
        type_="check",
    )
    op.drop_constraint(
        "restaurant_price_positive_check",
        "Restaurant",
        type_="check",
    )
    op.drop_column("Restaurant_submission", "price")
    op.drop_column("Restaurant", "price")
