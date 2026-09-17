"""Reader registration and signed config QR API tests."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.restaurants import Restaurant
from app.models.user import User
from app.routers.readers import (
    CONFIG_KEY_INFO,
    build_config_payload,
    build_config_qr,
)
from app.schemas.readers import ReaderConfigCreate
from app.utils import db as db_utils


SCANNER_KEY = "test-scanner-key"
SCANNER_KEY_HASH = hashlib.sha256(SCANNER_KEY.encode()).hexdigest()
BT_MAC = "44b176173668"


async def seed_restaurant(
    db_session: AsyncSession,
    manager: User,
    *,
    with_scanner_key: bool = True,
) -> tuple[User, User, Restaurant]:
    """Seed the authenticated fixture user as manager, plus an owner and restaurant."""
    owner = User(user_id="reader-owner-sub")
    restaurant = Restaurant(
        name="Reader Restaurant",
        owner_user=owner,
        is_campus=True,
        is_active=True,
        establishment_type="fixed_menu_restaurant",
        price=7000,
        scanner_key_hash=SCANNER_KEY_HASH if with_scanner_key else None,
    )
    restaurant.managers.append(manager)
    db_session.add_all([manager, owner, restaurant])
    await db_session.commit()
    return manager, owner, restaurant


async def override_current_user(test_app: FastAPI, current_user: User) -> None:
    """Override current-user dependency for one test scenario."""

    async def override() -> AsyncGenerator[User, None]:
        yield current_user

    test_app.dependency_overrides[db_utils.get_current_user] = override


def test_config_qr_matches_documented_vector() -> None:
    settings = ReaderConfigCreate(mode="ble")
    payload = build_config_payload(settings)
    config_key = hmac.new(
        bytes.fromhex(SCANNER_KEY_HASH), CONFIG_KEY_INFO, hashlib.sha256
    ).digest()

    assert SCANNER_KEY_HASH == (
        "494c1391a37a92811eee3f889a38bf2917baa832884deb0bcc94571f8ec69c0b"
    )
    assert config_key.hex() == (
        "05e22022caa116e85dc4ce83eecaba8370f01412f765ad97ad14130f9d83c1ac"
    )
    assert payload == "mode: ble"
    assert build_config_qr(SCANNER_KEY_HASH, BT_MAC, 1789600000, payload) == (
        "SKC1.AQAhq2pEsXYXNmgJbW9kZTogYmxl.4ebjRx-3vHPL0Ofdxn2ZQw"
    )


def test_payload_lines_follow_fixed_order() -> None:
    payload = build_config_payload(
        ReaderConfigCreate(t_rsp=3000, mode="auto", ssid="cafe-wifi", rid=1)
    )

    assert payload == "ssid: cafe-wifi\nmode: auto\nrid: 1\nt_rsp: 3000"


async def test_register_list_and_delete_reader(
    async_client: AsyncClient,
    db_session: AsyncSession,
    fake_authenticated_user: User,
) -> None:
    _, _, restaurant = await seed_restaurant(db_session, fake_authenticated_user)
    restaurant_id = restaurant.id

    created = await async_client.post(
        f"/restaurants/{restaurant_id}/readers",
        json={"bt_mac": "44:B1:76:17:36:68", "label": "counter"},
    )
    listed = await async_client.get(f"/restaurants/{restaurant_id}/readers")

    assert created.status_code == 201, created.text
    assert created.json()["data"]["bt_mac"] == BT_MAC
    assert created.json()["data"]["last_config_ctr"] == 0
    assert listed.status_code == 200
    assert [row["bt_mac"] for row in listed.json()["data"]] == [BT_MAC]

    relabeled = await async_client.post(
        f"/restaurants/{restaurant_id}/readers",
        json={"bt_mac": "44-b1-76-17-36-68", "label": "kitchen"},
    )
    assert relabeled.status_code == 201, relabeled.text
    assert relabeled.json()["data"]["id"] == created.json()["data"]["id"]
    assert relabeled.json()["data"]["label"] == "kitchen"

    deleted = await async_client.delete(
        f"/restaurants/{restaurant_id}/readers/{BT_MAC}"
    )
    after_delete = await async_client.get(f"/restaurants/{restaurant_id}/readers")

    assert deleted.status_code == 204
    assert after_delete.json()["data"] == []


async def test_register_rejects_malformed_mac(
    async_client: AsyncClient,
    db_session: AsyncSession,
    fake_authenticated_user: User,
) -> None:
    _, _, restaurant = await seed_restaurant(db_session, fake_authenticated_user)

    response = await async_client.post(
        f"/restaurants/{restaurant.id}/readers",
        json={"bt_mac": "not-a-mac"},
    )

    assert response.status_code == 400


async def test_register_conflicts_with_other_restaurant_mac(
    async_client: AsyncClient,
    db_session: AsyncSession,
    fake_authenticated_user: User,
) -> None:
    manager, owner, restaurant = await seed_restaurant(
        db_session, fake_authenticated_user
    )
    other = Restaurant(
        name="Other Restaurant",
        owner_user=owner,
        is_campus=True,
        is_active=True,
        establishment_type="fixed_menu_restaurant",
    )
    other.managers.append(manager)
    db_session.add(other)
    await db_session.commit()

    await async_client.post(
        f"/restaurants/{restaurant.id}/readers",
        json={"bt_mac": BT_MAC},
    )
    response = await async_client.post(
        f"/restaurants/{other.id}/readers",
        json={"bt_mac": BT_MAC},
    )

    assert response.status_code == 409


async def test_config_qr_counter_increases_within_one_second(
    async_client: AsyncClient,
    db_session: AsyncSession,
    fake_authenticated_user: User,
) -> None:
    _, _, restaurant = await seed_restaurant(db_session, fake_authenticated_user)
    restaurant_id = restaurant.id
    await async_client.post(
        f"/restaurants/{restaurant_id}/readers",
        json={"bt_mac": BT_MAC},
    )

    first = await async_client.post(
        f"/restaurants/{restaurant_id}/readers/{BT_MAC}/config-qr",
        json={"mode": "ble"},
    )
    second = await async_client.post(
        f"/restaurants/{restaurant_id}/readers/{BT_MAC}/config-qr",
        json={"mode": "ble"},
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["data"]["ctr"] > first.json()["data"]["ctr"]
    assert first.json()["data"]["qr"] != second.json()["data"]["qr"]
    assert first.json()["data"]["payload"] == "mode: ble"


async def test_config_qr_is_verifiable_and_bound_to_reader(
    async_client: AsyncClient,
    db_session: AsyncSession,
    fake_authenticated_user: User,
) -> None:
    _, _, restaurant = await seed_restaurant(db_session, fake_authenticated_user)
    restaurant_id = restaurant.id
    await async_client.post(
        f"/restaurants/{restaurant_id}/readers",
        json={"bt_mac": BT_MAC},
    )

    response = await async_client.post(
        f"/restaurants/{restaurant_id}/readers/{BT_MAC}/config-qr",
        json={"ssid": "cafe-wifi", "mode": "ble", "t_adv": 1500},
    )
    data = response.json()["data"]

    assert response.status_code == 200, response.text
    assert data["payload"] == "ssid: cafe-wifi\nmode: ble\nt_adv: 1500"
    assert data["qr"] == build_config_qr(
        SCANNER_KEY_HASH, BT_MAC, data["ctr"], data["payload"]
    )


async def test_config_qr_requires_registered_reader(
    async_client: AsyncClient,
    db_session: AsyncSession,
    fake_authenticated_user: User,
) -> None:
    _, _, restaurant = await seed_restaurant(db_session, fake_authenticated_user)

    response = await async_client.post(
        f"/restaurants/{restaurant.id}/readers/aabbccddeeff/config-qr",
        json={"mode": "ble"},
    )

    assert response.status_code == 404


async def test_config_qr_requires_scanner_key_and_a_field(
    async_client: AsyncClient,
    db_session: AsyncSession,
    fake_authenticated_user: User,
) -> None:
    _, _, restaurant = await seed_restaurant(
        db_session, fake_authenticated_user, with_scanner_key=False
    )
    restaurant_id = restaurant.id
    await async_client.post(
        f"/restaurants/{restaurant_id}/readers",
        json={"bt_mac": BT_MAC},
    )

    empty = await async_client.post(
        f"/restaurants/{restaurant_id}/readers/{BT_MAC}/config-qr",
        json={},
    )
    no_key = await async_client.post(
        f"/restaurants/{restaurant_id}/readers/{BT_MAC}/config-qr",
        json={"mode": "ble"},
    )

    assert empty.status_code == 400
    assert no_key.status_code == 400


async def test_config_qr_rejects_unknown_fields_and_newline_injection(
    async_client: AsyncClient,
    db_session: AsyncSession,
    fake_authenticated_user: User,
) -> None:
    _, _, restaurant = await seed_restaurant(db_session, fake_authenticated_user)
    restaurant_id = restaurant.id
    await async_client.post(
        f"/restaurants/{restaurant_id}/readers",
        json={"bt_mac": BT_MAC},
    )

    extra_field = await async_client.post(
        f"/restaurants/{restaurant_id}/readers/{BT_MAC}/config-qr",
        json={
            "key": "1122334455667788990011223344556677889900112233445566778899001122"
        },
    )
    injected = await async_client.post(
        f"/restaurants/{restaurant_id}/readers/{BT_MAC}/config-qr",
        json={"ssid": "cafe\nrid: 9"},
    )
    out_of_range = await async_client.post(
        f"/restaurants/{restaurant_id}/readers/{BT_MAC}/config-qr",
        json={"t_adv": 999},
    )

    assert extra_field.status_code == 422
    assert injected.status_code == 422
    assert out_of_range.status_code == 422


async def test_rid_is_owner_or_admin_only(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
    fake_authenticated_user: User,
) -> None:
    manager, owner, restaurant = await seed_restaurant(
        db_session, fake_authenticated_user
    )
    restaurant_id = restaurant.id
    await async_client.post(
        f"/restaurants/{restaurant_id}/readers",
        json={"bt_mac": BT_MAC},
    )

    manager_rid = await async_client.post(
        f"/restaurants/{restaurant_id}/readers/{BT_MAC}/config-qr",
        json={"rid": 1},
    )
    manager_mode = await async_client.post(
        f"/restaurants/{restaurant_id}/readers/{BT_MAC}/config-qr",
        json={"mode": "ble", "t_req": 4000},
    )
    await override_current_user(test_app, owner)
    owner_rid = await async_client.post(
        f"/restaurants/{restaurant_id}/readers/{BT_MAC}/config-qr",
        json={"rid": restaurant_id},
    )

    assert manager.id != owner.id
    assert manager_rid.status_code == 403
    assert manager_mode.status_code == 200, manager_mode.text
    assert owner_rid.status_code == 200, owner_rid.text
    assert owner_rid.json()["data"]["payload"] == f"rid: {restaurant_id}"


async def test_config_qr_audit_log_records_field_names_only(
    async_client: AsyncClient,
    db_session: AsyncSession,
    fake_authenticated_user: User,
) -> None:
    _, _, restaurant = await seed_restaurant(db_session, fake_authenticated_user)
    restaurant_id = restaurant.id
    await async_client.post(
        f"/restaurants/{restaurant_id}/readers",
        json={"bt_mac": BT_MAC},
    )
    await async_client.post(
        f"/restaurants/{restaurant_id}/readers/{BT_MAC}/config-qr",
        json={"ssid": "cafe-wifi", "password": "super-secret"},
    )

    log = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "restaurant_reader.config_qr.issue")
    )

    assert log is not None
    assert log.resource_type == "restaurant_reader"
    assert log.after is not None
    assert log.after["fields"] == ["ssid", "password"]
    assert log.after["bt_mac"] == BT_MAC
    assert "super-secret" not in str(log.after)
