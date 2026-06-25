"""update restaurant establishment types

Revision ID: 9c9a6d6a5f1d
Revises: 3297eb691f3b
Create Date: 2026-05-29 21:30:00.000000

"""

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9c9a6d6a5f1d"
down_revision: Union[str, None] = "3297eb691f3b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_VALUES = {"student", "vendor", "external"}
NEW_VALUES = {
    "student",
    "vendor",
    "fixed_korean_buffet",
    "variable_korean_buffet",
}
ALLOWED_VALUES = OLD_VALUES | NEW_VALUES


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
    """기존 external 식당 유형을 fixed_korean_buffet으로 이행합니다."""
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
            SET establishment_type = 'fixed_korean_buffet'
            WHERE establishment_type = 'external'
            """
        )
        op.execute(
            """
            UPDATE "Restaurant_submission"
            SET establishment_type = 'fixed_korean_buffet'
            WHERE establishment_type = 'external'
            """
        )
        return

    connection = op.get_bind()

    _assert_known_values(connection, "Restaurant")
    _assert_known_values(connection, "Restaurant_submission")

    _ = connection.execute(
        restaurant_update,
        {"old_value": "external", "new_value": "fixed_korean_buffet"},
    )
    _ = connection.execute(
        submission_update,
        {"old_value": "external", "new_value": "fixed_korean_buffet"},
    )


def downgrade() -> None:
    """데이터 손실 없이 되돌릴 수 없어 downgrade를 막습니다."""
    raise RuntimeError(
        "This migration is intentionally irreversible because newly created "
        "fixed_korean_buffet rows cannot be distinguished from migrated external rows."
    )
