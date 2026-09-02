"""Worker allowance wallet, cash wallet, and restaurant scan API tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import date
from uuid import uuid4

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


async def seed_bucket(
    db_session: AsyncSession, owner_id: int, amount: int, expires_on: str
) -> MealTicket:
    """Seed one allowance bucket the way the admin issue endpoint would."""
    bucket = MealTicket(
        code=uuid4().hex,
        owner_id=owner_id,
        amount=amount,
        remaining_amount=amount,
        expires_on=date.fromisoformat(expires_on),
    )
    db_session.add(bucket)
    await db_session.commit()
    return bucket


async def override_current_user(test_app: FastAPI, current_user: User) -> None:
    """Override current-user dependency for one test scenario."""

    async def override() -> AsyncGenerator[User, None]:
        yield current_user

    test_app.dependency_overrides[db_utils.get_current_user] = override


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


async def test_allowance_balance_sums_unexpired_buckets(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await seed_worker_and_restaurant(db_session)
    await seed_bucket(db_session, 1, 5000, "2099-12-31")
    stale = await seed_bucket(db_session, 1, 3000, "2000-01-01")

    balance_response = await async_client.get("/worker/allowance/balance")
    tickets_response = await async_client.get("/worker/tickets")
    await db_session.refresh(stale)

    assert balance_response.status_code == 200
    assert balance_response.json()["data"] == {"balance": 5000}
    assert tickets_response.status_code == 200
    assert {t["status"] for t in tickets_response.json()["data"]} == {
        "available",
        "expired",
    }
    assert stale.status == "expired"


async def test_scan_deducts_soonest_expiring_bucket_first_then_cash(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
) -> None:
    _, owner, restaurant = await seed_worker_and_restaurant(db_session)
    restaurant_id = restaurant.id
    later = await seed_bucket(db_session, 1, 4000, "2099-06-30")
    sooner = await seed_bucket(db_session, 1, 5000, "2099-01-31")
    await async_client.post("/worker/cash/card-charges", json={"amount": 3000})
    await override_current_user(test_app, owner)

    first_scan = await async_client.post(
        f"/restaurants/{restaurant_id}/ticket-scans",
        json={"worker_user_id": "test-user-sub", "meal_price": 7000},
    )
    await db_session.refresh(sooner)
    await db_session.refresh(later)

    assert first_scan.status_code == 201, first_scan.text
    first = first_scan.json()["data"]
    assert first["status"] == "used"
    assert first["ticket_amount_applied"] == 7000
    assert first["cash_amount_required"] == 0
    assert first["ticket_id"] == sooner.id
    assert (sooner.remaining_amount, sooner.status) == (0, "used")
    assert (later.remaining_amount, later.status) == (2000, "available")

    second_scan = await async_client.post(
        f"/restaurants/{restaurant_id}/ticket-scans",
        json={"worker_user_id": "test-user-sub", "meal_price": 5000},
    )
    await db_session.refresh(later)
    wallet = await db_session.scalar(select(CashWallet).where(CashWallet.user_id == 1))
    cash_debit = await db_session.scalar(
        select(CashTransaction).where(
            CashTransaction.transaction_type == "ticket_shortfall_payment"
        )
    )

    assert second_scan.status_code == 201, second_scan.text
    second = second_scan.json()["data"]
    assert second["ticket_amount_applied"] == 2000
    assert second["cash_amount_required"] == 3000
    assert (later.remaining_amount, later.status) == (0, "used")
    assert wallet is not None and wallet.balance == 0
    assert cash_debit is not None and cash_debit.amount == -3000

    third_scan = await async_client.post(
        f"/restaurants/{restaurant_id}/ticket-scans",
        json={"worker_user_id": "test-user-sub", "meal_price": 1000},
    )
    assert third_scan.status_code == 409
    assert third_scan.json()["detail"] == "부족분을 결제할 캐시 잔액이 부족합니다."


async def test_scan_rejects_unknown_worker_and_keeps_balance_on_shortfall(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
) -> None:
    _, owner, restaurant = await seed_worker_and_restaurant(db_session)
    restaurant_id = restaurant.id
    bucket = await seed_bucket(db_session, 1, 1000, "2099-12-31")
    await override_current_user(test_app, owner)

    unknown_response = await async_client.post(
        f"/restaurants/{restaurant_id}/ticket-scans",
        json={"worker_user_id": "no-such-worker"},
    )
    shortfall_response = await async_client.post(
        f"/restaurants/{restaurant_id}/ticket-scans",
        json={"worker_user_id": "test-user-sub", "meal_price": 7000},
    )
    await db_session.refresh(bucket)

    assert unknown_response.status_code == 404
    assert shortfall_response.status_code == 409
    # 결제 실패 시 지갑 잔액은 그대로 남아야 한다.
    assert (bucket.remaining_amount, bucket.status) == (1000, "available")

    exact_scan = await async_client.post(
        f"/restaurants/{restaurant_id}/ticket-scans",
        json={"worker_user_id": "test-user-sub", "meal_price": 1000},
    )
    empty_scan = await async_client.post(
        f"/restaurants/{restaurant_id}/ticket-scans",
        json={"worker_user_id": "test-user-sub", "meal_price": 1000},
    )
    await db_session.refresh(bucket)

    assert exact_scan.status_code == 201, exact_scan.text
    assert (bucket.remaining_amount, bucket.status) == (0, "used")
    assert empty_scan.status_code == 409


async def test_qr_device_scans_with_scanner_key_header(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
) -> None:
    _, owner, restaurant = await seed_worker_and_restaurant(db_session)
    restaurant_id = restaurant.id
    bucket = await seed_bucket(db_session, 1, 10000, "2099-12-31")

    qr_response = await async_client.get("/worker/qr")
    await override_current_user(test_app, owner)
    key_response = await async_client.post(f"/restaurants/{restaurant_id}/scanner-key")

    assert qr_response.status_code == 200
    qr_url = qr_response.json()["data"]["url"]
    assert qr_url.endswith("/meal/scan?worker=test-user-sub")
    assert key_response.status_code == 201, key_response.text
    scanner_key = key_response.json()["data"]["scanner_key"]
    assert key_response.json()["data"]["header"] == "X-Scanner-Key"

    # 기기는 QR의 URL을 그대로 GET 하고 고정 헤더만 붙인다. 가격은 식당 정책(7000)에서 온다.
    scan_path = qr_url.split("/meal", 1)[1]
    device_scan = await async_client.get(
        scan_path, headers={"X-Scanner-Key": scanner_key}
    )
    wrong_key = await async_client.get(scan_path, headers={"X-Scanner-Key": "nope"})
    no_key = await async_client.get(scan_path)
    await db_session.refresh(bucket)

    assert device_scan.status_code == 200, device_scan.text
    data = device_scan.json()["data"]
    assert data["meal_price"] == 7000
    assert data["ticket_amount_applied"] == 7000
    assert data["approved_by_user_id"] == "restaurant-owner-sub"
    assert bucket.remaining_amount == 3000
    assert wrong_key.status_code == 401
    assert no_key.status_code == 401


async def test_restaurant_revenue_reflects_payments_immediately(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
) -> None:
    _, owner, restaurant = await seed_worker_and_restaurant(db_session)
    restaurant_id = restaurant.id
    await seed_bucket(db_session, 1, 9000, "2099-12-31")
    await async_client.post("/worker/cash/card-charges", json={"amount": 5000})
    await override_current_user(test_app, owner)

    for served_date, price in (
        ("2099-03-01", 7000),
        ("2099-03-01", 4000),
        ("2099-03-02", 3000),
    ):
        scan = await async_client.post(
            f"/restaurants/{restaurant_id}/ticket-scans",
            json={
                "worker_user_id": "test-user-sub",
                "meal_price": price,
                "served_date": served_date,
            },
        )
        assert scan.status_code == 201, scan.text

    revenue = await async_client.get(
        f"/restaurants/{restaurant_id}/revenue",
        params={"date_from": "2099-03-01", "date_to": "2099-03-02"},
    )
    single_day = await async_client.get(
        f"/restaurants/{restaurant_id}/revenue", params={"date_from": "2099-03-02"}
    )
    history = await async_client.get(
        f"/restaurants/{restaurant_id}/ticket-usage-requests",
        params={"date_from": "2099-03-02", "date_to": "2099-03-02"},
    )
    bad_range = await async_client.get(
        f"/restaurants/{restaurant_id}/revenue",
        params={"date_from": "2099-03-02", "date_to": "2099-03-01"},
    )

    assert revenue.status_code == 200, revenue.text
    data = revenue.json()["data"]
    # 9000 식대 + 5000 캐시 = 14000 전액 결제
    assert (data["transaction_count"], data["total_amount"]) == (3, 14000)
    assert (data["allowance_amount"], data["cash_amount"]) == (9000, 5000)
    assert [(r["served_date"], r["total_amount"]) for r in data["by_day"]] == [
        ("2099-03-01", 11000),
        ("2099-03-02", 3000),
    ]
    assert single_day.json()["data"]["total_amount"] == 3000
    assert single_day.json()["data"]["date_to"] == "2099-03-02"
    assert [h["meal_price"] for h in history.json()["data"]] == [3000]
    assert bad_range.status_code == 400
