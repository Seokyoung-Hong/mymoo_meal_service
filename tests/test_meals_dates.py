"""Date-based meal API regression tests."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.meals import Meal, MealType
from app.models.restaurants import Restaurant
from app.models.user import User


async def seed_date_filter_meals(db_session: AsyncSession) -> None:
    """Seed meals across multiple dates, types, and restaurants."""
    owner = User(user_id="date-filter-owner")
    other_owner = User(user_id="date-filter-other-owner")
    lunch = MealType(name="lunch")
    breakfast = MealType(name="breakfast")
    dinner = MealType(name="dinner")
    restaurant = Restaurant(
        name="Date Filter Restaurant",
        owner_user=owner,
        is_campus=True,
        establishment_type="cafeteria",
    )
    other_restaurant = Restaurant(
        name="Other Date Restaurant",
        owner_user=other_owner,
        is_campus=True,
        establishment_type="cafeteria",
    )
    db_session.add_all(
        [owner, other_owner, lunch, breakfast, dinner, restaurant, other_restaurant]
    )
    await db_session.flush()

    db_session.add_all(
        [
            Meal(
                restaurant_id=restaurant.id,
                meal_type_id=lunch.id,
                served_date=date(2026, 6, 24),
                main_menu="June 24 Lunch",
                side_menus=["rice"],
                image_url=None,
                menu=["June 24 Lunch", "rice"],
                registered_at=datetime(2026, 6, 25, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 6, 25, 1, tzinfo=timezone.utc),
            ),
            Meal(
                restaurant_id=restaurant.id,
                meal_type_id=breakfast.id,
                served_date=date(2026, 6, 24),
                main_menu="June 24 Breakfast",
                side_menus=["toast"],
                image_url=None,
                menu=["June 24 Breakfast", "toast"],
            ),
            Meal(
                restaurant_id=restaurant.id,
                meal_type_id=lunch.id,
                served_date=date(2026, 6, 25),
                main_menu="June 25 Lunch",
                side_menus=["soup"],
                image_url=None,
                menu=["June 25 Lunch", "soup"],
                registered_at=datetime(2026, 6, 24, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 6, 24, 1, tzinfo=timezone.utc),
            ),
            Meal(
                restaurant_id=other_restaurant.id,
                meal_type_id=lunch.id,
                served_date=date(2026, 6, 24),
                main_menu="Other June 24 Lunch",
                side_menus=["kimchi"],
                image_url=None,
                menu=["Other June 24 Lunch", "kimchi"],
            ),
            Meal(
                restaurant_id=other_restaurant.id,
                meal_type_id=dinner.id,
                served_date=date(2026, 6, 25),
                main_menu="Other June 25 Dinner",
                side_menus=["salad"],
                image_url=None,
                menu=["Other June 25 Dinner", "salad"],
            ),
        ]
    )
    await db_session.commit()


def response_menu_names(response_data: list[dict[str, object]]) -> set[str]:
    return {str(meal["main_menu"]) for meal in response_data}


async def test_list_meals_filters_by_served_date(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await seed_date_filter_meals(db_session)

    response = await async_client.get("/meals", params={"date": "2026-06-24"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert response_menu_names(data) == {
        "June 24 Lunch",
        "June 24 Breakfast",
        "Other June 24 Lunch",
    }
    assert all(meal["served_date"] == "2026-06-24" for meal in data)
    assert all("menu" not in meal for meal in data)


async def test_list_meals_filters_by_date_and_type_alias(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await seed_date_filter_meals(db_session)

    response = await async_client.get(
        "/meals",
        params={"date": "2026-06-24", "type": "lunch"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert response_menu_names(data) == {"June 24 Lunch", "Other June 24 Lunch"}
    assert all(meal["meal_type"] == "lunch" for meal in data)
    assert all(meal["served_date"] == "2026-06-24" for meal in data)


async def test_today_meals_use_seoul_business_date_at_utc_boundary(
    async_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_date_filter_meals(db_session)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            utc_boundary = datetime(2026, 6, 24, 15, 30, tzinfo=timezone.utc)
            if tz is not None:
                return utc_boundary.astimezone(tz)
            return utc_boundary.replace(tzinfo=None)

    monkeypatch.setattr("app.utils.meals.datetime", FixedDateTime)

    response = await async_client.get("/meals/today")

    assert response.status_code == 200
    data = response.json()["data"]
    assert response_menu_names(data) == {"June 25 Lunch", "Other June 25 Dinner"}
    assert all(meal["served_date"] == "2026-06-25" for meal in data)


async def test_restaurant_scoped_meals_filter_by_served_date(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await seed_date_filter_meals(db_session)

    response = await async_client.get(
        "/restaurants/1/meals",
        params={"date": "2026-06-24"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert response_menu_names(data) == {"June 24 Lunch", "June 24 Breakfast"}
    assert all(meal["restaurant_id"] == 1 for meal in data)
    assert all(meal["served_date"] == "2026-06-24" for meal in data)


async def test_latest_meals_remain_latest_not_today(
    async_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_date_filter_meals(db_session)
    monkeypatch.setattr(
        "app.routers.meals.seoul_business_date", lambda: date(2026, 6, 24)
    )

    response = await async_client.get("/meals/latest")

    assert response.status_code == 200
    assert "June 25 Lunch" in response_menu_names(response.json()["data"])


async def test_invalid_exact_date_returns_client_error(
    async_client: AsyncClient,
) -> None:
    response = await async_client.get("/meals", params={"date": "2026-99-99"})

    assert response.status_code == 400
