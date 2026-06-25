"""Tests for meal migration safety duplicate detection."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.meals import Meal, MealType
from app.models.restaurants import Restaurant
from app.models.user import User
from app.utils.migration_safety import (
    DuplicateMealGroup,
    MealDuplicateCandidate,
    find_duplicate_meal_candidates,
    find_duplicate_meal_groups,
    served_date_from_timestamp,
)


async def _create_meal_context(db_session: AsyncSession) -> tuple[int, int, int, int]:
    owner = User(user_id="migration-safety-owner")
    first_restaurant = Restaurant(
        name="Migration Safety A",
        owner_user=owner,
        is_campus=True,
        establishment_type="cafeteria",
    )
    second_restaurant = Restaurant(
        name="Migration Safety B",
        owner_user=owner,
        is_campus=True,
        establishment_type="cafeteria",
    )
    breakfast = MealType(name="migration-breakfast")
    lunch = MealType(name="migration-lunch")

    db_session.add_all([owner, first_restaurant, second_restaurant, breakfast, lunch])
    await db_session.commit()

    return first_restaurant.id, second_restaurant.id, breakfast.id, lunch.id


async def _add_meal(
    db_session: AsyncSession,
    *,
    restaurant_id: int,
    meal_type_id: int,
    registered_at: datetime,
) -> None:
    db_session.add(
        Meal(
            restaurant_id=restaurant_id,
            meal_type_id=meal_type_id,
            served_date=registered_at.date(),
            main_menu="rice",
            side_menus=[],
            menu=["rice"],
            registered_at=registered_at,
            updated_at=registered_at,
        )
    )


async def test_db_detector_reports_duplicate_by_seoul_served_date(
    db_session: AsyncSession,
) -> None:
    first_restaurant_id, _, breakfast_id, _ = await _create_meal_context(db_session)
    timestamp = datetime(2026, 6, 24, 1, tzinfo=timezone.utc)

    await _add_meal(
        db_session,
        restaurant_id=first_restaurant_id,
        meal_type_id=breakfast_id,
        registered_at=timestamp,
    )
    await _add_meal(
        db_session,
        restaurant_id=first_restaurant_id,
        meal_type_id=breakfast_id,
        registered_at=timestamp.replace(hour=2),
    )
    await db_session.commit()

    duplicates = await find_duplicate_meal_groups(db_session)

    assert duplicates == [
        DuplicateMealGroup(
            restaurant_id=first_restaurant_id,
            meal_type_id=breakfast_id,
            served_date=date(2026, 6, 24),
            count=2,
        )
    ]


def test_meals_with_different_key_parts_are_not_duplicate_groups() -> None:
    timestamp = datetime(2026, 6, 24, 1, tzinfo=timezone.utc)
    candidates = [
        MealDuplicateCandidate(restaurant_id=1, meal_type_id=1, timestamp=timestamp),
        MealDuplicateCandidate(restaurant_id=2, meal_type_id=1, timestamp=timestamp),
        MealDuplicateCandidate(restaurant_id=1, meal_type_id=2, timestamp=timestamp),
        MealDuplicateCandidate(
            restaurant_id=1,
            meal_type_id=1,
            timestamp=datetime(2026, 6, 24, 16, tzinfo=timezone.utc),
        ),
    ]

    assert find_duplicate_meal_candidates(candidates) == []


def test_served_date_backfill_rule_uses_asia_seoul_utc_boundary() -> None:
    """served_date = timestamp.astimezone(Asia/Seoul).date()."""
    timestamp = datetime(2026, 6, 23, 15, 30, tzinfo=timezone.utc)

    assert served_date_from_timestamp(timestamp) == date(2026, 6, 24)


def test_empty_and_non_duplicate_datasets_return_no_duplicate_groups() -> None:
    single_meal = MealDuplicateCandidate(
        restaurant_id=1,
        meal_type_id=1,
        timestamp=datetime(2026, 6, 24, 1, tzinfo=timezone.utc),
    )

    assert find_duplicate_meal_candidates([]) == []
    assert find_duplicate_meal_candidates([single_meal]) == []


def test_naive_timestamps_are_interpreted_as_utc_before_seoul_date() -> None:
    naive_timestamp = datetime(2026, 6, 23, 15, 30)

    assert served_date_from_timestamp(naive_timestamp) == date(2026, 6, 24)
