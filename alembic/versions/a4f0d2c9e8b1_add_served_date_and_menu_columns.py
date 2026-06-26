"""add served date and menu columns

Revision ID: a4f0d2c9e8b1
Revises: c2d4a8f74f36
Create Date: 2026-06-24 00:00:00.000000

"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Sequence, Union
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa

from app.models.meals import NonEscapedJSON


# revision identifiers, used by Alembic.
revision: str = "a4f0d2c9e8b1"
down_revision: Union[str, None] = "c2d4a8f74f36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SEOUL_TZ = ZoneInfo("Asia/Seoul")


def _served_date_from_timestamp(timestamp: datetime | str) -> str:
    """Return the Asia/Seoul business date for a legacy meal timestamp."""
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if timestamp.tzinfo is None or timestamp.tzinfo.utcoffset(timestamp) is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(SEOUL_TZ).date().isoformat()


def _coerce_menu(value: object) -> list[str]:
    """Read legacy menu values across JSON/text encodings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        return _coerce_menu(decoded)
    return []


def _json_param(value: list[str], dialect_name: str) -> object:
    if dialect_name == "postgresql":
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


def _backfill_meal_columns(connection: sa.Connection) -> None:
    dialect_name = connection.dialect.name
    rows = connection.execute(
        sa.text('SELECT id, menu, registered_at FROM "meal"')
    ).mappings()

    for row in rows:
        menu = _coerce_menu(row["menu"])
        main_menu = menu[0] if menu else ""
        side_menus = menu[1:]
        connection.execute(
            sa.text(
                'UPDATE "meal" '
                "SET served_date = :served_date, "
                "main_menu = :main_menu, "
                "side_menus = :side_menus "
                "WHERE id = :meal_id"
            ),
            {
                "served_date": _served_date_from_timestamp(row["registered_at"]),
                "main_menu": main_menu,
                "side_menus": _json_param(side_menus, dialect_name),
                "meal_id": row["id"],
            },
        )


def _assert_no_future_unique_duplicates(connection: sa.Connection) -> None:
    """Fail before adding uniqueness if existing meals would violate it."""
    duplicates = (
        connection.execute(
            sa.text(
                "SELECT restaurant_id, meal_type_id, served_date, COUNT(*) AS count "
                'FROM "meal" '
                "GROUP BY restaurant_id, meal_type_id, served_date "
                "HAVING COUNT(*) > 1"
            )
        )
        .mappings()
        .all()
    )
    if duplicates:
        details = [
            {
                "restaurant_id": row["restaurant_id"],
                "meal_type_id": row["meal_type_id"],
                "served_date": str(row["served_date"]),
                "count": row["count"],
            }
            for row in duplicates
        ]
        raise RuntimeError(
            "Duplicate meals block meal served-date uniqueness: "
            f"{details}. Run app.utils.migration_safety.find_duplicate_meal_groups "
            "and resolve duplicates before retrying."
        )


def upgrade() -> None:
    """Add Mymoo served-date/menu columns and guarded uniqueness."""
    op.add_column("meal", sa.Column("served_date", sa.Date(), nullable=True))
    op.add_column("meal", sa.Column("main_menu", sa.Text(), nullable=True))
    op.add_column("meal", sa.Column("side_menus", NonEscapedJSON(), nullable=True))
    op.add_column("meal", sa.Column("image_url", sa.Text(), nullable=True))

    connection = op.get_bind()
    _backfill_meal_columns(connection)
    _assert_no_future_unique_duplicates(connection)

    with op.batch_alter_table("meal") as batch_op:
        batch_op.alter_column("served_date", existing_type=sa.Date(), nullable=False)
        batch_op.alter_column("main_menu", existing_type=sa.Text(), nullable=False)
        batch_op.alter_column(
            "side_menus",
            existing_type=NonEscapedJSON(),
            nullable=False,
        )

    op.create_index("meal_served_date_index", "meal", ["served_date"])
    op.create_index(
        "meal_restaurant_served_date_index",
        "meal",
        ["restaurant_id", "served_date"],
    )
    op.create_index(
        "meal_restaurant_type_served_date_index",
        "meal",
        ["restaurant_id", "meal_type_id", "served_date"],
    )
    op.create_index(
        "meal_restaurant_type_served_date_unique",
        "meal",
        ["restaurant_id", "meal_type_id", "served_date"],
        unique=True,
    )


def downgrade() -> None:
    """Remove Mymoo served-date/menu columns and indexes."""
    op.drop_index("meal_restaurant_type_served_date_unique", table_name="meal")
    op.drop_index("meal_restaurant_type_served_date_index", table_name="meal")
    op.drop_index("meal_restaurant_served_date_index", table_name="meal")
    op.drop_index("meal_served_date_index", table_name="meal")
    op.drop_column("meal", "image_url")
    op.drop_column("meal", "side_menus")
    op.drop_column("meal", "main_menu")
    op.drop_column("meal", "served_date")
