"""Smoke tests for the reusable async test harness."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.meals import MealType
from app.models.user import User
from app.utils import db as db_utils


async def test_async_client_reaches_health_endpoint(
    async_client: httpx.AsyncClient,
) -> None:
    """The ASGITransport async client can call local app routes."""
    response = await async_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_db_session_can_insert_and_query_model(db_session: AsyncSession) -> None:
    """The isolated database fixture can persist and query mapped models."""
    meal_type = MealType(name="test-breakfast")
    db_session.add(meal_type)
    await db_session.commit()

    result = await db_session.execute(
        select(MealType).where(MealType.name == "test-breakfast")
    )

    assert result.scalar_one().name == "test-breakfast"


async def test_dependency_override_prevents_production_db_usage(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Router dependencies use the test session instead of AsyncSessionLocal."""

    class FailingSessionLocal:
        def __call__(self) -> None:
            msg = "production AsyncSessionLocal should not be used in tests"
            raise AssertionError(msg)

    monkeypatch.setattr(db_utils, "AsyncSessionLocal", FailingSessionLocal())

    db_session.add(User(user_id="override-user"))
    await db_session.commit()

    response = await async_client.get("/users/")

    assert response.status_code == 200
    assert [user["user_id"] for user in response.json()] == ["override-user"]


async def test_jwt_and_jwks_helpers_are_deterministic_without_network(
    fake_jwks: dict[str, list[dict[str, str]]],
    fake_jwt_payload: dict[str, Any],
    make_test_jwt: Callable[[dict[str, Any] | None], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth helpers return local deterministic data and never require Keycloak."""

    async def fail_network(*_: object, **__: object) -> None:
        msg = "network access is forbidden in auth helper tests"
        raise AssertionError(msg)

    monkeypatch.setattr(httpx.AsyncClient, "get", fail_network)

    token = make_test_jwt(None)
    same_token = make_test_jwt(None)

    assert fake_jwks["keys"][0]["kid"] == "test-key-id"
    assert fake_jwt_payload["sub"] == "test-user-sub"
    assert token == same_token
    assert token.count(".") == 2
