"""rename vendor to fixed_menu_restaurant

Revision ID: c2d4a8f74f36
Revises: f5f2d67168b1
Create Date: 2026-05-29 23:10:00.000000

"""

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c2d4a8f74f36"
down_revision: Union[str, None] = "f5f2d67168b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PRE_RENAME_VALUES = {
    "student",
    "vendor",
    "fixed_korean_buffet",
    "variable_korean_buffet",
}
POST_RENAME_VALUES = {
    "student",
    "fixed_menu_restaurant",
    "fixed_korean_buffet",
    "variable_korean_buffet",
}
ALLOWED_VALUES = PRE_RENAME_VALUES | POST_RENAME_VALUES


def _assert_known_values(connection: sa.Connection, table_name: str) -> None:
    if table_name == "Restaurant":
        query = sa.text('SELECT DISTINCT establishment_type FROM "Restaurant"')
    elif table_name == "Restaurant_submission":
        query = sa.text(
            'SELECT DISTINCT establishment_type FROM "Restaurant_submission"'
        )
    else:
        raise ValueError(f"Unsupported table name: {table_name}")

    rows = connection.execute(query)
    existing_values = {row[0] for row in rows if row[0] is not None}
    unknown_values = existing_values - ALLOWED_VALUES
    if unknown_values:
        raise RuntimeError(
            f"Unexpected establishment_type values found in {table_name}: "
            f"{sorted(unknown_values)}"
        )


def upgrade() -> None:
    """Restaurant 계열 테이블의 vendor 값을 fixed_menu_restaurant로 바꿉니다."""
    restaurant_update = sa.text(
        'UPDATE "Restaurant" '
        "SET establishment_type = :new_value "
        "WHERE establishment_type = :old_value"
    )
    submission_update = sa.text(
        'UPDATE "Restaurant_submission" '
        "SET establishment_type = :new_value "
        "WHERE establishment_type = :old_value"
    )

    if context.is_offline_mode():
        op.execute(
            """
            UPDATE "Restaurant"
            SET establishment_type = 'fixed_menu_restaurant'
            WHERE establishment_type = 'vendor'
            """
        )
        op.execute(
            """
            UPDATE "Restaurant_submission"
            SET establishment_type = 'fixed_menu_restaurant'
            WHERE establishment_type = 'vendor'
            """
        )
        return

    connection = op.get_bind()
    _assert_known_values(connection, "Restaurant")
    _assert_known_values(connection, "Restaurant_submission")

    _ = connection.execute(
        restaurant_update,
        {"old_value": "vendor", "new_value": "fixed_menu_restaurant"},
    )
    _ = connection.execute(
        submission_update,
        {"old_value": "vendor", "new_value": "fixed_menu_restaurant"},
    )


def downgrade() -> None:
    """값 rename은 신규 데이터와 구분할 수 없어 downgrade를 막습니다."""
    raise RuntimeError(
        "This migration is intentionally irreversible because newly created "
        "fixed_menu_restaurant rows cannot be distinguished from migrated vendor rows."
    )
