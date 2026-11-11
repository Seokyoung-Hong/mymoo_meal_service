"""BLE 중계 v2 테스트 — docs/ble-relay-v2.md의 벡터 포함."""

from __future__ import annotations

import struct
import time
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.readers import RelayOfflineRecord
from app.routers import worker as worker_router
from app.services import relay_v2 as r
from tests.test_relay import WORKER_UUID, b64url_decode
from tests.test_worker_tickets import (
    override_current_user,
    seed_bucket,
    seed_worker_and_restaurant,
)

SECRET = bytes(range(32))
TICKET = UUID(WORKER_UUID)
READER_KEY = Ed25519PrivateKey.from_private_bytes(bytes([0x11] * 32))
EPH = X25519PrivateKey.from_private_bytes(bytes([0x22] * 32))
RNONCE = bytes(range(16))
IAT = 1800000000


def qr_bytes(qr: str) -> bytes:
    return b64url_decode(qr.split(".")[1])


def test_document_vectors_match(fixed_secret: bytes) -> None:
    """문서 §8의 바이트와 서버 구현이 같아야 한다(펌웨어가 같은 벡터로 맞춘다)."""
    keys = r.ServerKeys.from_secret(SECRET)
    assert keys.sign_pub.hex() == (
        "20d27176801f39e3ff3617d0531a8539eca617c3b0e1638873d5af3784595ba7"
    )
    assert keys.enc_pub.hex() == (
        "4f945e9aa340b05550dac55fb8c874a6e4cdbd633bebaaf88be7db40fa6d4476"
    )
    qr = qr_bytes(r.make_qr(keys, SECRET, TICKET, IAT, 60))
    assert qr.hex() == (
        "02012ab708e15dc74f54abc4b291f0a08dd1bc1999459330123f00d2496b3cd2496b"
        "66fa7f9a03b6f3478829004ad38c75d1dd2a30190d7f41c225bb119e0bcb532d36ae"
        "97e40cb5d7dddf1cccf396b4561b6f1cc41e33ef3b2ae4a04fcaaa174e03"
    )
    offline = (
        r.OfflineRecord(
            TICKET, IAT - 120, r.ticket_qn(SECRET, TICKET, IAT - 120), IAT - 100, 1
        ),
    )
    req, k_rsp = r.seal_request(
        keys.enc_pub,
        EPH,
        READER_KEY,
        reader_id=1,
        rnonce=RNONCE,
        rtc_time=IAT + 5,
        qr_signed=qr,
        offline=offline,
    )
    assert len(req) == 273
    assert req.hex().startswith(
        "02010faa684ed28867b97f4a6a2dee5df8ce974e76b7018e3f22a1c4cf2678570f20"
        "75183483bc37ea75"
    )
    assert req.hex().endswith(
        "9aef81d9a4c4ca282ee6c925a68575c5feef4293947d3672183e3b9796469dc9"
    )
    assert k_rsp.hex() == (
        "825ceee8d681d403f4c855ca841667306782cbee853d2a3db2076b4b5642e420"
    )
    opened = r.open_request(keys, req)
    assert r.reader_signature_ok(r.raw_public(READER_KEY.public_key()), opened)
    assert opened.offline == offline

    resp = r.seal_response(
        keys,
        k_rsp,
        r.Response(
            status=0,
            rnonce=RNONCE,
            reader_id=1,
            ticket_id=TICKET.bytes,
            qn=r.ticket_qn(SECRET, TICKET, IAT),
            server_time=IAT + 3,
            txid=7,
            meal_price=9000,
            ticket_applied=9000,
            ack_seq=1,
        ),
        nonce=bytes([0x33] * 12),
    )
    assert len(resp) == r.RESPONSE_LEN == 166
    assert resp.hex() == (
        "3333333333333333333333332204a97267523e288f97d0d4f47505438740a3f8b798"
        "c6bc7ea9d750d5262cd37dd0b4f6986ba39d8c0d5de5a3488e2915f470871514fae7"
        "136ad42c7fe7047c1059c82c8bb0f300807cfa6dea57344fc20712e85afa4cd165d6"
        "97b32498b322cb911f27ca806f17751b9a828ba4377a5a4e7ac08fb13701a2a0c5c4"
        "33ee66ef2822671e3888dc8aa12b0ef08d93eac5ca7ab1c5915d6d0ba177"
    )
    assert r.open_response(keys.sign_pub, k_rsp, resp).hex() == (
        "0200000102030405060708090a0b0c0d0e0f010000002ab708e15dc74f54abc4b291"
        "f0a08dd1bc1999459330123f07000000000000002823000028230000000000000"
        "3d2496b01000000"
    )


