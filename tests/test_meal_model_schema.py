"""Regression tests for the Maemoo meal model and schema contract."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.meals import Meal, MealType
from app.models.restaurants import Restaurant
from app.models.user import User
from app.schemas.meals import MealRegister, MealResponse, MealType as MealTypeSchema


def test_meal_register_accepts_served_date_empty_sides_and_null_image() -> None:
    meal = MealRegister(
        served_date=date(2026, 6, 24),
        main_menu="김치찌개",
        side_menus=[],
        image_url=None,
        meal_type=MealTypeSchema.lunch,
    )

    assert meal.served_date == date(2026, 6, 24)
    assert meal.main_menu == "김치찌개"
    assert meal.side_menus == []
    assert meal.image_url is None


def test_meal_register_rejects_invalid_image_url() -> None:
    with pytest.raises(ValidationError):
        MealRegister(
            served_date=date(2026, 6, 24),
            main_menu="김치찌개",
            side_menus=[],
            image_url="not-a-url",
            meal_type=MealTypeSchema.lunch,
        )


def test_meal_register_rejects_missing_served_date() -> None:
    with pytest.raises(ValidationError):
        MealRegister.model_validate(
            {
                "main_menu": "김치찌개",
                "side_menus": [],
                "image_url": None,
                "meal_type": "lunch",
            }
        )


async def test_orm_persists_and_reads_new_meal_columns(
    db_session: AsyncSession,
) -> None:
    owner = User(user_id="meal-model-owner")
    restaurant = Restaurant(
        name="Meal Model Restaurant",
        owner_user=owner,
        is_campus=True,
        establishment_type="cafeteria",
    )
    meal_type = MealType(name="meal-model-lunch")
    db_session.add_all([owner, restaurant, meal_type])
    await db_session.commit()

    meal = Meal(
        restaurant_id=restaurant.id,
        meal_type_id=meal_type.id,
        served_date=date(2026, 6, 24),
        main_menu="김치찌개",
        side_menus=["쌀밥", "깍두기"],
        image_url="https://example.com/meal.jpg",
        menu=["김치찌개", "쌀밥", "깍두기"],
        registered_at=datetime(2026, 6, 24, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 24, tzinfo=timezone.utc),
    )
    db_session.add(meal)
    await db_session.commit()

    result = await db_session.execute(select(Meal).where(Meal.id == meal.id))
    saved_meal = result.scalar_one()

    assert saved_meal.served_date == date(2026, 6, 24)
    assert saved_meal.main_menu == "김치찌개"
    assert saved_meal.side_menus == ["쌀밥", "깍두기"]
    assert saved_meal.image_url == "https://example.com/meal.jpg"


def test_meal_response_includes_new_fields_without_legacy_menu() -> None:
    response = MealResponse(
        id=1,
        served_date=date(2026, 6, 24),
        main_menu="김치찌개",
        side_menus=[],
        image_url=None,
        meal_type=MealTypeSchema.lunch,
        restaurant_id=10,
        restaurant_name="Meal Response Restaurant",
        registered_at=datetime(2026, 6, 24, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 24, tzinfo=timezone.utc),
    )

    dumped = response.model_dump()

    assert dumped["served_date"] == date(2026, 6, 24)
    assert dumped["main_menu"] == "김치찌개"
    assert dumped["side_menus"] == []
    assert dumped["image_url"] is None
    assert "menu" not in dumped
