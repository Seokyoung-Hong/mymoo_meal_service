"""BLE 중계 결제(POST /relay) 테스트 — 문서 docs/ble-relay.md의 벡터 포함."""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.routers import worker as worker_router
from tests.test_worker_tickets import (
    override_current_user,
    seed_bucket,
    seed_worker_and_restaurant,
)

WORKER_UUID = "2ab708e1-5dc7-4f54-abc4-b291f0a08dd1"
NONCE = bytes.fromhex("0102030405060708")
SID = bytes.fromhex("a1b2c3d4e5f60718")


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def make_tok(secret: bytes, worker_uuid: str, expires_at: int) -> bytes:
    body = UUID(worker_uuid).bytes + struct.pack("<I", expires_at)
    return body + hmac.new(secret, b"\x01" + body, hashlib.sha256).digest()[:16]


def make_req(key: bytes, tok: bytes, *, rid: int = 1, nonce: bytes = NONCE) -> bytes:
    head = struct.pack("<BH", 1, rid) + nonce + SID + tok
    return head + hmac.new(key, head, hashlib.sha256).digest()


@pytest.fixture(autouse=True)
def fixed_secret(monkeypatch: pytest.MonkeyPatch) -> bytes:
    """테스트마다 같은 SERVER_SECRET을 쓰고 프로세스 캐시를 비운다."""
    secret = bytes(range(32))
    monkeypatch.setattr(Config, "RELAY_TOKEN_SECRET", secret)
    worker_router._relay_replies.clear()
    worker_router._relay_hits.clear()
    return secret


def test_document_vectors_match(fixed_secret: bytes) -> None:
    """문서의 테스트 벡터와 서버 구현이 같은 바이트를 내야 한다."""
    key = hashlib.sha256(b"test-scanner-key").digest()
    assert key.hex() == (
        "494c1391a37a92811eee3f889a38bf2917baa832884deb0bcc94571f8ec69c0b"
    )

    tok = make_tok(fixed_secret, WORKER_UUID, 1800000000)
    assert tok.hex() == (
        "2ab708e15dc74f54abc4b291f0a08dd100d2496bec53c5d872f2d788b47a106c992f2caa"
    )

    req = make_req(key, tok)
    assert req.hex().endswith(
        "a318224af023fd24b16281b631387268db344abca5ac8461e7a76bf195dd10a6"
    )
    assert len(req) == worker_router.RELAY_REQUEST_LEN

    resp = worker_router._relay_pack(
        worker_router.RELAY_OK,
        NONCE,
        key,
        meal_price=9000,
        ticket_applied=9000,
        cash=0,
        txid=7,
    )
    assert resp.hex() == (
        "0100010203040506070828230000282300000000000007000000000000008e25"
        "7cae3e58a5cfb81d15e95acb2a140f3421e93ca9323622272e862e90d61d"
    )
    assert len(resp) == 62


async def seed_relay_restaurant(
    async_client: AsyncClient, db_session: AsyncSession, test_app: FastAPI
) -> tuple[int, bytes]:
    """근로자(UUID sub)·식당·식대 버킷을 만들고 (식당 id, K)를 돌려준다."""
    worker, owner, restaurant = await seed_worker_and_restaurant(db_session)
    worker.user_id = WORKER_UUID
    await db_session.commit()
    await seed_bucket(db_session, worker.id, 10000, "2099-12-31")

    restaurant_id = restaurant.id
    await override_current_user(test_app, owner)
    key_response = await async_client.post(f"/restaurants/{restaurant_id}/scanner-key")
    assert key_response.status_code == 201, key_response.text
    scanner_key = key_response.json()["data"]["scanner_key"]
    return restaurant_id, hashlib.sha256(scanner_key.encode()).digest()