@pytest.fixture
def fixed_secret(monkeypatch: pytest.MonkeyPatch) -> bytes:
    monkeypatch.setattr(worker_router.Config, "RELAY_TOKEN_SECRET", SECRET)
    worker_router._relay_replies.clear()
    worker_router._relay_hits.clear()
    worker_router._relay_decisions.clear()
    return SECRET


async def seed_v2(
    async_client: AsyncClient, db_session: AsyncSession, test_app: FastAPI
) -> tuple[int, int]:
    """근로자·식당·버킷·공개키 등록된 리더를 만들고 (식당 id, reader id)."""
    worker, owner, restaurant = await seed_worker_and_restaurant(db_session)
    worker.user_id = WORKER_UUID
    await db_session.commit()
    await seed_bucket(db_session, worker.id, 10000, "2099-12-31")
    restaurant_id = restaurant.id
    await override_current_user(test_app, owner)
    created = await async_client.post(
        f"/restaurants/{restaurant_id}/readers",
        json={
            "bt_mac": "44:B1:76:17:36:68",
            "pubkey": r.raw_public(READER_KEY.public_key()).hex(),
        },
    )
    assert created.status_code == 201, created.text
    return restaurant_id, created.json()["data"]["id"]


def fresh_qr(iat: int | None = None) -> bytes:
    keys = r.ServerKeys.from_secret(SECRET)
    return qr_bytes(r.make_qr(keys, SECRET, TICKET, iat or int(time.time()), 60))


async def relay(
    async_client: AsyncClient,
    reader_id: int,
    qr: bytes,
    *,
    offline: tuple[r.OfflineRecord, ...] = (),
    reader_key: Ed25519PrivateKey = READER_KEY,
) -> tuple[int, bytes]:
    """리더처럼 요청을 만들어 보내고 (HTTP status, 검증된 응답 평문)."""
    keys = r.ServerKeys.from_secret(SECRET)
    req, k_rsp = r.seal_request(
        keys.enc_pub,
        X25519PrivateKey.generate(),
        reader_key,
        reader_id=reader_id,
        rnonce=RNONCE,
        rtc_time=int(time.time()),
        qr_signed=qr,
        offline=offline,
    )
    response = await async_client.post("/relay", json={"req": r.b64url(req)})
    if response.status_code != 200:
        return response.status_code, b""
    return 200, r.open_response(
        keys.sign_pub, k_rsp, b64url_decode(response.json()["resp"])
    )


def fields(plain: bytes) -> dict[str, int | bytes]:
    txid, price, _, _, server_time, ack = struct.unpack("<QIIIII", plain[46:74])
    return {
        "status": plain[1],
        "rnonce": plain[2:18],
        "reader_id": struct.unpack("<I", plain[18:22])[0],
        "ticket_id": plain[22:38],
        "txid": txid,
        "price": price,
        "server_time": server_time,
        "ack": ack,
    }


async def test_v2_charges_once_binds_reader_and_ticket(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
    fixed_secret: bytes,
) -> None:
    restaurant_id, reader_id = await seed_v2(async_client, db_session, test_app)

    status, plain = await relay(async_client, reader_id, fresh_qr())
    # 리더가 QR을 다시 찍은 재시도: 새 eph·새 QR이라도 같은 (식당, 근로자)는 한 번만 청구
    _, again = await relay(async_client, reader_id, fresh_qr())

    assert status == 200
    first = fields(plain)
    assert first["status"] == worker_router.RELAY_OK
    assert first["rnonce"] == RNONCE
    assert first["reader_id"] == reader_id
    assert first["ticket_id"] == TICKET.bytes
    assert first["price"] == 7000 and first["txid"] > 0
    assert abs(first["server_time"] - time.time()) < 5
    assert fields(again)["txid"] == first["txid"]

    history = await async_client.get(
        f"/restaurants/{restaurant_id}/ticket-usage-requests"
    )
    assert [h["meal_price"] for h in history.json()["data"]] == [7000]


