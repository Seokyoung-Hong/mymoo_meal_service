r"""Restaurant BLE reader registration and signed config QR API.

리더(ESP32-C3) 설정을 바꾸는 원격 경로다. 사장이 웹에서 타입된 값을 넣으면 서버가 payload
문자열을 조립해 식당 스캐너 키로 서명한 QR 문자열을 돌려주고, 사장이 그 QR을 리더에 보여주면
리더가 검증 후 적용한다. BLE 설정 채널은 없고, 스캐너 키 자체는 어떤 경로로도 QR에 담기지 않는다.

서명 형식::

    K       = bytes.fromhex(restaurant.scanner_key_hash)
    KC      = HMAC-SHA256(K, b"SKcfg1")
    payload = "키: 값" 줄들 (ssid, password, mode, rid, t_adv, t_req, t_rsp 순, 준 필드만)
    body    = b"\x01" + struct.pack("<I", ctr) + dev(6바이트) + bytes([len(payload)]) + payload
    QR      = "SKC1." + b64url(body) + "." + b64url(HMAC-SHA256(KC, body)[:16])
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct
import time
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.models.readers import RestaurantReader
from app.models.restaurants import Restaurant
from app.models.user import User
from app.schemas.base import BaseSchema
from app.schemas.readers import (
    ReaderConfigCreate,
    ReaderConfigQrResponse,
    ReaderCreate,
    ReaderResponse,
    ReaderSetupCreate,
    ReaderSetupResponse,
    RelaySettingsResponse,
)
from app.services import relay_v2
from app.services.audit import AuditLogEntry, add_audit_log, request_id_from_request
from app.utils.db import check_admin_user, get_current_user, get_db
from app.utils.restaurants import get_restaurant_with_permission, relay_dedup_seconds


router = APIRouter(prefix="/restaurants", tags=["Reader"])

CONFIG_QR_PREFIX = "SKC1"
CONFIG_QR_VERSION = 1
CONFIG_KEY_INFO = b"SKcfg1"
CONFIG_MAC_LEN = 16
CONFIG_PAYLOAD_MAX_BYTES = 160
_BT_MAC_PATTERN = re.compile(r"[0-9a-f]{12}")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def normalize_bt_mac(value: str) -> str:
    """Normalize a reader MAC to 12 lowercase hex characters without separators.

    ``44:B1:76:17:36:68``, ``44-b1-76-17-36-68``, ``44b176173668`` 모두 받는다.

    Args:
        value (str): 사용자가 입력한 MAC 문자열.

    Returns:
        str: 구분자 없는 소문자 hex 12자.

    Raises:
        HTTPException: MAC 형식이 아니면 400.
    """
    compact = value.strip().replace(":", "").replace("-", "").lower()
    if not _BT_MAC_PATTERN.fullmatch(compact):
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="bt_mac은 hex 12자(구분자 :, - 허용) 형식이어야 합니다.",
        )
    return compact


def build_config_payload(settings: ReaderConfigCreate) -> str:
    """Assemble the signed payload text from typed settings only.

    준 필드만 선언 순서대로 ``키: 값`` 줄로 잇는다. 클라이언트가 보낸 자유 텍스트는 쓰지 않는다.

    Args:
        settings (ReaderConfigCreate): 검증된 설정 필드.

    Returns:
        str: 개행으로 이어붙인 payload 문자열.
    """
    return "\n".join(
        f"{name}: {value}"
        for name, value in settings.model_dump(exclude_none=True).items()
    )


def build_config_qr(scanner_key_hash: str, bt_mac: str, ctr: int, payload: str) -> str:
    """Sign a reader config payload into its QR string.

    Args:
        scanner_key_hash (str): 식당 스캐너 키 해시(hex). 서명 키 K다.
        bt_mac (str): 대상 리더 MAC(소문자 hex 12자).
        ctr (int): 재생 방지 카운터.
        payload (str): ``build_config_payload``가 만든 줄 단위 텍스트.

    Returns:
        str: ``SKC1.<body>.<mac16>`` 형식의 QR 문자열.
    """
    config_key = hmac.new(
        bytes.fromhex(scanner_key_hash), CONFIG_KEY_INFO, hashlib.sha256
    ).digest()
    raw = payload.encode("utf-8")
    body = (
        bytes([CONFIG_QR_VERSION])
        + struct.pack("<I", ctr)
        + bytes.fromhex(bt_mac)
        + bytes([len(raw)])
        + raw
    )
    mac16 = hmac.new(config_key, body, hashlib.sha256).digest()[:CONFIG_MAC_LEN]
    return f"{CONFIG_QR_PREFIX}.{_b64url(body)}.{_b64url(mac16)}"


def _reader_response(reader: RestaurantReader) -> ReaderResponse:
    return ReaderResponse(
        id=reader.id,
        bt_mac=reader.bt_mac,
        label=reader.label,
        pubkey=reader.pubkey,
        last_config_ctr=reader.last_config_ctr,
        last_config_at=reader.last_config_at,
        created_at=reader.created_at,
    )


async def _get_reader_or_404(
    db: AsyncSession, restaurant_id: int, bt_mac: str
) -> RestaurantReader:
    reader = await db.scalar(
        select(RestaurantReader).where(
            RestaurantReader.restaurant_id == restaurant_id,
            RestaurantReader.bt_mac == bt_mac,
        )
    )
    if reader is None:
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND,
            detail="등록된 리더가 아닙니다.",
        )
    return reader


async def _get_or_add_reader(
    db: AsyncSession, restaurant_id: int, bt_mac: str
) -> RestaurantReader:
    """이 식당의 리더를 찾거나 새로 추가한다. 다른 식당에 등록된 MAC이면 409."""
    reader = await db.scalar(
        select(RestaurantReader).where(RestaurantReader.bt_mac == bt_mac)
    )
    if reader is not None and reader.restaurant_id != restaurant_id:
        raise HTTPException(
            status_code=Config.HttpStatus.CONFLICT,
            detail="이미 다른 식당에 등록된 리더입니다.",
        )
    if reader is None:
        reader = RestaurantReader(restaurant_id=restaurant_id, bt_mac=bt_mac)
        db.add(reader)
    return reader


async def _set_pubkey(
    db: AsyncSession,
    request: Request,
    current_user: User,
    reader: RestaurantReader,
    pubkey: str | None,
) -> None:
    """공개키가 바뀌면 저장하고 감사 로그를 남긴다. 리더 공개키는 결제 권한 그 자체다."""
    if pubkey is None or pubkey == reader.pubkey:
        return
    reader.pubkey = pubkey
    await db.flush()
    _ = add_audit_log(
        db,
        AuditLogEntry(
            request_id=request_id_from_request(request),
            actor_user_id=current_user.user_id,
            action="restaurant_reader.pubkey.set",
            resource_type="restaurant_reader",
            resource_id=reader.id,
            after={"bt_mac": reader.bt_mac, "pubkey": pubkey},
        ),
    )


async def _ensure_rid_allowed(restaurant: Restaurant, current_user: User) -> None:
    """rid(식당 ID) 지정은 주인 또는 관리자만 할 수 있다. 매니저는 403."""
    if restaurant.owner == current_user.id:
        return
    if (await check_admin_user(current_user)).is_admin:
        return
    raise HTTPException(
        status_code=Config.HttpStatus.FORBIDDEN,
        detail="rid는 식당 주인 또는 관리자만 설정할 수 있습니다.",
    )


async def _apply_restaurant_settings(
    restaurant: Restaurant, settings: ReaderConfigCreate, current_user: User
) -> None:
    """식당 단위 설정을 검사하고 서버에도 반영한다.

    rid는 주인·관리자만 쓴다. dedup(재결제 차단 창)은 서버 결제 판정에도 쓰이므로 QR을
    발급하는 순간 식당에 저장한다(리더는 QR을 찍어야 바뀐다).
    """
    if settings.rid is not None:
        await _ensure_rid_allowed(restaurant, current_user)
    if settings.dedup is not None:
        restaurant.relay_dedup_seconds = settings.dedup


@router.get("/{restaurant_id}/relay-settings")
async def get_relay_settings(
    restaurant_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[RelaySettingsResponse]:
    """식당의 결제 중계 설정(지금은 재결제 차단 창)을 돌려준다."""
    restaurant = await get_restaurant_with_permission(restaurant_id, db, current_user)
    return BaseSchema[RelaySettingsResponse](
        data=RelaySettingsResponse(dedup=relay_dedup_seconds(restaurant))
    )


@router.get("/{restaurant_id}/readers")
async def list_readers(
    restaurant_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[list[ReaderResponse]]:
    """List the readers registered to a restaurant."""
    _ = await get_restaurant_with_permission(restaurant_id, db, current_user)
    result = await db.execute(
        select(RestaurantReader)
        .where(RestaurantReader.restaurant_id == restaurant_id)
        .order_by(RestaurantReader.id)
    )
    return BaseSchema[list[ReaderResponse]](
        data=[_reader_response(reader) for reader in result.scalars().all()]
    )


@router.post("/{restaurant_id}/readers", status_code=Config.HttpStatus.CREATED)
async def register_reader(
    restaurant_id: int,
    payload: ReaderCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[ReaderResponse]:
    """Register a reader, or relabel it (and set its v2 pubkey) if already registered."""
    _ = await get_restaurant_with_permission(restaurant_id, db, current_user)
    bt_mac = normalize_bt_mac(payload.bt_mac)
    reader = await _get_or_add_reader(db, restaurant_id, bt_mac)
    reader.label = payload.label
    pubkey = payload.pubkey.lower() if payload.pubkey is not None else None

    try:
        await _set_pubkey(db, request, current_user, reader, pubkey)
        await db.commit()
        await db.refresh(reader)
    except Exception:
        await db.rollback()
        raise
    return BaseSchema[ReaderResponse](data=_reader_response(reader))


@router.post("/{restaurant_id}/readers/setup")
async def setup_reader(
    restaurant_id: int,
    payload: ReaderSetupCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[ReaderSetupResponse]:
    """Register a reader from its OLED registration QR and issue a signed setup QR.

    리더가 화면에 띄운 ``SK2R:``(MAC·공개키)를 폰이 찍어 보내면, 리더를 등록(또는 공개키
    갱신)하고 운영 서명키로 서명한 ``SK2S.`` 설치 QR을 돌려준다. 리더는 이 QR을 찍어
    reader_id와 (선택) Wi-Fi 설정을 적용한다. kiosk_c6 SETUP_QR.md, docs/ble-relay-v2.md §2.
    """
    _ = await get_restaurant_with_permission(restaurant_id, db, current_user)
    try:
        bt_mac, reader_pub = relay_v2.parse_reg_code(payload.reg)
    except relay_v2.RelayV2Error as exc:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST, detail=str(exc)
        ) from exc
    setup_payload = (
        f"ssid: {payload.wifi.ssid}\npassword: {payload.wifi.password}".encode()
        if payload.wifi is not None
        else b""
    )
    if len(setup_payload) > relay_v2.SETUP_PAYLOAD_MAX:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="Wi-Fi 설정이 너무 깁니다.",
        )

    reader = await _get_or_add_reader(db, restaurant_id, bt_mac)
    if payload.label is not None:
        reader.label = payload.label
    reader.setup_ctr = (reader.setup_ctr or 0) + 1  # 새 행은 flush 전까지 None
    try:
        await _set_pubkey(db, request, current_user, reader, reader_pub.hex())
        await db.flush()
        setup_qr = relay_v2.make_setup_qr(
            relay_v2.ServerKeys.from_secret(Config.RELAY_TOKEN_SECRET),
            bt_mac,
            reader_pub,
            reader.id,
            reader.setup_ctr,
            setup_payload,
        )
        _ = add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=current_user.user_id,
                action="restaurant_reader.setup_qr.issue",
                resource_type="restaurant_reader",
                resource_id=reader.id,
                # Wi-Fi 비밀번호는 남기지 않는다.
                after={
                    "bt_mac": bt_mac,
                    "ctr": reader.setup_ctr,
                    "wifi_ssid": payload.wifi.ssid if payload.wifi else None,
                },
            ),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return BaseSchema[ReaderSetupResponse](
        data=ReaderSetupResponse(reader_id=reader.id, bt_mac=bt_mac, setup_qr=setup_qr)
    )


@router.post("/{restaurant_id}/readers/{bt_mac}/settings-qr")
async def issue_reader_settings_qr(  # noqa: PLR0913
    restaurant_id: int,
    bt_mac: str,
    settings: ReaderConfigCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[ReaderSetupResponse]:
    """Issue a server-signed ``SK2S.`` QR that changes an installed v2 reader's settings.

    ``config-qr``(SKC1)은 식당 스캐너 키가 들어간 v1 리더용이다. QR로 설치한 v2 리더는
    스캐너 키가 없으므로, 등록된 공개키로 설치 QR을 다시 만들고 설정을 payload에 싣는다.
    리더는 서명·MAC·공개키·ctr을 확인한 뒤 payload를 평문 설정 문법 그대로 적용한다.
    """
    restaurant = await get_restaurant_with_permission(restaurant_id, db, current_user)
    reader = await _get_reader_or_404(db, restaurant_id, normalize_bt_mac(bt_mac))
    if reader.pubkey is None:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="아직 설치되지 않은 리더입니다. 먼저 리더기 설치를 해 주세요.",
        )
    fields = list(settings.model_dump(exclude_none=True))
    if not fields:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="설정할 필드를 최소 하나는 보내야 합니다.",
        )
    await _apply_restaurant_settings(restaurant, settings, current_user)
    payload = build_config_payload(settings).encode("utf-8")
    if len(payload) > relay_v2.SETUP_PAYLOAD_MAX:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail=f"설정은 {relay_v2.SETUP_PAYLOAD_MAX}바이트를 넘을 수 없습니다.",
        )

    reader.setup_ctr += 1
    try:
        setup_qr = relay_v2.make_setup_qr(
            relay_v2.ServerKeys.from_secret(Config.RELAY_TOKEN_SECRET),
            reader.bt_mac,
            bytes.fromhex(reader.pubkey),
            reader.id,
            reader.setup_ctr,
            payload,
        )
        _ = add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=current_user.user_id,
                action="restaurant_reader.settings_qr.issue",
                resource_type="restaurant_reader",
                resource_id=reader.id,
                # 값은 남기지 않는다(password).
                after={
                    "bt_mac": reader.bt_mac,
                    "ctr": reader.setup_ctr,
                    "fields": fields,
                },
            ),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return BaseSchema[ReaderSetupResponse](
        data=ReaderSetupResponse(
            reader_id=reader.id, bt_mac=reader.bt_mac, setup_qr=setup_qr
        )
    )


@router.delete(
    "/{restaurant_id}/readers/{bt_mac}",
    status_code=Config.HttpStatus.NO_CONTENT,
    response_model=None,
)
async def delete_reader(
    restaurant_id: int,
    bt_mac: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Remove a reader registration from a restaurant."""
    _ = await get_restaurant_with_permission(restaurant_id, db, current_user)
    reader = await _get_reader_or_404(db, restaurant_id, normalize_bt_mac(bt_mac))
    try:
        await db.delete(reader)
        await db.commit()
    except Exception:
        await db.rollback()
        raise