async def test_relay_charges_once_and_signs_response(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
    fixed_secret: bytes,
) -> None:
    restaurant_id, key = await seed_relay_restaurant(async_client, db_session, test_app)
    tok = make_tok(fixed_secret, WORKER_UUID, int(time.time()) + 60)
    req = make_req(key, tok, rid=restaurant_id)

    first = await async_client.post("/relay", json={"req": b64url(req)})
    retry = await async_client.post("/relay", json={"req": b64url(req)})

    assert first.status_code == 200, first.text
    resp = b64url_decode(first.json()["resp"])
    assert len(resp) == 62
    assert resp[0] == 1 and resp[1] == worker_router.RELAY_OK
    assert resp[2:10] == NONCE
    price, applied, cash = struct.unpack("<III", resp[10:22])
    (txid,) = struct.unpack("<Q", resp[22:30])
    assert (price, applied, cash) == (7000, 7000, 0)
    assert txid > 0
    assert hmac.compare_digest(
        resp[30:], hmac.new(key, resp[:30], hashlib.sha256).digest()
    )
    # 같은 87바이트 재전송은 저장된 응답을 그대로 돌려주고 두 번 결제하지 않는다.
    assert retry.json()["resp"] == first.json()["resp"]

    history = await async_client.get(
        f"/restaurants/{restaurant_id}/ticket-usage-requests"
    )
    assert [h["meal_price"] for h in history.json()["data"]] == [7000]


async def test_relay_rejects_bad_mac_unknown_restaurant_and_expired_token(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
    fixed_secret: bytes,
) -> None:
    restaurant_id, key = await seed_relay_restaurant(async_client, db_session, test_app)
    tok = make_tok(fixed_secret, WORKER_UUID, int(time.time()) + 60)

    tampered = bytearray(make_req(key, tok, rid=restaurant_id, nonce=b"\x11" * 8))
    tampered[-1] ^= 0xFF
    bad_mac = await async_client.post("/relay", json={"req": b64url(bytes(tampered))})

    unknown = await async_client.post(
        "/relay",
        json={"req": b64url(make_req(key, tok, rid=9999, nonce=b"\x22" * 8))},
    )
    expired = await async_client.post(
        "/relay",
        json={
            "req": b64url(
                make_req(
                    key,
                    make_tok(fixed_secret, WORKER_UUID, int(time.time()) - 1),
                    rid=restaurant_id,
                    nonce=b"\x33" * 8,
                )
            )
        },
    )
    forged = await async_client.post(
        "/relay",
        json={
            "req": b64url(
                make_req(
                    key,
                    make_tok(b"\x99" * 32, WORKER_UUID, int(time.time()) + 60),
                    rid=restaurant_id,
                    nonce=b"\x44" * 8,
                )
            )
        },
    )
    short = await async_client.post("/relay", json={"req": b64url(b"\x01\x02")})
    bad_version = await async_client.post(
        "/relay", json={"req": b64url(b"\x02" + bytes(86))}
    )

    # 거절도 200이고 status로 구분한다. 결제는 일어나지 않는다.
    bad_mac_resp = b64url_decode(bad_mac.json()["resp"])
    assert bad_mac_resp[1] == worker_router.RELAY_DEVICE_AUTH
    assert hmac.compare_digest(  # K를 아는 식당이므로 정상 서명한다
        bad_mac_resp[30:], hmac.new(key, bad_mac_resp[:30], hashlib.sha256).digest()
    )
    unknown_resp = b64url_decode(unknown.json()["resp"])
    assert unknown_resp[1] == worker_router.RELAY_DEVICE_AUTH
    assert unknown_resp[30:] == bytes(32)  # K를 모르면 mac은 0
    assert b64url_decode(expired.json()["resp"])[1] == worker_router.RELAY_QR_EXPIRED
    assert b64url_decode(forged.json()["resp"])[1] == worker_router.RELAY_INVALID_QR
    assert short.status_code == 400 and "error" in short.json()
    assert bad_version.status_code == 400

    history = await async_client.get(
        f"/restaurants/{restaurant_id}/ticket-usage-requests"
    )
    assert history.json()["data"] == []


async def test_worker_qr_returns_signed_token_for_uuid_subs(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
    fixed_secret: bytes,
) -> None:
    worker, _, _ = await seed_worker_and_restaurant(db_session)

    legacy = await async_client.get("/worker/qr")  # sub가 UUID가 아닌 계정
    worker.user_id = WORKER_UUID
    await db_session.commit()
    await override_current_user(test_app, worker)
    uuid_sub = await async_client.get("/worker/qr")

    assert legacy.json()["data"]["tok"] is None
    assert legacy.json()["data"]["ttl"] == 60
    tok = b64url_decode(uuid_sub.json()["data"]["tok"])
    assert len(tok) == 36
    assert worker_router._read_ticket_token(tok) == (
        WORKER_UUID,
        worker_router.RELAY_OK,
    )
