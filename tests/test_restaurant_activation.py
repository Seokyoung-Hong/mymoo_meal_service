"""Restaurant activation semantics regression tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from jwcrypto import jwk, jwt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.models.audit import AuditLog
from app.models.meals import Meal, MealType
from app.models.restaurants import Restaurant
from app.models.user import User
from app.utils import auth as auth_utils
from app.utils.request_id import REQUEST_ID_HEADER


TEST_ISSUER = "https://issuer.test/realms/mymoo"
TEST_AUDIENCE = "mymoo-meal-service"
TEST_CLIENT_ID = "mymoo-meal-service"


async def seed_activation_context(
    db_session: AsyncSession,
) -> tuple[Restaurant, Restaurant]:
    """Seed one active and one inactive restaurant with matching lunch meals."""
    owner = User(user_id="activation-owner")
    lunch = MealType(name="lunch")
    active = Restaurant(
        name="Active Restaurant",
        owner_user=owner,
        is_campus=True,
        is_active=True,
        establishment_type="fixed_menu_restaurant",
    )
    inactive = Restaurant(
        name="Inactive Restaurant",
        owner_user=owner,
        is_campus=True,
        is_active=False,
        establishment_type="fixed_menu_restaurant",
    )
    db_session.add_all([owner, lunch, active, inactive])
    await db_session.flush()

    db_session.add_all(
        [
            Meal(
                restaurant_id=active.id,
                meal_type_id=lunch.id,
                served_date=date(2026, 6, 24),
                main_menu="Active Lunch",
                side_menus=["rice"],
                image_url=None,
                menu=["Active Lunch", "rice"],
            ),
            Meal(
                restaurant_id=inactive.id,
                meal_type_id=lunch.id,
                served_date=date(2026, 6, 24),
                main_menu="Inactive Lunch",
                side_menus=["soup"],
                image_url=None,
                menu=["Inactive Lunch", "soup"],
            ),
        ]
    )
    await db_session.commit()
    return active, inactive


def response_names(response_data: list[dict[str, object]]) -> set[str]:
    return {str(item["name"]) for item in response_data}


def install_activation_jwks(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[jwk.JWK, Callable[[str], str]]:
    """Install deterministic JWKS auth settings and return an admin JWT factory."""
    rsa_key = jwk.JWK.generate(kty="RSA", size=2048, kid="activation-key-id")
    key_set = jwk.JWKSet()
    key_set.add(jwk.JWK.from_json(rsa_key.export(private_key=False)))

    async def fake_get_jwks(force_refresh: bool = False) -> jwk.JWKSet:
        _ = force_refresh
        return key_set

    monkeypatch.setattr(Config, "JWT_ISSUER", TEST_ISSUER)
    monkeypatch.setattr(Config, "JWT_AUDIENCE", TEST_AUDIENCE)
    monkeypatch.setattr(Config, "JWT_CLIENT_ID", TEST_CLIENT_ID)
    monkeypatch.setattr(Config, "JWT_ALLOWED_ALGORITHMS", ["RS256"])
    monkeypatch.setattr(auth_utils, "get_jwks", fake_get_jwks)
    auth_utils.clear_jwks_cache()

    def make_admin_token(subject: str) -> str:
        now = datetime.now(timezone.utc)
        token = jwt.JWT(
            header={"alg": "RS256", "kid": "activation-key-id", "typ": "JWT"},
            claims={
                "iss": TEST_ISSUER,
                "aud": TEST_AUDIENCE,
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=5)).timestamp()),
                "sub": subject,
                "realm_access": {"roles": []},
                "resource_access": {TEST_CLIENT_ID: {"roles": ["meal_admin"]}},
            },
        )
        token.make_signed_token(rsa_key)
        return token.serialize()

    return rsa_key, make_admin_token


async def test_public_restaurant_list_hides_inactive_by_default(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await seed_activation_context(db_session)

    response = await async_client.get("/restaurants/")

    assert response.status_code == 200
    data = response.json()["data"]
    assert response_names(data) == {"Active Restaurant"}
    assert data[0]["is_active"] is True


async def test_admin_include_inactive_list_includes_inactive_restaurants(
    async_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_activation_context(db_session)
    _, make_admin_token = install_activation_jwks(monkeypatch)
    admin_user = User(user_id="activation-admin")
    db_session.add(admin_user)
    await db_session.commit()
    token = make_admin_token(admin_user.user_id)

    response = await async_client.get(
        "/restaurants/",
        params={"include_inactive": "true"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert response_names(data) == {"Active Restaurant", "Inactive Restaurant"}
    inactive = next(item for item in data if item["name"] == "Inactive Restaurant")
    assert inactive["is_active"] is False


async def test_include_inactive_requires_admin_authorization(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await seed_activation_context(db_session)

    response = await async_client.get(
        "/restaurants/",
        params={"include_inactive": "true"},
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "data" not in response.json()


async def test_public_detail_for_inactive_restaurant_returns_not_found(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    _, inactive = await seed_activation_context(db_session)

    response = await async_client.get(f"/restaurants/{inactive.id}")

    assert response.status_code == 404


async def test_public_meal_lists_hide_inactive_restaurant_meals(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    _, inactive = await seed_activation_context(db_session)

    response = await async_client.get("/meals", params={"date": "2026-06-24"})
    scoped_response = await async_client.get(
        f"/restaurants/{inactive.id}/meals",
        params={"date": "2026-06-24"},
    )

    assert response.status_code == 200
    assert {meal["main_menu"] for meal in response.json()["data"]} == {"Active Lunch"}
    assert scoped_response.status_code == 200
    assert scoped_response.json()["data"] == []


async def test_public_meal_detail_hides_inactive_restaurant_meal(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    active, inactive = await seed_activation_context(db_session)
    active_meal = await db_session.scalar(
        select(Meal).where(Meal.restaurant_id == active.id)
    )
    inactive_meal = await db_session.scalar(
        select(Meal).where(Meal.restaurant_id == inactive.id)
    )

    assert active_meal is not None
    assert inactive_meal is not None

    active_response = await async_client.get(f"/meals/{active_meal.id}")
    inactive_response = await async_client.get(f"/meals/{inactive_meal.id}")

    assert active_response.status_code == 200
    assert active_response.json()["data"]["main_menu"] == "Active Lunch"
    assert inactive_response.status_code == 404


async def test_creating_meal_for_inactive_restaurant_fails_without_insert(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    _, inactive = await seed_activation_context(db_session)
    before_count = await db_session.scalar(select(func.count()).select_from(Meal))

    response = await async_client.post(
        f"/meals/{inactive.id}",
        json={
            "served_date": "2026-06-25",
            "main_menu": "Blocked Lunch",
            "side_menus": ["kimchi"],
            "image_url": None,
            "meal_type": "lunch",
        },
    )

    after_count = await db_session.scalar(select(func.count()).select_from(Meal))
    assert response.status_code == 409
    assert after_count == before_count


async def test_status_change_creates_audit_log_with_request_id(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    active, _ = await seed_activation_context(db_session)
    request_id = "activation-status-request-id"

    response = await async_client.patch(
        f"/restaurants/{active.id}/status",
        json={"is_active": False},
        headers={REQUEST_ID_HEADER: request_id},
    )

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == request_id
    assert response.json()["data"]["is_active"] is False

    saved = await db_session.scalar(
        select(AuditLog).where(AuditLog.request_id == request_id)
    )
    assert saved is not None
    assert saved.actor_user_id == "test-user-sub"
    assert saved.action == "restaurant.status.update"
    assert saved.resource_type == "restaurant"
    assert saved.resource_id == str(active.id)
    assert saved.before == {"is_active": True}
    assert saved.after == {"is_active": False}
