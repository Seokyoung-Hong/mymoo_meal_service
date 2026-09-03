"""SSE 이벤트 발행 검증: 결제·충전이 근로자/식당 토픽에 실리는지."""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils import events
from tests.test_worker_tickets import (
    override_current_user,
    seed_bucket,
    seed_worker_and_restaurant,
)


def _parse(frame: str) -> tuple[str, dict]:
    event_line, data_line, _ = frame.split("\n", 2)
    return (
        event_line.removeprefix("event: "),
        json.loads(data_line.removeprefix("data: ")),
    )


async def _subscriber(topic: str) -> tuple[asyncio.Queue[str], asyncio.Task]:
    queue: asyncio.Queue[str] = asyncio.Queue()

    async def pump() -> None:
        async for frame in events.subscribe(topic):
            await queue.put(frame)

    task = asyncio.create_task(pump())
    assert await queue.get() == ": connected\n\n"
    return queue, task


async def test_scan_and_charge_publish_sse_events(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
) -> None:
    _, owner, restaurant = await seed_worker_and_restaurant(db_session)
    await seed_bucket(db_session, 1, 5000, "2099-12-31")
    worker_q, worker_task = await _subscriber("worker:1")
    shop_q, shop_task = await _subscriber(f"restaurant:{restaurant.id}")
    try:
        charge = await async_client.post(
            "/worker/cash/card-charges", json={"amount": 3000}
        )
        assert charge.status_code == 201, charge.text
        event, data = _parse(await asyncio.wait_for(worker_q.get(), 1))
        assert (event, data["cash_balance"]) == ("cash_charged", 3000)

        await override_current_user(test_app, owner)
        scan = await async_client.post(
            f"/restaurants/{restaurant.id}/ticket-scans",
            json={"worker_user_id": "test-user-sub", "meal_price": 7000},
        )
        assert scan.status_code == 201, scan.text

        event, data = _parse(await asyncio.wait_for(worker_q.get(), 1))
        assert event == "payment"
        assert (data["allowance_balance"], data["cash_balance"]) == (0, 1000)
        assert data["usage_request"]["id"] == scan.json()["data"]["id"]

        event, data = _parse(await asyncio.wait_for(shop_q.get(), 1))
        assert event == "payment"
        assert data["usage_request"]["meal_price"] == 7000
        assert shop_q.empty() and worker_q.empty()
    finally:
        for task in (worker_task, shop_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    assert not events._subscribers  # 구독 해제 시 토픽이 정리된다
