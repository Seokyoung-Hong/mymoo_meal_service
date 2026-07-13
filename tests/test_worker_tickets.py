"""Worker meal ticket and cash wallet API tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.restaurants import Restaurant
from app.models.user import User
from app.models.worker import CashTransaction, CashWallet, MealTicket
from app.utils import db as db_utils


async def seed_worker_and_restaurant(
    db_session: AsyncSession,
) -> tuple[User, User, Restaurant]:
    """Seed a worker, a restaurant owner, and an active restaurant."""
    worker = User(id=1, user_id="test-user-sub")
    owner = User(user_id="restaurant-owner-sub")
    restaurant = Restaurant(
        name="Ticket Restaurant",
        owner_user=owner,
        is_campus=True,
        is_active=True,
        establishment_type="fixed_menu_restaurant",
        price=7000,
    )
    db_session.add_all([worker, owner, restaurant])
    await db_session.commit()
    return worker, owner, restaurant


async def override_current_user(test_app: FastAPI, current_user: User) -> None:
    """Override current-user dependency for one test scenario."""

    async def override() -> AsyncGenerator[User, None]:
        yield current_user

    test_app.dependency_overrides[db_utils.get_current_user] = override


async def register_ticket(
    async_client: AsyncClient,
    *,
    code: str = "TICKET-001",
    amount: int = 5000,
) -> dict[str, object]:
    """Register a ticket and return response data."""
    response = await async_client.post(
        "/worker/tickets",
        json={"code": code, "amount": amount, "expires_on": "2099-12-31"},
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert isinstance(data, dict)
    return data


async def test_worker_registers_and_lists_meal_tickets(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await seed_worker_and_restaurant(db_session)

    ticket = await register_ticket(async_client)
    list_response = await async_client.get("/worker/tickets")

    assert ticket["code"] == "TICKET-001"
    assert ticket["amount"] == 5000
    assert ticket["status"] == "available"
    assert list_response.status_code == 200
    assert list_response.json()["data"][0]["code"] == "TICKET-001"


async def test_mock_card_charge_credits_cash_wallet(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await seed_worker_and_restaurant(db_session)

    charge_response = await async_client.post(
        "/worker/cash/card-charges",
        json={"amount": 3000, "card_last4": "1234"},
    )
    balance_response = await async_client.get("/worker/cash/balance")
    transactions_response = await async_client.get("/worker/cash/transactions")

    assert charge_response.status_code == 201
    assert charge_response.json()["data"]["amount"] == 3000
    assert charge_response.json()["data"]["transaction_type"] == "mock_card_charge"
    assert balance_response.status_code == 200
    assert balance_response.json()["data"] == {"balance": 3000}
    assert transactions_response.status_code == 200
    assert transactions_response.json()["data"][0]["amount"] == 3000


async def test_ticket_usage_request_approval_marks_ticket_used_and_deducts_cash(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
) -> None:
    _, owner, restaurant = await seed_worker_and_restaurant(db_session)
    await register_ticket(async_client, amount=5000)
    charge_response = await async_client.post(
        "/worker/cash/card-charges",
        json={"amount": 3000},
    )
    request_response = await async_client.post(
        "/worker/ticket-usage-requests",
        json={
            "ticket_code": "TICKET-001",
            "restaurant_id": restaurant.id,
            "meal_type": "lunch",
            "served_date": "2099-01-01",
            "meal_price": 7000,
        },
    )
    await override_current_user(test_app, owner)

    restaurant_list_response = await async_client.get(
        f"/restaurants/{restaurant.id}/ticket-usage-requests",
        params={"status": "pending"},
    )
    approve_response = await async_client.post(
        f"/restaurants/{restaurant.id}/ticket-usage-requests/"
        f"{request_response.json()['data']['id']}/approval"
    )

    wallet = await db_session.scalar(select(CashWallet).where(CashWallet.user_id == 1))
    ticket = await db_session.scalar(select(MealTicket).where(MealTicket.code == "TICKET-001"))
    cash_debit = await db_session.scalar(
        select(CashTransaction).where(
            CashTransaction.transaction_type == "ticket_shortfall_payment"
        )
    )

    assert charge_response.status_code == 201
    assert request_response.status_code == 201
    assert request_response.json()["data"]["status"] == "pending"
    assert request_response.json()["data"]["cash_amount_required"] == 2000
    assert restaurant_list_response.status_code == 200
    assert len(restaurant_list_response.json()["data"]) == 1
    assert approve_response.status_code == 200
    assert approve_response.json()["data"]["status"] == "used"
    assert ticket is not None
    assert ticket.status == "used"
    assert wallet is not None
    assert wallet.balance == 1000
    assert cash_debit is not None
    assert cash_debit.amount == -2000


async def test_ticket_usage_approval_requires_sufficient_cash(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
) -> None:
    _, owner, restaurant = await seed_worker_and_restaurant(db_session)
    await register_ticket(async_client, code="LOW-TICKET", amount=1000)
    request_response = await async_client.post(
        "/worker/ticket-usage-requests",
        json={
            "ticket_code": "LOW-TICKET",
            "restaurant_id": restaurant.id,
            "meal_price": 7000,
        },
    )
    await override_current_user(test_app, owner)

    approve_response = await async_client.post(
        f"/restaurants/{restaurant.id}/ticket-usage-requests/"
        f"{request_response.json()['data']['id']}/approval"
    )
    ticket = await db_session.scalar(select(MealTicket).where(MealTicket.code == "LOW-TICKET"))

    assert request_response.status_code == 201
    assert request_response.json()["data"]["cash_amount_required"] == 6000
    assert approve_response.status_code == 409
    assert approve_response.json()["detail"] == "부족분을 결제할 캐시 잔액이 부족합니다."
    assert ticket is not None
    assert ticket.status == "pending"