async def test_v2_rejects_unknown_reader_bad_signature_and_expired_qr(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
    fixed_secret: bytes,
) -> None:
    _, reader_id = await seed_v2(async_client, db_session, test_app)
    impostor = Ed25519PrivateKey.from_private_bytes(bytes([0x33] * 32))

    _, unknown = await relay(async_client, reader_id + 99, fresh_qr())
    _, forged = await relay(async_client, reader_id, fresh_qr(), reader_key=impostor)
    _, expired = await relay(
        async_client, reader_id, fresh_qr(iat=int(time.time()) - 120)
    )
    bad_qr = bytearray(fresh_qr())
    bad_qr[-1] ^= 1
    _, invalid = await relay(async_client, reader_id, bytes(bad_qr))

    assert fields(unknown)["status"] == worker_router.RELAY_DEVICE_AUTH
    assert fields(forged)["status"] == worker_router.RELAY_DEVICE_AUTH
    assert fields(expired)["status"] == worker_router.RELAY_QR_EXPIRED
    assert fields(invalid)["status"] == worker_router.RELAY_INVALID_QR


async def test_v2_tampered_ciphertext_is_400(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
    fixed_secret: bytes,
) -> None:
    _, reader_id = await seed_v2(async_client, db_session, test_app)
    keys = r.ServerKeys.from_secret(SECRET)
    req, _ = r.seal_request(
        keys.enc_pub,
        EPH,
        READER_KEY,
        reader_id=reader_id,
        rnonce=RNONCE,
        rtc_time=0,
        qr_signed=fresh_qr(),
    )
    tampered = bytearray(req)
    tampered[60] ^= 1  # 폰이 암호문 한 비트를 바꿨다

    response = await async_client.post(
        "/relay", json={"req": r.b64url(bytes(tampered))}
    )
    too_long = await async_client.post(
        "/relay", json={"req": r.b64url(req + bytes(36 * 8))}
    )

    assert response.status_code == 400
    assert too_long.status_code == 400


async def test_v2_offline_records_settle_once_and_flag_duplicates(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
    fixed_secret: bytes,
) -> None:
    restaurant_id, reader_id = await seed_v2(async_client, db_session, test_app)
    iat = int(time.time()) - 3600
    qn = r.ticket_qn(SECRET, TICKET, iat)
    used = r.OfflineRecord(TICKET, iat, qn, iat + 10, 1)
    same_qr_again = r.OfflineRecord(TICKET, iat, qn, iat + 20, 2)
    forged = r.OfflineRecord(TICKET, iat, bytes(8), iat + 30, 3)

    _, plain = await relay(
        async_client, reader_id, fresh_qr(), offline=(used, same_qr_again, forged)
    )
    # ack를 못 받아 리더가 같은 기록을 다시 올려도 한 번만 처리된다.
    _, resent = await relay(async_client, reader_id, fresh_qr(), offline=(used,))

    assert fields(plain)["ack"] == 3
    assert fields(resent)["ack"] == 1
    # 버킷 10000에서 오프라인 7000이 먼저 정산돼 온라인 결제는 잔액 부족이다.
    assert fields(plain)["status"] == worker_router.RELAY_INSUFFICIENT
    rows = (
        await db_session.execute(
            select(RelayOfflineRecord.seq, RelayOfflineRecord.status).order_by(
                RelayOfflineRecord.seq
            )
        )
    ).all()
    assert [tuple(row) for row in rows] == [
        (1, "charged"),
        (2, "duplicate"),
        (3, "invalid"),
    ]
    history = await async_client.get(
        f"/restaurants/{restaurant_id}/ticket-usage-requests"
    )
    assert [h["meal_price"] for h in history.json()["data"]] == [7000]


