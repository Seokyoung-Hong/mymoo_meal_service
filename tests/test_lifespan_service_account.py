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


@pytest.mark.asyncio
async def test_service_account_bootstrap_skips_placeholder_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Config,
        "SERVICE_ACCOUNT_SUB",
        "replace-with-service-account-sub",
    )

    def fail_session() -> None:
        raise AssertionError("DB should not be opened for placeholder config")

    monkeypatch.setattr(lifespan, "AsyncSessionLocal", fail_session)

    await lifespan.ensure_service_account_in_db()


@pytest.mark.asyncio
async def test_service_account_bootstrap_skips_missing_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Config, "SERVICE_ACCOUNT_SUB", None)

    def fail_session() -> None:
        raise AssertionError("DB should not be opened for missing config")

    monkeypatch.setattr(lifespan, "AsyncSessionLocal", fail_session)

    await lifespan.ensure_service_account_in_db()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_service_account_bootstrap_inserts_configured_user(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
) -> None:
    set_valid_service_account_config(monkeypatch)
    monkeypatch.setattr(
        lifespan, "AsyncSessionLocal", lambda: SessionContext(db_session)
    )

    await lifespan.ensure_service_account_in_db()

    result = await db_session.execute(
        select(User).where(User.user_id == "valid-service-sub")
    )
    assert result.scalar_one().user_id == "valid-service-sub"
