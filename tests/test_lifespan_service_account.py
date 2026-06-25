from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.models.user import User
from app.utils import lifespan


class SessionContext:
    """Async context wrapper that keeps the fixture session open."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session supplied by the db_session fixture."""
        self.session: AsyncSession = session

    async def __aenter__(self) -> AsyncSession:
        """Return the fixture session without creating a new connection."""
        return self.session

    async def __aexit__(self, *args: object) -> None:
        """Leave fixture cleanup to pytest instead of closing the session here."""


def set_valid_service_account_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Config, "SERVICE_ACCOUNT_SUB", "valid-service-sub")
    monkeypatch.setattr(Config, "KC_CLIENT_SECRET", "valid-client-secret")
    monkeypatch.setattr(Config, "KC_REALM", "valid-realm")


@pytest.mark.asyncio
async def test_service_account_bootstrap_skips_placeholder_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Config,
        "SERVICE_ACCOUNT_SUB",
        "replace-with-service-account-sub",
    )
    monkeypatch.setattr(Config, "KC_CLIENT_SECRET", "valid-client-secret")
    monkeypatch.setattr(Config, "KC_REALM", "valid-realm")

    async def fail_keycloak_lookup(user_id: str) -> bool:
        raise AssertionError(f"Keycloak should not be called for {user_id}")

    def fail_session() -> None:
        raise AssertionError("DB should not be opened for placeholder config")

    monkeypatch.setattr(lifespan, "keycloak_user_exists_by_id", fail_keycloak_lookup)
    monkeypatch.setattr(lifespan, "AsyncSessionLocal", fail_session)

    await lifespan.ensure_service_account_in_db()


@pytest.mark.asyncio
async def test_service_account_bootstrap_skips_missing_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Config, "SERVICE_ACCOUNT_SUB", None)
    monkeypatch.setattr(Config, "KC_CLIENT_SECRET", "valid-client-secret")
    monkeypatch.setattr(Config, "KC_REALM", "valid-realm")

    async def fail_keycloak_lookup(user_id: str) -> bool:
        raise AssertionError(f"Keycloak should not be called for {user_id}")

    def fail_session() -> None:
        raise AssertionError("DB should not be opened for missing config")

    monkeypatch.setattr(lifespan, "keycloak_user_exists_by_id", fail_keycloak_lookup)
    monkeypatch.setattr(lifespan, "AsyncSessionLocal", fail_session)

    await lifespan.ensure_service_account_in_db()


@pytest.mark.asyncio
async def test_service_account_bootstrap_skips_unreachable_keycloak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_valid_service_account_config(monkeypatch)

    async def raise_keycloak_lookup(user_id: str) -> bool:
        raise RuntimeError(f"Keycloak unavailable for {user_id}")

    def fail_session() -> None:
        raise AssertionError("DB should not be opened when Keycloak is unreachable")

    monkeypatch.setattr(lifespan, "keycloak_user_exists_by_id", raise_keycloak_lookup)
    monkeypatch.setattr(lifespan, "AsyncSessionLocal", fail_session)

    await lifespan.ensure_service_account_in_db()


@pytest.mark.asyncio
async def test_service_account_bootstrap_inserts_user_when_keycloak_valid(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
) -> None:
    set_valid_service_account_config(monkeypatch)

    async def confirm_keycloak_user_exists(user_id: str) -> bool:
        return user_id == "valid-service-sub"

    monkeypatch.setattr(
        lifespan, "keycloak_user_exists_by_id", confirm_keycloak_user_exists
    )
    monkeypatch.setattr(
        lifespan, "AsyncSessionLocal", lambda: SessionContext(db_session)
    )

    await lifespan.ensure_service_account_in_db()

    result = await db_session.execute(
        select(User).where(User.user_id == "valid-service-sub")
    )
    assert result.scalar_one().user_id == "valid-service-sub"


@pytest.mark.asyncio
async def test_service_account_bootstrap_still_fails_when_keycloak_user_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_valid_service_account_config(monkeypatch)

    async def keycloak_user_missing(_user_id: str) -> bool:
        return False

    monkeypatch.setattr(lifespan, "keycloak_user_exists_by_id", keycloak_user_missing)

    with pytest.raises(RuntimeError, match="Keycloak에 service_account가 없습니다"):
        await lifespan.ensure_service_account_in_db()
