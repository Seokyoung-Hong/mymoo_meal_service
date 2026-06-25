"""Flexible pricing policy tests."""

from __future__ import annotations

from collections.abc import Mapping

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.meals import MealType
from app.models.restaurants import Restaurant
from app.models.user import User
from app.utils.request_id import REQUEST_ID_HEADER


async def seed_pricing_context(
    db_session: AsyncSession,
    *,
    restaurant_price: int | None = None,
) -> Restaurant:
    """Seed a restaurant owned by the default authenticated test user."""
    owner = User(id=1, user_id="test-user-sub")
    restaurant = Restaurant(
        name="Pricing Restaurant",
        owner_user=owner,
        is_campus=True,
        is_active=True,
        establishment_type="fixed_menu_restaurant",
        price=restaurant_price,
    )
    db_session.add_all(
        [
            owner,
            MealType(name="breakfast"),
            MealType(name="lunch"),
            MealType(name="dinner"),
            restaurant,
        ]
    )
    await db_session.commit()
    return restaurant


async def create_policy(
    async_client: AsyncClient,
    restaurant_id: int,
    payload: Mapping[str, object],
    request_id: str | None = None,
) -> dict[str, object]:
    """Create a pricing policy and return the response data."""
    headers = {REQUEST_ID_HEADER: request_id} if request_id else None
    response = await async_client.post(
        f"/restaurants/{restaurant_id}/pricing",
        json=payload,
        headers=headers,
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert isinstance(data, dict)
    return data


async def test_price_resolution_precedence_date_then_meal_then_restaurant(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    restaurant = await seed_pricing_context(db_session)
    _ = await create_policy(
        async_client,
        restaurant.id,
        {"policy_type": "restaurant_fixed", "price": 5000},
    )
    _ = await create_policy(
        async_client,
        restaurant.id,
        {"policy_type": "meal_type_fixed", "price": 6000, "meal_type": "lunch"},
    )
    date_policy = await create_policy(
        async_client,
        restaurant.id,
        {
            "policy_type": "date_specific",
            "price": 7000,
            "meal_type": "lunch",
            "served_date": "2026-06-24",
        },
    )

    date_response = await async_client.get(
        f"/restaurants/{restaurant.id}/price",
        params={"meal_type": "lunch", "served_date": "2026-06-24"},
    )
    meal_response = await async_client.get(
        f"/restaurants/{restaurant.id}/price",
        params={"meal_type": "lunch", "served_date": "2026-06-25"},
    )
    restaurant_response = await async_client.get(
        f"/restaurants/{restaurant.id}/price",
        params={"meal_type": "breakfast", "served_date": "2026-06-25"},
    )

    assert date_response.status_code == 200
    assert meal_response.status_code == 200
    assert restaurant_response.status_code == 200
    date_data = date_response.json()["data"]
    assert date_data["price"] == 7000
    assert date_data["policy_type"] == "date_specific"
    assert date_data["pricing_policy_id"] == date_policy["id"]
    assert date_data["meal_type"] == "lunch"
    assert meal_response.json()["data"]["price"] == 6000
    assert meal_response.json()["data"]["policy_type"] == "meal_type_fixed"
    assert restaurant_response.json()["data"]["price"] == 5000
    assert restaurant_response.json()["data"]["policy_type"] == "restaurant_fixed"


async def test_price_resolution_returns_null_without_match(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    restaurant = await seed_pricing_context(db_session)

    response = await async_client.get(
        f"/restaurants/{restaurant.id}/price",
        params={"meal_type": "dinner", "served_date": "2026-06-24"},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "restaurant_id": restaurant.id,
        "price": None,
        "policy_type": None,
        "pricing_policy_id": None,
        "meal_type": None,
        "served_date": None,
        "source": None,
    }


async def test_legacy_restaurant_price_is_restaurant_fixed_fallback(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    restaurant = await seed_pricing_context(db_session, restaurant_price=4500)

    response = await async_client.get(f"/restaurants/{restaurant.id}/price")

    assert response.status_code == 200
    assert response.json()["data"]["price"] == 4500
    assert response.json()["data"]["policy_type"] == "restaurant_fixed"
    assert response.json()["data"]["source"] == "legacy_restaurant_price"


async def test_duplicate_active_exact_scope_is_rejected_with_409(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    restaurant = await seed_pricing_context(db_session)
    payload = {"policy_type": "restaurant_fixed", "price": 5000}
    _ = await create_policy(async_client, restaurant.id, payload)

    response = await async_client.post(
        f"/restaurants/{restaurant.id}/pricing",
        json=payload,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "활성 가격 정책이 같은 범위에 이미 존재합니다."


async def test_price_mutation_creates_audit_log_with_request_id(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    restaurant = await seed_pricing_context(db_session)
    request_id = "pricing-create-request-id"

    data = await create_policy(
        async_client,
        restaurant.id,
        {"policy_type": "restaurant_fixed", "price": 5100},
        request_id=request_id,
    )

    saved = await db_session.scalar(
        select(AuditLog).where(AuditLog.request_id == request_id)
    )
    assert saved is not None
    assert saved.actor_user_id == "test-user-sub"
    assert saved.action == "pricing_policy.create"
    assert saved.resource_type == "pricing_policy"
    assert saved.resource_id == str(data["id"])
    assert saved.before is None
    assert saved.after is not None
    assert saved.after["price"] == 5100
    assert saved.after["policy_type"] == "restaurant_fixed"
