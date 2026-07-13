"""Reusable async pytest fixtures for the meal service test harness."""

from __future__ import annotations

import base64
import json
import os
import sys
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("KC_SERVER_URL", "http://keycloak.test/")
os.environ.setdefault("KC_REALM", "test-realm")
os.environ.setdefault("KC_CLIENT_ID", "test-client")
os.environ.setdefault("SERVICE_ACCOUNT_SUB", "service-account-test")
os.environ.setdefault("SERVICE_ACCOUNT_TOKEN", "test-token")

from app.database import Base
from app.models import MealType, User  # noqa: E402, F401
from app.routers import (
    admin_restaurants_router,
    meals_router,
    pricing_router,
    restaurants_router,
    users_router,
    worker_router,
)  # noqa: E402
from app.utils import db as db_utils  # noqa: E402
from app.utils import http as http_utils  # noqa: E402
from app.utils.auth import optional_metrics_x_user_id  # noqa: E402
from app.utils.request_id import add_request_id_middleware  # noqa: E402


@pytest.fixture
def fake_user_id() -> str:
    """Return a stable fake Keycloak subject for tests."""
    return "test-user-sub"


@pytest.fixture
def fake_authenticated_user(fake_user_id: str) -> User:
    """Return a local user object without contacting Keycloak."""
    return User(id=1, user_id=fake_user_id)


@pytest.fixture
def fake_jwks() -> dict[str, list[dict[str, str]]]:
    """Return deterministic JWKS-like data for auth tests."""
    return {
        "keys": [
            {
                "kid": "test-key-id",
                "kty": "RSA",
                "alg": "RS256",
                "use": "sig",
                "n": "test-modulus",
                "e": "AQAB",
            }
        ]
    }


@pytest.fixture
def fake_jwt_payload(fake_user_id: str) -> dict[str, Any]:
    """Return a deterministic JWT payload for tests."""
    return {
        "sub": fake_user_id,
        "preferred_username": "test-user",
        "email": "test-user@example.invalid",
        "realm_access": {"roles": ["meal_admin"]},
        "resource_access": {"test-client": {"roles": ["meal_admin"]}},
    }


@pytest.fixture
def make_test_jwt(
    fake_jwt_payload: dict[str, Any],
) -> Callable[[dict[str, Any] | None], str]:
    """Create deterministic unsigned JWT-shaped strings for local tests."""

    def encode_part(value: dict[str, Any]) -> str:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def make_token(overrides: dict[str, Any] | None = None) -> str:
        payload = {**fake_jwt_payload, **(overrides or {})}
        return ".".join(
            [
                encode_part({"alg": "none", "kid": "test-key-id", "typ": "JWT"}),
                encode_part(payload),
                "test-signature",
            ]
        )

    return make_token


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Create an isolated SQLite database session for each test."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with engine.connect() as connection:
        transaction = await connection.begin()
        session_factory = async_sessionmaker(
            bind=connection,
            class_=AsyncSession,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        session = session_factory()
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def test_app(
    db_session: AsyncSession,
    fake_authenticated_user: User,
) -> FastAPI:
    """Build a FastAPI app with routers but without production lifespan work."""
    app = FastAPI(root_path="/meal")
    add_request_id_middleware(app)

    app.include_router(meals_router)
    app.include_router(pricing_router)
    app.include_router(restaurants_router)
    app.include_router(admin_restaurants_router)
    app.include_router(users_router)
    app.include_router(worker_router)

    @app.get("/", dependencies=[Depends(optional_metrics_x_user_id)])
    async def root() -> dict[str, str]:
        return {"test": "Hello Mymoo"}

    @app.get("/health", dependencies=[Depends(optional_metrics_x_user_id)])
    async def health_check() -> dict[str, str]:
        return {"status": "ok"}

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def override_current_user() -> User:
        return fake_authenticated_user

    async def override_async_client() -> AsyncGenerator[httpx.AsyncClient, None]:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"mocked": True, "url": str(request.url)},
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            yield client

    app.dependency_overrides[db_utils.get_db] = override_get_db
    app.dependency_overrides[db_utils.get_current_user] = override_current_user
    app.dependency_overrides[db_utils.get_admin_user] = override_current_user
    app.dependency_overrides[http_utils.get_async_client] = override_async_client

    return app


@pytest_asyncio.fixture
async def async_client(test_app: FastAPI) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Create an HTTPX async client using explicit ASGITransport."""
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        yield client