@router.post("/{restaurant_id}/readers/{bt_mac}/config-qr")
async def issue_reader_config_qr(  # noqa: PLR0913
    restaurant_id: int,
    bt_mac: str,
    settings: ReaderConfigCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[ReaderConfigQrResponse]:
    """Issue a signed config QR for one registered reader of this restaurant."""
    restaurant = await get_restaurant_with_permission(restaurant_id, db, current_user)
    reader = await _get_reader_or_404(db, restaurant_id, normalize_bt_mac(bt_mac))

    fields = list(settings.model_dump(exclude_none=True))
    if not fields:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="설정할 필드를 최소 하나는 보내야 합니다.",
        )
    await _apply_restaurant_settings(restaurant, settings, current_user)
    if restaurant.scanner_key_hash is None:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="식당 스캐너 키가 아직 발급되지 않았습니다.",
        )

    payload = build_config_payload(settings)
    if len(payload.encode("utf-8")) > CONFIG_PAYLOAD_MAX_BYTES:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail=f"payload는 {CONFIG_PAYLOAD_MAX_BYTES}바이트를 넘을 수 없습니다.",
        )

    # 리더는 저장값보다 큰 ctr만 받는다. 같은 초에 두 번 발급해도 반드시 커지게 한다.
    ctr = max(int(time.time()), reader.last_config_ctr + 1)
    reader.last_config_ctr = ctr
    reader.last_config_at = datetime.now(timezone.utc)

    try:
        _ = add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=current_user.user_id,
                action="restaurant_reader.config_qr.issue",
                resource_type="restaurant_reader",
                resource_id=reader.id,
                # 값은 남기지 않는다. password가 로그로 새어 나가면 안 된다.
                after={"bt_mac": reader.bt_mac, "ctr": ctr, "fields": fields},
            ),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return BaseSchema[ReaderConfigQrResponse](
        data=ReaderConfigQrResponse(
            qr=build_config_qr(
                restaurant.scanner_key_hash, reader.bt_mac, ctr, payload
            ),
            ctr=ctr,
            payload=payload,
        )
    )