async def test_relay_keys_and_worker_qr2(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
    fixed_secret: bytes,
) -> None:
    worker, _, _ = await seed_worker_and_restaurant(db_session)
    worker.user_id = WORKER_UUID
    await db_session.commit()
    await override_current_user(test_app, worker)

    keys = (await async_client.get("/relay/keys")).json()["data"]
    qr2 = (await async_client.get("/worker/qr")).json()["data"]["qr2"]

    server = r.ServerKeys.from_secret(SECRET)
    assert keys == {
        "kid": 1,
        "sign_pub": server.sign_pub.hex(),
        "enc_pub": server.enc_pub.hex(),
    }
    assert qr2.startswith("SK2.")
    ticket = r.read_qr(server, qr_bytes(qr2))
    assert ticket is not None and ticket.ticket_id == TICKET
    assert ticket.exp - ticket.iat == 60


async def test_v2_offline_record_after_online_approval_is_not_charged_again(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
    fixed_secret: bytes,
) -> None:
    """온라인 승인 응답이 유실돼 리더가 같은 QR(또는 재스캔한 새 QR)을 오프라인 승인해도 한 번만 청구."""
    restaurant_id, reader_id = await seed_v2(async_client, db_session, test_app)
    qr = fresh_qr()
    ticket = r.read_qr(r.ServerKeys.from_secret(SECRET), qr)
    assert ticket is not None
    _, online = await relay(async_client, reader_id, qr)
    worker_router._relay_decisions.clear()  # 90초 뒤 다른 결제 시점이라고 가정

    rescanned_iat = ticket.iat + 5
    same_qr = r.OfflineRecord(TICKET, ticket.iat, ticket.qn, ticket.iat + 2, 1)
    rescanned = r.OfflineRecord(
        TICKET,
        rescanned_iat,
        r.ticket_qn(SECRET, TICKET, rescanned_iat),
        rescanned_iat + 1,
        2,
    )
    old_iat = ticket.iat - 7200
    out_of_range = r.OfflineRecord(
        TICKET, old_iat, r.ticket_qn(SECRET, TICKET, old_iat), ticket.iat, 3
    )
    _, plain = await relay(
        async_client,
        reader_id,
        fresh_qr(iat=ticket.iat - 3600),  # 온라인 QR은 만료돼 청구 없음
        offline=(same_qr, rescanned, out_of_range),
    )

    assert fields(online)["status"] == worker_router.RELAY_OK
    assert fields(plain)["status"] == worker_router.RELAY_QR_EXPIRED
    rows = (
        await db_session.execute(
            select(RelayOfflineRecord.seq, RelayOfflineRecord.status).order_by(
                RelayOfflineRecord.seq
            )
        )
    ).all()
    assert [tuple(row) for row in rows] == [
        (1, "duplicate"),
        (2, "duplicate"),
        (3, "invalid"),
    ]
    history = await async_client.get(
        f"/restaurants/{restaurant_id}/ticket-usage-requests"
    )
    assert [h["meal_price"] for h in history.json()["data"]] == [7000]


async def test_v2_low_order_eph_and_fresh_response_nonce(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
    fixed_secret: bytes,
) -> None:
    """저차 점 eph는 500이 아니라 400. 같은 요청을 다시 처리해도 응답 nonce는 매번 다르다."""
    _, reader_id = await seed_v2(async_client, db_session, test_app)
    keys = r.ServerKeys.from_secret(SECRET)
    req, _ = r.seal_request(
        keys.enc_pub,
        EPH,
        READER_KEY,
        reader_id=reader_id,
        rnonce=RNONCE,
        rtc_time=0,
        qr_signed=fresh_qr(),
    )
    low_order = req[:2] + bytes(32) + req[34:]

    bad = await async_client.post("/relay", json={"req": r.b64url(low_order)})
    first = await async_client.post("/relay", json={"req": r.b64url(req)})
    worker_router._relay_replies.clear()  # 재전송 캐시(300초)가 지난 뒤라고 가정
    second = await async_client.post("/relay", json={"req": r.b64url(req)})

    assert bad.status_code == 400
    nonce_first = b64url_decode(first.json()["resp"])[:12]
    nonce_second = b64url_decode(second.json()["resp"])[:12]
    assert nonce_first != nonce_second
