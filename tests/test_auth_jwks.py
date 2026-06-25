"""Deterministic JWT/JWKS authentication tests."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi import Depends, FastAPI
from jwcrypto import jwk, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.models.user import User
from app.schemas.users import AdminUserSchema
from app.utils import auth as auth_utils
from app.utils import db as db_utils


TEST_ISSUER = "https://issuer.test/realms/mymoo"
TEST_AUDIENCE = "mymoo-meal-service"
TEST_CLIENT_ID = "mymoo-meal-service"
CURRENT_USER_DEP = Depends(db_utils.get_current_user)
ADMIN_USER_DEP = Depends(db_utils.get_admin_user)


@pytest.fixture
def rsa_key() -> jwk.JWK:
    return jwk.JWK.generate(kty="RSA", size=2048, kid="test-key-id")


@pytest.fixture
def auth_app(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    @app.get("/me")
    async def me(current_user: User = CURRENT_USER_DEP) -> dict[str, Any]:
        return {
            "user_id": current_user.user_id,
            "global_admin": bool(getattr(current_user, "auth_global_admin", False)),
            "meal_admin": bool(getattr(current_user, "auth_meal_admin", False)),
        }

    @app.get("/admin")
    async def admin(
        current_user: AdminUserSchema = ADMIN_USER_DEP,
    ) -> dict[str, Any]:
        return {
            "user_id": current_user.user_id,
            "global_admin": current_user.global_admin,
            "meal_admin": current_user.meal_admin,
        }

    app.dependency_overrides[db_utils.get_db] = override_get_db
    monkeypatch.setattr(Config, "ENV", "test")
    monkeypatch.setattr(Config, "AUTH_DEV_HEADER_FALLBACK_ENABLED", False)
    monkeypatch.setattr(Config, "JWT_ISSUER", TEST_ISSUER)
    monkeypatch.setattr(Config, "JWT_AUDIENCE", TEST_AUDIENCE)
    monkeypatch.setattr(Config, "JWT_CLIENT_ID", TEST_CLIENT_ID)
    monkeypatch.setattr(Config, "JWT_ALLOWED_ALGORITHMS", ["RS256"])
    auth_utils.clear_jwks_cache()
    return app


@pytest.fixture
async def auth_client(auth_app: FastAPI) -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client


@pytest.fixture
def install_jwks(
    rsa_key: jwk.JWK,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[jwk.JWK | None], None]:
    def install(key: jwk.JWK | None = None) -> None:
        key_set = jwk.JWKSet()
        key_set.add(key or jwk.JWK.from_json(rsa_key.export(private_key=False)))

        async def fake_get_jwks(force_refresh: bool = False) -> jwk.JWKSet:
            _ = force_refresh
            return key_set

        monkeypatch.setattr(auth_utils, "get_jwks", fake_get_jwks)

    install()
    return install


@pytest.fixture
def make_token(rsa_key: jwk.JWK) -> Callable[..., str]:
    def make(  # noqa: PLR0913
        *,
        subject: str = "jwt-user",
        issuer: str = TEST_ISSUER,
        audience: str = TEST_AUDIENCE,
        expires_delta: timedelta = timedelta(minutes=5),
        algorithm: str = "RS256",
        kid: str = "test-key-id",
        include_sub: bool = True,
        roles: list[str] | None = None,
        client_roles: list[str] | None = None,
        signing_key: jwk.JWK | None = None,
    ) -> str:
        now = datetime.now(timezone.utc)
        claims: dict[str, Any] = {
            "iss": issuer,
            "aud": audience,
            "iat": int(now.timestamp()),
            "exp": int((now + expires_delta).timestamp()),
            "realm_access": {"roles": roles or []},
            "resource_access": {TEST_CLIENT_ID: {"roles": client_roles or []}},
        }
        if include_sub:
            claims["sub"] = subject

        token = jwt.JWT(
            header={"alg": algorithm, "kid": kid, "typ": "JWT"},
            claims=claims,
        )
        token.make_signed_token(signing_key or rsa_key)
        return token.serialize()

    return make


async def seed_user(db_session: AsyncSession, user_id: str) -> None:
    db_session.add(User(user_id=user_id))
    await db_session.commit()


async def test_valid_jwt_maps_to_user_and_roles(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_token: Callable[..., str],
    install_jwks: Callable[[jwk.JWK | None], None],
) -> None:
    _ = install_jwks
    await seed_user(db_session, "jwt-user")

    token = make_token(roles=["global_admin"], client_roles=["meal_admin"])
    response = await auth_client.get(
        "/me", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "jwt-user",
        "global_admin": True,
        "meal_admin": True,
    }


async def test_missing_auth_returns_401_with_bearer(
    auth_client: httpx.AsyncClient,
) -> None:
    response = await auth_client.get("/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    ("token_kwargs", "user_id"),
    [
        ({"expires_delta": timedelta(minutes=-5)}, "jwt-user"),
        ({"issuer": "https://issuer.test/wrong"}, "jwt-user"),
        ({"audience": "wrong-audience"}, "jwt-user"),
        ({"include_sub": False}, "jwt-user"),
        ({"kid": "unknown-key-id"}, "jwt-user"),
        ({"algorithm": "RS512"}, "jwt-user"),
    ],
)
async def test_invalid_jwt_cases_return_401(  # noqa: PLR0913
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_token: Callable[..., str],
    install_jwks: Callable[[jwk.JWK | None], None],
    token_kwargs: dict[str, Any],
    user_id: str,
) -> None:
    _ = install_jwks
    await seed_user(db_session, user_id)

    token = make_token(**token_kwargs)
    response = await auth_client.get(
        "/me", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_malformed_token_returns_401(
    auth_client: httpx.AsyncClient,
    install_jwks: Callable[[jwk.JWK | None], None],
) -> None:
    _ = install_jwks

    response = await auth_client.get(
        "/me",
        headers={"Authorization": "Bearer not-a-jwt"},
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_non_admin_on_admin_dependency_returns_403(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_token: Callable[..., str],
    install_jwks: Callable[[jwk.JWK | None], None],
) -> None:
    _ = install_jwks
    await seed_user(db_session, "jwt-user")

    token = make_token()
    response = await auth_client.get(
        "/admin",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403


async def test_admin_dependency_accepts_meal_admin_role(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_token: Callable[..., str],
    install_jwks: Callable[[jwk.JWK | None], None],
) -> None:
    _ = install_jwks
    await seed_user(db_session, "jwt-user")

    token = make_token(client_roles=["meal_admin"])
    response = await auth_client.get(
        "/admin",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["meal_admin"] is True


async def test_dev_header_fallback_enabled_in_test(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_user(db_session, "dev-user")
    monkeypatch.setattr(Config, "AUTH_DEV_HEADER_FALLBACK_ENABLED", True)
    monkeypatch.setattr(Config, "ENV", "test")

    response = await auth_client.get("/me", headers={"X-User-ID": "dev-user"})

    assert response.status_code == 200
    assert response.json()["user_id"] == "dev-user"


@pytest.mark.parametrize("env", ["test", "production"])
async def test_dev_header_fallback_disabled_or_production_rejected(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    env: str,
) -> None:
    await seed_user(db_session, "dev-user")
    monkeypatch.setattr(Config, "AUTH_DEV_HEADER_FALLBACK_ENABLED", env == "production")
    monkeypatch.setattr(Config, "ENV", env)

    response = await auth_client.get("/me", headers={"X-User-ID": "dev-user"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_jwt_takes_precedence_over_header_fallback(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_token: Callable[..., str],
    install_jwks: Callable[[jwk.JWK | None], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = install_jwks
    await seed_user(db_session, "jwt-user")
    await seed_user(db_session, "header-user")
    monkeypatch.setattr(Config, "AUTH_DEV_HEADER_FALLBACK_ENABLED", True)
    monkeypatch.setattr(Config, "ENV", "test")

    token = make_token(subject="jwt-user")
    response = await auth_client.get(
        "/me",
        headers={"Authorization": f"Bearer {token}", "X-User-ID": "header-user"},
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == "jwt-user"


async def test_mocked_discovery_and_jwks_fetch_without_keycloak(
    auth_app: FastAPI,
    db_session: AsyncSession,
    rsa_key: jwk.JWK,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_user(db_session, "jwt-user")
    auth_utils.clear_jwks_cache()
    monkeypatch.setattr(
        Config,
        "KEYCLOAK_DISCOVERY_URL",
        "https://issuer.test/.well-known/openid-configuration",
    )
    public_jwks = {"keys": [json.loads(rsa_key.export(private_key=False))]}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == Config.KEYCLOAK_DISCOVERY_URL:
            return httpx.Response(200, json={"jwks_uri": "https://issuer.test/jwks"})
        if str(request.url) == "https://issuer.test/jwks":
            return httpx.Response(200, json=public_jwks)
        return httpx.Response(404)

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(auth_utils.httpx, "AsyncClient", MockAsyncClient)
    transport = httpx.ASGITransport(app=auth_app)
    async with real_async_client(
        transport=transport, base_url="http://testserver"
    ) as client:
        token = make_token()
        response = await client.get("/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["user_id"] == "jwt-user"


async def test_cached_jwks_kid_miss_refreshes_and_validates_rotated_key(
    auth_app: FastAPI,
    db_session: AsyncSession,
    rsa_key: jwk.JWK,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_user(db_session, "rotated-user")
    auth_utils.clear_jwks_cache()
    monkeypatch.setattr(
        Config,
        "KEYCLOAK_DISCOVERY_URL",
        "https://issuer.test/.well-known/openid-configuration",
    )
    rotated_key = jwk.JWK.generate(kty="RSA", size=2048, kid="rotated-key-id")
    old_jwks = {"keys": [json.loads(rsa_key.export(private_key=False))]}
    rotated_jwks = {"keys": [json.loads(rotated_key.export(private_key=False))]}
    jwks_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal jwks_calls
        if str(request.url) == Config.KEYCLOAK_DISCOVERY_URL:
            return httpx.Response(200, json={"jwks_uri": "https://issuer.test/jwks"})
        if str(request.url) == "https://issuer.test/jwks":
            jwks_calls += 1
            return httpx.Response(
                200, json=old_jwks if jwks_calls == 1 else rotated_jwks
            )
        return httpx.Response(404)

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(auth_utils.httpx, "AsyncClient", MockAsyncClient)
    await auth_utils.get_jwks()

    transport = httpx.ASGITransport(app=auth_app)
    async with real_async_client(
        transport=transport, base_url="http://testserver"
    ) as client:
        token = make_token(
            subject="rotated-user",
            kid="rotated-key-id",
            signing_key=rotated_key,
        )
        response = await client.get("/me", headers={"Authorization": f"Bearer {token}"})

    assert jwks_calls == 2
    assert response.status_code == 200
    assert response.json()["user_id"] == "rotated-user"


async def test_cached_jwks_kid_miss_returns_401_after_refresh_still_misses(
    auth_app: FastAPI,
    db_session: AsyncSession,
    rsa_key: jwk.JWK,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_user(db_session, "rotated-user")
    auth_utils.clear_jwks_cache()
    monkeypatch.setattr(
        Config,
        "KEYCLOAK_DISCOVERY_URL",
        "https://issuer.test/.well-known/openid-configuration",
    )
    rotated_key = jwk.JWK.generate(kty="RSA", size=2048, kid="still-missing-key-id")
    old_jwks = {"keys": [json.loads(rsa_key.export(private_key=False))]}
    jwks_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal jwks_calls
        if str(request.url) == Config.KEYCLOAK_DISCOVERY_URL:
            return httpx.Response(200, json={"jwks_uri": "https://issuer.test/jwks"})
        if str(request.url) == "https://issuer.test/jwks":
            jwks_calls += 1
            return httpx.Response(200, json=old_jwks)
        return httpx.Response(404)

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(auth_utils.httpx, "AsyncClient", MockAsyncClient)
    await auth_utils.get_jwks()

    transport = httpx.ASGITransport(app=auth_app)
    async with real_async_client(
        transport=transport, base_url="http://testserver"
    ) as client:
        token = make_token(
            subject="rotated-user",
            kid="still-missing-key-id",
            signing_key=rotated_key,
        )
        response = await client.get("/me", headers={"Authorization": f"Bearer {token}"})

    assert jwks_calls == 2
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
