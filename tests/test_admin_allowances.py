"""Admin meal allowance API tests."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.models.user import User
from app.models.worker import MealTicket
from app.utils import db as db_utils


async def seed_admin_and_workers(
    db_session: AsyncSession,
) -> tuple[User, User]:
    """Seed the fake admin (current user) and two workers."""
    admin = User(id=1, user_id="test-user-sub")
    worker_a = User(user_id="worker-a-sub")
    worker_b = User(user_id="worker-b-sub")
    db_session.add_all([admin, worker_a, worker_b])
    await db_session.commit()
    return worker_a, worker_b


async def test_admin_issues_allowances_to_two_workers(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
) -> None:
    worker_a, worker_b = await seed_admin_and_workers(db_session)

    issue_response = await async_client.post(
        "/admin/meal-allowances",
        json={
            "worker_user_ids": ["worker-a-sub", "worker-b-sub"],
            "amount": 6000,
            "expires_on": "2099-12-31",
        },
    )
    overview_response = await async_client.get("/admin/meal-allowances")
    filtered_response = await async_client.get(
        "/admin/meal-allowances",
        params={"worker_user_id": "worker-a-sub"},
    )

    assert issue_response.status_code == 201, issue_response.text
    issued = issue_response.json()["data"]
    assert len(issued) == 2
    assert [item["worker_user_id"] for item in issued] == [
        "worker-a-sub",
        "worker-b-sub",
    ]
    assert all(item["amount"] == 6000 for item in issued)
    assert all(item["status"] == "available" for item in issued)
    assert all(item["expires_on"] == "2099-12-31" for item in issued)

    assert overview_response.status_code == 200
    overview = overview_response.json()["data"]
    assert len(overview) == 2
    # 최신순 정렬: 마지막에 만들어진 티켓이 먼저 온다.
    assert overview[0]["id"] > overview[1]["id"]

    assert filtered_response.status_code == 200
    filtered = filtered_response.json()["data"]
    assert len(filtered) == 1
    assert filtered[0]["worker_user_id"] == "worker-a-sub"

    # 지급받은 근로자 본인의 식권 목록에도 나타난다.
    async def override_worker() -> User:
        return worker_a

    test_app.dependency_overrides[db_utils.get_current_user] = override_worker
    worker_tickets_response = await async_client.get("/worker/tickets")
    assert worker_tickets_response.status_code == 200
    worker_tickets = worker_tickets_response.json()["data"]
    assert len(worker_tickets) == 1
    assert worker_tickets[0]["code"] == issued[0]["code"]
    _ = worker_b


async def test_unknown_worker_user_id_issues_nothing(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await seed_admin_and_workers(db_session)

    response = await async_client.post(
        "/admin/meal-allowances",
        json={
            "worker_user_ids": ["worker-a-sub", "no-such-sub"],
            "amount": 6000,
            "expires_on": "2099-12-31",
        },
    )
    ticket_count = await db_session.scalar(select(func.count(MealTicket.id)))

    assert response.status_code == 404
    assert "no-such-sub" in response.json()["detail"]
    assert ticket_count == 0


async def test_past_expires_on_is_rejected(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await seed_admin_and_workers(db_session)

    response = await async_client.post(
        "/admin/meal-allowances",
        json={
            "worker_user_ids": ["worker-a-sub"],
            "amount": 6000,
            "expires_on": "2000-01-01",
        },
    )

    assert response.status_code == 400


async def test_non_admin_is_forbidden(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_admin_and_workers(db_session)
    # 관리자 오버라이드를 제거하고 실제 get_admin_user가 동작하게 한다.
    del test_app.dependency_overrides[db_utils.get_admin_user]
    monkeypatch.setattr(Config, "ENV", "test")
    monkeypatch.setattr(Config, "AUTH_DEV_HEADER_FALLBACK_ENABLED", True)

    response = await async_client.get(
        "/admin/meal-allowances",
        headers={"X-User-ID": "worker-a-sub"},
    )

    assert response.status_code == 403
