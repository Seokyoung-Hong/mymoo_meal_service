"""Worker meal allowance wallet, cash wallet, and restaurant scan API.

결제 모델은 잔액 차감형 지갑이다. 관리자(고객사 콘솔)가 발급한 식대는 만료일이 있는
버킷(``MealTicket``)으로 쌓이고, 식당에서 결제할 때 만료일이 빠른 버킷부터 식사 금액을
차감한다. 부족분은 근로자의 현금성 캐시에서 결제된다.

QR 기기 규칙 (``GET /scan``):
- 근로자 QR에는 ``{PUBLIC_BASE_URL}/scan?worker=<worker_user_id>`` URL 전체가 담긴다
  (``GET /worker/qr``가 이 URL을 돌려준다).
- 기기는 그 URL을 그대로 GET 하되 고정 헤더 ``X-Scanner-Key: <식당 스캐너 키>``를 붙인다.
  키는 식당 주인/매니저가 ``POST /restaurants/{id}/scanner-key``로 발급받는다.
- 필요하면 기기가 고정 쿼리 ``&meal_type=lunch`` 를 덧붙여 끼니를 지정할 수 있다.
- 금액은 식당 가격 정책으로 정해진다. 정책이 없으면 400.

BLE 중계 규칙 (``POST /relay``, docs/ble-relay.md):
- 리더가 87바이트 요청을 스캐너 키로 서명하고, 근로자 폰(또는 리더 자신)이 그대로 중계한다.
- 서버는 같은 키로 검증·결제하고 62바이트 응답에 서명한다. 폰은 바이트를 옮길 뿐이다.
- 결과를 못 받아 QR을 다시 찍으면 87바이트가 통째로 달라진다. 그래서 (식당, 근로자)
  단위로도 최근 결정을 재사용해 한 끼에 두 번 청구되는 것을 막는다.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import struct
import time
from datetime import date, datetime, timezone
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.config import Config, logger
from app.models.readers import RelayOfflineRecord, RestaurantReader
from app.models.restaurants import Restaurant
from app.models.user import User
from app.models.worker import (
    CashTransaction,
    CashWallet,
    MealTicket,
    MealTicketUsageRequest,
)
from app.schemas.base import BaseSchema
from app.schemas.meals import MealType as MealTypeSchema
from app.schemas.users import AdminUserSchema
from app.schemas.worker import (
    AllowanceBalanceResponse,
    AllowanceTicketResponse,
    CashBalanceResponse,
    CashTransactionResponse,
    MealAllowanceCreate,
    MealTicketResponse,
    DeviceKeyCreate,
    MockCardChargeCreate,
    OfflineTokensResponse,
    RelayKeysResponse,
    RelayRequest,
    RelayResponse,
    RestaurantRevenueResponse,
    RevenueRow,
    ScannerKeyResponse,
    TicketScanCreate,
    TicketUsageRequestResponse,
    TicketUsageRequestStatus,
    WorkerQrResponse,
)
from app.services import relay_v2
from app.services.audit import AuditLogEntry, add_audit_log, request_id_from_request
from app.services.pricing import resolve_price
from app.utils.auth import optional_metrics_x_user_id
from app.utils.events import publish, sse_response
from app.utils.db import get_admin_user, get_current_user, get_db
from app.utils.restaurants import get_restaurant_with_permission, relay_dedup_seconds


router = APIRouter(tags=["Worker"])

SCANNER_KEY_HEADER = "X-Scanner-Key"


def _today() -> date:
    return datetime.now(Config.TZ).date()


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


async def _get_or_create_wallet(db: AsyncSession, user_id: int) -> CashWallet:
    wallet = await db.scalar(select(CashWallet).where(CashWallet.user_id == user_id))
    if wallet is not None:
        return wallet
    wallet = CashWallet(user_id=user_id, balance=0)
    db.add(wallet)
    return wallet


def _ticket_response(ticket: MealTicket) -> MealTicketResponse:
    return MealTicketResponse(
        id=ticket.id,
        code=ticket.code,
        amount=ticket.amount,
        remaining_amount=ticket.remaining_amount,
        expires_on=ticket.expires_on,
        status=ticket.status,  # type: ignore[arg-type]
        registered_at=ticket.registered_at,
        used_at=ticket.used_at,
    )


def _cash_transaction_response(
    transaction: CashTransaction,
) -> CashTransactionResponse:
    return CashTransactionResponse(
        id=transaction.id,
        amount=transaction.amount,
        transaction_type=transaction.transaction_type,  # type: ignore[arg-type]
        status=transaction.status,  # type: ignore[arg-type]
        description=transaction.description,
        usage_request_id=transaction.usage_request_id,
        created_at=transaction.created_at,
    )


def _usage_request_response(
    usage_request: MealTicketUsageRequest,
) -> TicketUsageRequestResponse:
    ticket = usage_request.ticket
    worker = usage_request.worker
    restaurant = usage_request.restaurant
    approver = usage_request.approver
    return TicketUsageRequestResponse(
        id=usage_request.id,
        ticket_id=usage_request.ticket_id,
        ticket_code=ticket.code if ticket is not None else None,
        worker_user_id=worker.user_id,
        restaurant_id=usage_request.restaurant_id,
        restaurant_name=restaurant.name,
        meal_type=(
            MealTypeSchema(usage_request.meal_type)
            if usage_request.meal_type is not None
            else None
        ),
        served_date=usage_request.served_date,
        meal_price=usage_request.meal_price,
        ticket_amount_applied=usage_request.ticket_amount_applied,
        cash_amount_required=usage_request.cash_amount_required,
        status=usage_request.status,  # type: ignore[arg-type]
        requested_at=usage_request.requested_at,
        approved_at=usage_request.approved_at,
        approved_by_user_id=approver.user_id if approver is not None else None,
    )


async def _load_usage_request_or_404(
    db: AsyncSession,
    request_id: int,
) -> MealTicketUsageRequest:
    usage_request = await db.scalar(
        select(MealTicketUsageRequest)
        .where(MealTicketUsageRequest.id == request_id)
        .options(
            joinedload(MealTicketUsageRequest.ticket),
            joinedload(MealTicketUsageRequest.worker),
            joinedload(MealTicketUsageRequest.restaurant),
            joinedload(MealTicketUsageRequest.approver),
        )
    )
    if usage_request is None:
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND,
            detail="식권 사용 요청을 찾을 수 없습니다.",
        )
    return usage_request


# ---------------------------------------------------------------------------
# 지갑 차감 공통 로직
# ---------------------------------------------------------------------------


async def _available_buckets(
    db: AsyncSession, owner_id: int, today: date
) -> list[MealTicket]:
    """잔액이 남은 미만료 버킷을 만료일 빠른 순으로 돌려주고, 지난 버킷은 expired로 바꾼다."""
    result = await db.execute(
        select(MealTicket)
        .where(MealTicket.owner_id == owner_id, MealTicket.status == "available")
        .order_by(MealTicket.expires_on, MealTicket.id)
    )
    buckets: list[MealTicket] = []
    for ticket in result.scalars():
        if ticket.expires_on < today:
            ticket.status = "expired"
        elif ticket.remaining_amount > 0:
            buckets.append(ticket)
    return buckets


def _deduct(
    buckets: list[MealTicket], amount: int, used_at: datetime
) -> tuple[int, MealTicket | None]:
    """버킷 순서대로 amount를 차감한다. (적용된 금액, 첫 차감 버킷)을 돌려준다."""
    applied = 0
    first: MealTicket | None = None
    for bucket in buckets:
        if applied >= amount:
            break
        take = min(bucket.remaining_amount, amount - applied)
        bucket.remaining_amount -= take
        applied += take
        first = first or bucket
        if bucket.remaining_amount == 0:
            bucket.status = "used"
            bucket.used_at = used_at
    return applied, first


async def _meal_price_for(
    db: AsyncSession,
    restaurant_id: int,
    meal_type: MealTypeSchema | None,
    served_date: date,
    explicit_price: int | None,
) -> int:
    if explicit_price is not None:
        return explicit_price
    resolved = await resolve_price(
        db, restaurant_id=restaurant_id, meal_type=meal_type, served_date=served_date
    )
    if resolved.price is not None:
        return resolved.price
    raise HTTPException(
        status_code=Config.HttpStatus.BAD_REQUEST,
        detail="가격 정책이 없어 meal_price를 입력해야 합니다.",
    )


async def _charge_wallet(  # noqa: PLR0913
    db: AsyncSession,
    request: Request,
    *,
    restaurant: Restaurant,
    worker: User,
    approver_id: int,
    actor_user_id: str,
    action: str,
    meal_type: MealTypeSchema | None,
    served_date: date | None,
    meal_price: int | None,
) -> TicketUsageRequestResponse:
    """근로자 지갑에서 식사 금액을 차감하고 사용 내역을 돌려준다. 커밋 후 SSE로 알린다."""
    today = _today()
    served_on = served_date or today
    price = await _meal_price_for(db, restaurant.id, meal_type, served_on, meal_price)

    buckets = await _available_buckets(db, worker.id, today)
    allowance_before = sum(bucket.remaining_amount for bucket in buckets)
    cash_required = max(price - allowance_before, 0)
    wallet = await _get_or_create_wallet(db, worker.id)
    if wallet.balance < cash_required:
        raise HTTPException(
            status_code=Config.HttpStatus.CONFLICT,
            detail="부족분을 결제할 캐시 잔액이 부족합니다.",
        )

    now = datetime.now(timezone.utc)
    cash_before = wallet.balance
    applied, first_bucket = _deduct(buckets, price, now)
    usage_request = MealTicketUsageRequest(
        ticket_id=first_bucket.id if first_bucket is not None else None,
        worker_id=worker.id,
        restaurant_id=restaurant.id,
        meal_type=meal_type.value if meal_type else None,
        served_date=served_on,
        meal_price=price,
        ticket_amount_applied=applied,
        cash_amount_required=cash_required,
        status="used",
        approved_at=now,
        approved_by=approver_id,
    )
    try:
        db.add(usage_request)
        await db.flush()
        usage_request_id = usage_request.id
        transaction: CashTransaction | None = None
        if cash_required > 0:
            wallet.balance -= cash_required
            transaction = CashTransaction(
                user_id=worker.id,
                usage_request_id=usage_request_id,
                amount=-cash_required,
                transaction_type="ticket_shortfall_payment",
                description=f"ticket request #{usage_request_id} shortfall",
            )
            db.add(transaction)
            await db.flush()
        add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=actor_user_id,
                action=action,
                resource_type="meal_ticket_usage_request",
                resource_id=usage_request_id,
                before={
                    "allowance_balance": allowance_before,
                    "cash_balance": cash_before,
                },
                after={
                    "allowance_balance": allowance_before - applied,
                    "cash_balance": wallet.balance,
                    "ticket_amount_applied": applied,
                    "cash_amount_required": cash_required,
                    "cash_transaction_id": transaction.id if transaction else None,
                },
            ),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    response = _usage_request_response(
        await _load_usage_request_or_404(db, usage_request_id)
    )
    payment = {"usage_request": response.model_dump(mode="json")}
    publish(f"restaurant:{restaurant.id}", "payment", payment)
    publish(
        f"worker:{worker.id}",
        "payment",
        {
            **payment,
            "allowance_balance": allowance_before - applied,
            "cash_balance": wallet.balance,
        },
    )
    return response


# ---------------------------------------------------------------------------
# 근로자: 식대 지갑
# ---------------------------------------------------------------------------


@router.get("/worker/tickets")
async def list_meal_tickets(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[list[MealTicketResponse]]:
    """List allowance buckets owned by the current worker."""
    result = await db.execute(
        select(MealTicket)
        .where(MealTicket.owner_id == current_user.id)
        .order_by(MealTicket.registered_at.desc(), MealTicket.id.desc())
    )
    return BaseSchema[list[MealTicketResponse]](
        data=[_ticket_response(ticket) for ticket in result.scalars().all()]
    )


@router.get("/worker/allowance/balance")
async def get_allowance_balance(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[AllowanceBalanceResponse]:
    """Return the sum of the worker's unexpired allowance bucket balances."""
    buckets = await _available_buckets(db, current_user.id, _today())
    balance = sum(bucket.remaining_amount for bucket in buckets)
    await db.commit()
    return BaseSchema[AllowanceBalanceResponse](
        data=AllowanceBalanceResponse(balance=balance)
    )


@router.get("/worker/events", response_class=StreamingResponse)
async def worker_events(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> StreamingResponse:
    """SSE: 내 지갑 변동 (``payment`` / ``cash_charged`` / ``allowance_issued``)."""
    await db.close()  # 스트림이 열려 있는 동안 DB 커넥션을 붙들지 않는다.
    return sse_response(f"worker:{current_user.id}")


@router.get("/worker/qr")
async def get_worker_qr(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[WorkerQrResponse]:
    """Return the scan URL (legacy readers) and the signed BLE ticket token.

    앱은 ``ttl``초마다 다시 호출해 QR을 새로 그린다. BLE QR 문자열은
    ``SK1.<sid base64url>.<tok>`` 이며 ``sid``는 앱이 만드는 8바이트 난수다.
    """
    base = Config.PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    url = f"{base}/scan?{urlencode({'worker': current_user.user_id})}"
    return BaseSchema[WorkerQrResponse](
        data=WorkerQrResponse(
            url=url,
            tok=make_ticket_token(current_user.user_id),
            qr2=_make_qr2(current_user.user_id),
            ttl=TICKET_TOKEN_TTL_SECONDS,
        )
    )


@router.get("/worker/offline-tokens")
async def get_worker_offline_tokens(
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[OfflineTokensResponse]:
    """Return one-time offline tokens the app caches for when the phone has no network.

    같은 창(``OFFLINE_TOKEN_TTL_SECONDS``) 안에서는 몇 번 불러도 같은 토큰이다. 토큰은 리더가
    오프라인으로만 승인하고, 나중에 올라온 기록에서 토큰당 한 번만 청구된다.
    """
    try:
        ticket_id = UUID(current_user.user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="UUID가 아닌 레거시 계정은 오프라인 토큰을 받을 수 없습니다.",
        ) from exc
    if current_user.device_pubkey is None:
        raise HTTPException(
            status_code=Config.HttpStatus.CONFLICT,
            detail="기기 키가 등록되지 않았습니다. POST /worker/device-key 먼저.",
        )
    now = int(time.time())
    ttl = Config.OFFLINE_TOKEN_TTL_SECONDS
    return BaseSchema[OfflineTokensResponse](
        data=OfflineTokensResponse(
            tokens=relay_v2.make_offline_tokens(
                _v2_keys(),
                Config.RELAY_TOKEN_SECRET,
                ticket_id,
                now,
                ttl,
                Config.OFFLINE_TOKENS_PER_WINDOW,
                bytes.fromhex(current_user.device_pubkey),
            ),
            exp=now - now % ttl + ttl,
        )
    )


@router.post(
    "/worker/device-key",
    status_code=Config.HttpStatus.NO_CONTENT,
    response_model=None,
)
async def register_worker_device_key(
    payload: DeviceKeyCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Register (or replace) the phone key that offline tokens are bound to.

    교체하면 이전 키로 받은 토큰은 리더의 기기 서명 확인을 통과하지 못한다(폰 분실 시 교체).
    """
    pubkey = payload.pubkey.lower()
    if pubkey == current_user.device_pubkey:
        return
    previous = current_user.device_pubkey
    current_user.device_pubkey = pubkey
    try:
        _ = add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=current_user.user_id,
                action="worker.device_key.set",
                resource_type="User",
                resource_id=current_user.id,
                before={"device_pubkey": previous},
                after={"device_pubkey": pubkey},
            ),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise


# ---------------------------------------------------------------------------
# 관리자: 식대 발급
# ---------------------------------------------------------------------------


def _allowance_response(
    ticket: MealTicket,
    worker_user_id: str,
) -> AllowanceTicketResponse:
    return AllowanceTicketResponse(
        **_ticket_response(ticket).model_dump(),
        worker_user_id=worker_user_id,
    )


@router.post("/admin/meal-allowances", status_code=Config.HttpStatus.CREATED)
async def issue_meal_allowances(
    payload: MealAllowanceCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin_user: Annotated[AdminUserSchema, Depends(get_admin_user)],
) -> BaseSchema[list[AllowanceTicketResponse]]:
    """Issue a meal allowance bucket to each of the given workers (admin only)."""
    if payload.expires_on < _today():
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="만료일이 지난 식대는 제공할 수 없습니다.",
        )
    result = await db.execute(
        select(User).where(User.user_id.in_(payload.worker_user_ids))
    )
    workers = {user.user_id: user for user in result.scalars()}
    missing = [uid for uid in payload.worker_user_ids if uid not in workers]
    if missing:
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND,
            detail="존재하지 않는 사용자입니다: " + ", ".join(missing),
        )

    issued: list[tuple[MealTicket, str]] = []
    try:
        for worker_user_id in payload.worker_user_ids:
            ticket = MealTicket(
                code=uuid4().hex,
                owner_id=workers[worker_user_id].id,
                amount=payload.amount,
                remaining_amount=payload.amount,
                expires_on=payload.expires_on,
            )
            db.add(ticket)
            issued.append((ticket, worker_user_id))
        await db.flush()
        for ticket, worker_user_id in issued:
            add_audit_log(
                db,
                AuditLogEntry(
                    request_id=request_id_from_request(request),
                    actor_user_id=admin_user.user_id,
                    action="meal_allowance.issue",
                    resource_type="meal_ticket",
                    resource_id=ticket.id,
                    after={
                        "code": ticket.code,
                        "worker_user_id": worker_user_id,
                        "amount": ticket.amount,
                        "expires_on": ticket.expires_on.isoformat(),
                        "status": ticket.status,
                    },
                ),
            )
        # 커밋 후 만료된 인스턴스 접근을 피하려고 응답을 먼저 만든다.
        data = [
            _allowance_response(ticket, worker_user_id)
            for ticket, worker_user_id in issued
        ]
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    for ticket, worker_user_id in issued:
        publish(
            f"worker:{ticket.owner_id}",
            "allowance_issued",
            {
                "ticket": _allowance_response(ticket, worker_user_id).model_dump(
                    mode="json"
                )
            },
        )
    return BaseSchema[list[AllowanceTicketResponse]](data=data)


@router.get("/admin/meal-allowances")
async def list_meal_allowances(
    db: Annotated[AsyncSession, Depends(get_db)],
    admin_user: Annotated[AdminUserSchema, Depends(get_admin_user)],
    worker_user_id: Annotated[str | None, Query()] = None,
) -> BaseSchema[list[AllowanceTicketResponse]]:
    """List all allowance buckets with their owners for admin overview."""
    _ = admin_user
    stmt = (
        select(MealTicket, User.user_id)
        .join(User, MealTicket.owner_id == User.id)
        .order_by(MealTicket.registered_at.desc(), MealTicket.id.desc())
    )
    if worker_user_id is not None:
        stmt = stmt.where(User.user_id == worker_user_id)
    result = await db.execute(stmt)
    return BaseSchema[list[AllowanceTicketResponse]](
        data=[
            _allowance_response(ticket, owner_user_id)
            for ticket, owner_user_id in result.all()
        ]
    )


# ---------------------------------------------------------------------------
# 근로자: 현금성 캐시
# ---------------------------------------------------------------------------


@router.get("/worker/cash/balance")
async def get_cash_balance(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[CashBalanceResponse]:
    """Return the current cash-like wallet balance."""
    wallet = await _get_or_create_wallet(db, current_user.id)
    await db.commit()
    return BaseSchema[CashBalanceResponse](
        data=CashBalanceResponse(balance=wallet.balance)
    )


@router.post("/worker/cash/card-charges", status_code=Config.HttpStatus.CREATED)
async def mock_card_charge(
    payload: MockCardChargeCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[CashTransactionResponse]:
    """Mock a card charge and credit the worker cash wallet."""
    wallet = await _get_or_create_wallet(db, current_user.id)
    wallet.balance += payload.amount
    description = "mock card charge"
    if payload.card_last4 is not None:
        description = f"mock card charge ****{payload.card_last4}"
    transaction = CashTransaction(
        user_id=current_user.id,
        amount=payload.amount,
        transaction_type="mock_card_charge",
        description=description,
    )
    try:
        db.add(transaction)
        await db.flush()
        add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=current_user.user_id,
                action="cash.mock_card_charge",
                resource_type="cash_transaction",
                resource_id=transaction.id,
                after={
                    "amount": payload.amount,
                    "balance": wallet.balance,
                    "transaction_type": transaction.transaction_type,
                },
            ),
        )
        await db.commit()
        await db.refresh(transaction)
    except Exception:
        await db.rollback()
        raise

    data = _cash_transaction_response(transaction)
    publish(
        f"worker:{current_user.id}",
        "cash_charged",
        {"cash_balance": wallet.balance, "transaction": data.model_dump(mode="json")},
    )
    return BaseSchema[CashTransactionResponse](data=data)


@router.get("/worker/cash/transactions")
async def list_cash_transactions(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[list[CashTransactionResponse]]:
    """List the current worker's cash transactions."""
    result = await db.execute(
        select(CashTransaction)
        .where(CashTransaction.user_id == current_user.id)
        .order_by(CashTransaction.created_at.desc(), CashTransaction.id.desc())
    )
    return BaseSchema[list[CashTransactionResponse]](
        data=[
            _cash_transaction_response(transaction)
            for transaction in result.scalars().all()
        ]
    )


# ---------------------------------------------------------------------------
# 사용 내역 조회
# ---------------------------------------------------------------------------


@router.get("/worker/ticket-usage-requests")
async def list_worker_ticket_usage_requests(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[list[TicketUsageRequestResponse]]:
    """List meal payments made from the current worker's wallet."""
    result = await db.execute(
        select(MealTicketUsageRequest)
        .where(MealTicketUsageRequest.worker_id == current_user.id)
        .options(
            joinedload(MealTicketUsageRequest.ticket),
            joinedload(MealTicketUsageRequest.worker),
            joinedload(MealTicketUsageRequest.restaurant),
            joinedload(MealTicketUsageRequest.approver),
        )
        .order_by(
            MealTicketUsageRequest.requested_at.desc(),
            MealTicketUsageRequest.id.desc(),
        )
    )
    return BaseSchema[list[TicketUsageRequestResponse]](
        data=[
            _usage_request_response(usage_request)
            for usage_request in result.scalars().all()
        ]
    )


@router.get("/restaurants/{restaurant_id}/ticket-usage-requests")
async def list_restaurant_ticket_usage_requests(  # noqa: PLR0913
    restaurant_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    status: Annotated[TicketUsageRequestStatus | None, Query()] = None,
    date_from: Annotated[date | None, Query(description="제공일 시작(포함)")] = None,
    date_to: Annotated[date | None, Query(description="제공일 끝(포함)")] = None,
) -> BaseSchema[list[TicketUsageRequestResponse]]:
    """List meal payments received by a restaurant (owner, manager, or admin)."""
    _ = await get_restaurant_with_permission(restaurant_id, db, current_user)
    stmt = (
        select(MealTicketUsageRequest)
        .where(MealTicketUsageRequest.restaurant_id == restaurant_id)
        .options(
            joinedload(MealTicketUsageRequest.ticket),
            joinedload(MealTicketUsageRequest.worker),
            joinedload(MealTicketUsageRequest.restaurant),
            joinedload(MealTicketUsageRequest.approver),
        )
        .order_by(
            MealTicketUsageRequest.requested_at.desc(),
            MealTicketUsageRequest.id.desc(),
        )
    )
    if status is not None:
        stmt = stmt.where(MealTicketUsageRequest.status == status)
    if date_from is not None:
        stmt = stmt.where(MealTicketUsageRequest.served_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(MealTicketUsageRequest.served_date <= date_to)
    result = await db.execute(stmt)
    return BaseSchema[list[TicketUsageRequestResponse]](
        data=[
            _usage_request_response(usage_request)
            for usage_request in result.scalars().all()
        ]
    )


@router.get("/restaurants/{restaurant_id}/events", response_class=StreamingResponse)
async def restaurant_events(
    restaurant_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> StreamingResponse:
    """SSE: 이 식당의 결제 발생 (``payment``). 주인·매니저·관리자."""
    _ = await get_restaurant_with_permission(restaurant_id, db, current_user)
    await db.close()
    return sse_response(f"restaurant:{restaurant_id}")


@router.get("/restaurants/{restaurant_id}/revenue")
async def get_restaurant_revenue(
    restaurant_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    date_from: Annotated[
        date | None, Query(description="제공일 시작(포함), 기본 오늘")
    ] = None,
    date_to: Annotated[
        date | None, Query(description="제공일 끝(포함), 기본 date_from")
    ] = None,
) -> BaseSchema[RestaurantRevenueResponse]:
    """식당 매출 합계와 일별 내역 (주인·매니저·관리자). 결제 즉시 반영된다."""
    _ = await get_restaurant_with_permission(restaurant_id, db, current_user)
    start = date_from or _today()
    end = date_to or start
    if end < start:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="date_to는 date_from보다 앞설 수 없습니다.",
        )
    rows = (
        await db.execute(
            select(
                MealTicketUsageRequest.served_date,
                func.count(MealTicketUsageRequest.id),
                func.coalesce(func.sum(MealTicketUsageRequest.meal_price), 0),
                func.coalesce(
                    func.sum(MealTicketUsageRequest.ticket_amount_applied), 0
                ),
                func.coalesce(func.sum(MealTicketUsageRequest.cash_amount_required), 0),
            )
            .where(
                MealTicketUsageRequest.restaurant_id == restaurant_id,
                MealTicketUsageRequest.status == "used",
                MealTicketUsageRequest.served_date >= start,
                MealTicketUsageRequest.served_date <= end,
            )
            .group_by(MealTicketUsageRequest.served_date)
            .order_by(MealTicketUsageRequest.served_date)
        )
    ).all()
    by_day = [
        RevenueRow(
            served_date=row[0],
            transaction_count=row[1],
            total_amount=row[2],
            allowance_amount=row[3],
            cash_amount=row[4],
        )
        for row in rows
    ]
    return BaseSchema[RestaurantRevenueResponse](
        data=RestaurantRevenueResponse(
            restaurant_id=restaurant_id,
            date_from=start,
            date_to=end,
            transaction_count=sum(r.transaction_count for r in by_day),
            total_amount=sum(r.total_amount for r in by_day),
            allowance_amount=sum(r.allowance_amount for r in by_day),
            cash_amount=sum(r.cash_amount for r in by_day),
            by_day=by_day,
        )
    )


# ---------------------------------------------------------------------------
# 식당: 스캔 결제
# ---------------------------------------------------------------------------


async def _load_worker_or_404(db: AsyncSession, worker_user_id: str) -> User:
    worker = await db.scalar(select(User).where(User.user_id == worker_user_id))
    if worker is None:
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND,
            detail="등록되지 않은 근로자입니다.",
        )
    return worker


@router.post(
    "/restaurants/{restaurant_id}/ticket-scans",
    status_code=Config.HttpStatus.CREATED,
)
async def scan_and_charge_wallet(
    restaurant_id: int,
    payload: TicketScanCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[TicketUsageRequestResponse]:
    """Charge a scanned worker's allowance wallet as the restaurant owner, manager, or admin."""
    restaurant = await get_restaurant_with_permission(restaurant_id, db, current_user)
    worker = await _load_worker_or_404(db, payload.worker_user_id)
    data = await _charge_wallet(
        db,
        request,
        restaurant=restaurant,
        worker=worker,
        approver_id=current_user.id,
        actor_user_id=current_user.user_id,
        action="meal_ticket_usage_request.scan_redeem",
        meal_type=payload.meal_type,
        served_date=payload.served_date,
        meal_price=payload.meal_price,
    )
    return BaseSchema[TicketUsageRequestResponse](data=data)


@router.post(
    "/restaurants/{restaurant_id}/scanner-key",
    status_code=Config.HttpStatus.CREATED,
)
async def issue_scanner_key(
    restaurant_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[ScannerKeyResponse]:
    """Issue (or rotate) the QR scanner device key for a restaurant. Shown once."""
    restaurant = await get_restaurant_with_permission(restaurant_id, db, current_user)
    key = secrets.token_urlsafe(32)
    rotated = restaurant.scanner_key_hash is not None
    # 기기에 새 키를 심기 전까지 옛 키도 통과시킨다(새 키 결제가 한 번 성공하면 폐기).
    restaurant.scanner_key_hash_prev = restaurant.scanner_key_hash
    restaurant.scanner_key_hash = _hash_key(key)
    try:
        add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=current_user.user_id,
                action="restaurant.scanner_key.issue",
                resource_type="restaurant",
                resource_id=restaurant.id,
                after={"rotated": rotated, "prev_kept": rotated},
            ),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return BaseSchema[ScannerKeyResponse](
        data=ScannerKeyResponse(scanner_key=key, header=SCANNER_KEY_HEADER)
    )


@router.get("/scan", dependencies=[Depends(optional_metrics_x_user_id)])
async def scan_qr(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    worker: Annotated[
        str, Query(min_length=1, description="근로자 user_id (QR URL에 포함)")
    ],
    x_scanner_key: Annotated[
        str | None,
        Header(
            alias=SCANNER_KEY_HEADER,
            description="식당 스캐너 기기 키 (펌웨어가 붙이는 고정 헤더)",
        ),
    ] = None,
    meal_type: Annotated[MealTypeSchema | None, Query()] = None,
) -> BaseSchema[TicketUsageRequestResponse]:
    """QR 기기 진입점: 근로자 QR의 URL을 그대로 GET 하면 지갑 결제가 완료된다.

    인증은 Bearer 토큰이 아니라 기기 고정 헤더 ``X-Scanner-Key``로 한다.
    """
    if not x_scanner_key:
        raise HTTPException(
            status_code=Config.HttpStatus.UNAUTHORIZED,
            detail=f"{SCANNER_KEY_HEADER} 헤더가 필요합니다.",
        )
    key_hash = _hash_key(x_scanner_key)
    restaurant = await db.scalar(
        select(Restaurant).where(
            or_(
                Restaurant.scanner_key_hash == key_hash,
                Restaurant.scanner_key_hash_prev == key_hash,
            ),
            Restaurant.is_active.is_(True),
        )
    )
    if restaurant is None:
        raise HTTPException(
            status_code=Config.HttpStatus.UNAUTHORIZED,
            detail="유효하지 않은 스캐너 키입니다.",
        )
    if restaurant.scanner_key_hash == key_hash:
        # 새 키로 결제가 성공하면 유예 키를 버린다(결제와 같은 트랜잭션에서 커밋된다).
        restaurant.scanner_key_hash_prev = None
    worker_user = await _load_worker_or_404(db, worker.strip())
    data = await _charge_wallet(
        db,
        request,
        restaurant=restaurant,
        worker=worker_user,
        approver_id=restaurant.owner,
        actor_user_id=f"scanner:restaurant:{restaurant.id}",
        action="meal_ticket_usage_request.qr_scan",
        meal_type=meal_type,
        served_date=None,
        meal_price=None,
    )
    return BaseSchema[TicketUsageRequestResponse](data=data)


# ---------------------------------------------------------------------------
# BLE 중계 결제 (POST /relay) — docs/ble-relay.md
# ---------------------------------------------------------------------------

RELAY_VERSION = 1
RELAY_REQUEST_LEN = 87
TICKET_TOKEN_TTL_SECONDS = 60
RELAY_REPLAY_TTL_SECONDS = 300
RELAY_MAX_PER_SECOND = 5
RELAY_RATE_TABLE_MAX = 1000

# 응답 status: 리더가 화면에 띄우는 코드
RELAY_OK = 0
RELAY_INVALID_QR = 1
RELAY_INSUFFICIENT = 2
RELAY_NO_PRICE = 3
RELAY_DEVICE_AUTH = 4
RELAY_QR_EXPIRED = 5
# 같은 (식당, 근로자)가 결정 창 안에 이미 승인됐다. 결제는 없고 리더는 DUPLICATE를 띄운다.
RELAY_DUPLICATE = 6

# ponytail: 재전송 캐시도 레이트 리밋도 프로세스 안 dict이다. uvicorn 워커 1개 전제로,
# app/utils/events.py의 SSE 큐와 같은 가정이다. 워커를 늘리면 Redis로 옮긴다.
_relay_replies: dict[bytes, tuple[float, bytes]] = {}
_relay_hits: dict[str, tuple[int, int]] = {}
# (식당 id, 근로자 user_id) -> (첫 결정 시각, (status, price, applied, cash, txid))
_relay_decisions: dict[
    tuple[int, str], tuple[float, tuple[int, int, int, int, int]]
] = {}
# 식당별 재결제 차단 창의 상한(초). 리더 설정 dedup: 범위(30..600)와 같다.
RELAY_DEDUP_MAX_SECONDS = 600


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _ticket_mac(body: bytes) -> bytes:
    """식권 토큰 본문(uuid+exp) 서명의 앞 16바이트. 키는 서버만 안다."""
    return hmac.new(Config.RELAY_TOKEN_SECRET, b"\x01" + body, hashlib.sha256).digest()[
        :16
    ]


def make_ticket_token(worker_user_id: str) -> str | None:
    """36바이트 식권 토큰(base64url). sub가 UUID가 아닌 레거시 계정이면 None."""
    try:
        worker_uuid = UUID(worker_user_id)
    except ValueError:
        return None
    expires_at = int(time.time()) + TICKET_TOKEN_TTL_SECONDS
    body = worker_uuid.bytes + struct.pack("<I", expires_at)
    return _b64url(body + _ticket_mac(body))


def _read_ticket_token(tok: bytes) -> tuple[str | None, int]:
    """식권 토큰을 검증해 (근로자 user_id, status)를 돌려준다."""
    body, mac = tok[:20], tok[20:]
    if not hmac.compare_digest(mac, _ticket_mac(body)):
        return None, RELAY_INVALID_QR
    (expires_at,) = struct.unpack("<I", body[16:20])
    if expires_at < time.time():
        return None, RELAY_QR_EXPIRED
    return str(UUID(bytes=body[:16])), RELAY_OK


def _relay_pack(  # noqa: PLR0913
    status: int,
    nonce: bytes,
    key: bytes | None,
    *,
    meal_price: int = 0,
    ticket_applied: int = 0,
    cash: int = 0,
    txid: int = 0,
) -> bytes:
    """62바이트 응답을 조립해 스캐너 키로 서명한다. 키를 모르면 mac은 0으로 둔다."""
    body = (
        struct.pack("<BB", RELAY_VERSION, status)
        + nonce
        + struct.pack("<IIIQ", meal_price, ticket_applied, cash, txid)
    )
    mac = hmac.new(key, body, hashlib.sha256).digest() if key else bytes(32)
    return body + mac


def _relay_cached(digest: bytes) -> bytes | None:
    """같은 요청을 최근에 처리했으면 그때 보낸 응답을 그대로 돌려준다(이중 결제 방지)."""
    hit = _relay_replies.get(digest)
    if hit is None or time.monotonic() - hit[0] >= RELAY_REPLAY_TTL_SECONDS:
        return None
    return hit[1]


def _relay_remember(digest: bytes, response: bytes) -> None:
    now = time.monotonic()
    for key, (stored_at, _) in list(_relay_replies.items()):
        if now - stored_at >= RELAY_REPLAY_TTL_SECONDS:
            del _relay_replies[key]
    _relay_replies[digest] = (now, response)


def _relay_decision(
    key: tuple[int, str], ttl: int
) -> tuple[int, int, int, int, int] | None:
    """(식당, 근로자)로 최근에 내린 결정. 창 시작은 첫 결정 시각이라 재사용해도 늘지 않는다.

    창 길이(``ttl``)는 조회 시점의 식당 설정이라, 사장님이 줄이면 바로 반영된다.
    """
    hit = _relay_decisions.get(key)
    if hit is None or time.monotonic() - hit[0] >= ttl:
        return None
    return hit[1]


def _relay_remember_decision(
    key: tuple[int, str], decision: tuple[int, int, int, int, int]
) -> None:
    now = time.monotonic()
    # 식당마다 창이 다르고 바뀔 수도 있어, 설정 상한(600초)이 지난 것만 버린다.
    for stale, (decided_at, _) in list(_relay_decisions.items()):
        if now - decided_at >= RELAY_DEDUP_MAX_SECONDS:
            del _relay_decisions[stale]
    _relay_decisions[key] = (now, decision)


def _relay_matching_key(
    restaurant: Restaurant, req: bytes
) -> tuple[bytes | None, bool]:
    """요청 mac을 현재 키·교체 유예 키 순서로 검증한다. (맞은 키, 현재 키였는지)."""
    for key_hash, is_current in (
        (restaurant.scanner_key_hash, True),
        (restaurant.scanner_key_hash_prev, False),
    ):
        if key_hash is None:
            continue
        key = bytes.fromhex(key_hash)
        if hmac.compare_digest(
            req[55:], hmac.new(key, req[:55], hashlib.sha256).digest()
        ):
            return key, is_current
    return None, False


def _relay_client_key(request: Request) -> str:
    """요청자 식별 키. 프록시 뒤라 X-Forwarded-For 첫 홉을 먼저 본다."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _relay_rate_limited(client: str) -> bool:
    """IP당 초당 요청 수 제한. /relay는 인증이 없어 누구나 부를 수 있다."""
    second = int(time.monotonic())
    if len(_relay_hits) > RELAY_RATE_TABLE_MAX:
        _relay_hits.clear()  # 창이 1초라 통째로 비워도 손해가 없다
    window, count = _relay_hits.get(client, (second, 0))
    count = count + 1 if window == second else 1
    _relay_hits[client] = (second, count)
    return count > RELAY_MAX_PER_SECOND


async def _relay_charge(  # noqa: PLR0913
    db: AsyncSession,
    request: Request,
    restaurant: Restaurant,
    worker_user_id: str,
    nonce: bytes,
    key: bytes,
    *,
    key_is_current: bool,
) -> tuple[bytes, bool]:
    """검증을 통과한 요청을 ``GET /scan``과 같은 결제 로직으로 처리한다.

    같은 (식당, 근로자)에 최근 내린 결정이 있으면 결제하지 않고 그 결정을 재사용한다.
    응답은 언제나 **이번 요청의 nonce**로 다시 서명한다(리더가 nonce 일치를 요구한다).
    """
    decision, reused = await _relay_decide(
        db,
        request,
        restaurant,
        worker_user_id,
        action="meal_ticket_usage_request.ble_relay",
        retire_prev_key=key_is_current,
    )
    return _relay_decided(decision, nonce, key), reused


async def _relay_decide(  # noqa: PLR0913
    db: AsyncSession,
    request: Request,
    restaurant: Restaurant,
    worker_user_id: str,
    *,
    action: str,
    retire_prev_key: bool = False,
) -> tuple[tuple[int, int, int, int, int], bool]:
    """(식당, 근로자) 결제 결정. 최근 결정이 있으면 결제 없이 재사용한다(v1·v2 공용).

    Returns:
        ((status, price, applied, cash, txid), 재사용 여부)
    """
    worker = await db.scalar(select(User).where(User.user_id == worker_user_id))
    if worker is None:
        return (RELAY_INVALID_QR, 0, 0, 0, 0), False

    decision_key = (restaurant.id, worker_user_id)
    decision = _relay_decision(decision_key, relay_dedup_seconds(restaurant))
    if decision is not None:
        # 재스캔한 새 요청이다. 이미 승인됐으면 OK가 아니라 DUPLICATE로 알린다(금액·txid는
        # 첫 결제 것을 그대로 실어 리더·로그에서 대조할 수 있게 한다). 거절 결정은 그대로 준다.
        if decision[0] == RELAY_OK:
            decision = (RELAY_DUPLICATE, *decision[1:])
        return decision, True

    if retire_prev_key and restaurant.scanner_key_hash_prev is not None:
        # 새 키로 결제가 성공하면 유예 키를 버린다(실패하면 롤백되어 그대로 남는다).
        restaurant.scanner_key_hash_prev = None
    try:
        data = await _charge_wallet(
            db,
            request,
            restaurant=restaurant,
            worker=worker,
            approver_id=restaurant.owner,
            actor_user_id=f"relay:restaurant:{restaurant.id}",
            action=action,
            meal_type=None,
            served_date=None,
            meal_price=None,
        )
    except HTTPException as exc:
        decision = (_relay_refusal(exc), 0, 0, 0, 0)
    else:
        decision = (
            RELAY_OK,
            data.meal_price,
            data.ticket_amount_applied,
            data.cash_amount_required,
            data.id,
        )
    _relay_remember_decision(decision_key, decision)
    return decision, False


def _relay_refusal(exc: HTTPException) -> int:
    """``_charge_wallet``의 거절을 relay status로. 그 밖의 오류는 다시 던진다."""
    if exc.status_code == Config.HttpStatus.BAD_REQUEST:
        return RELAY_NO_PRICE
    if exc.status_code == Config.HttpStatus.CONFLICT:
        return RELAY_INSUFFICIENT
    raise exc


def _relay_decided(
    decision: tuple[int, int, int, int, int], nonce: bytes, key: bytes
) -> bytes:
    status, meal_price, ticket_applied, cash, txid = decision
    return _relay_pack(
        status,
        nonce,
        key,
        meal_price=meal_price,
        ticket_applied=ticket_applied,
        cash=cash,
        txid=txid,
    )


async def _relay_settle(
    db: AsyncSession, request: Request, req: bytes
) -> tuple[bytes, bool]:
    """87바이트 요청을 검증·결제하고 (서명된 62바이트 응답, 결정 재사용 여부)를 만든다."""
    restaurant_id = int.from_bytes(req[1:3], "little")
    nonce = req[3:11]
    restaurant = await db.scalar(
        select(Restaurant).where(
            Restaurant.id == restaurant_id,
            Restaurant.is_active.is_(True),
        )
    )
    if restaurant is None or restaurant.scanner_key_hash is None:
        # K를 모르니 서명도 못 한다.
        return _relay_pack(RELAY_DEVICE_AUTH, nonce, None), False
    key, key_is_current = _relay_matching_key(restaurant, req)
    if key is None:
        # mac이 틀렸다. 응답은 현재 키로 서명한다.
        current = bytes.fromhex(restaurant.scanner_key_hash)
        return _relay_pack(RELAY_DEVICE_AUTH, nonce, current), False
    worker_user_id, status = _read_ticket_token(req[19:55])
    if worker_user_id is None:
        return _relay_pack(status, nonce, key), False
    return await _relay_charge(
        db,
        request,
        restaurant,
        worker_user_id,
        nonce,
        key,
        key_is_current=key_is_current,
    )


# ---------------------------------------------------------------------------
# BLE 중계 v2 — docs/ble-relay-v2.md
# 서버 서명(Ed25519) · 요청 암호화(폰이 못 읽음) · 리더별 키 · 오프라인 기록 정산
# ---------------------------------------------------------------------------

OFFLINE_CHARGED = "charged"
OFFLINE_DUPLICATE = "duplicate"
OFFLINE_INVALID = "invalid"
# 오프라인 기록 used_at(리더 RTC)이 QR 유효 시간에서 벗어나도 되는 폭. RTC는 서명된
# server_time으로 보정되므로 몇 초 수준이어야 한다.
OFFLINE_CLOCK_SKEW_SECONDS = 300
_OFFLINE_REFUSALS = {RELAY_NO_PRICE: "no_price", RELAY_INSUFFICIENT: "insufficient"}


def _v2_keys() -> relay_v2.ServerKeys:
    return relay_v2.ServerKeys.from_secret(Config.RELAY_TOKEN_SECRET)


def _make_qr2(worker_user_id: str) -> str | None:
    """v2 QR 문자열. sub가 UUID가 아닌 레거시 계정이면 None."""
    try:
        ticket_id = UUID(worker_user_id)
    except ValueError:
        return None
    return relay_v2.make_qr(
        _v2_keys(),
        Config.RELAY_TOKEN_SECRET,
        ticket_id,
        int(time.time()),
        TICKET_TOKEN_TTL_SECONDS,
    )


@router.get("/relay/keys", dependencies=[Depends(optional_metrics_x_user_id)])
async def relay_keys() -> BaseSchema[RelayKeysResponse]:
    """Server public keys a v2 reader is provisioned with (USB, at install)."""
    keys = _v2_keys()
    return BaseSchema[RelayKeysResponse](
        data=RelayKeysResponse(
            kid=relay_v2.KID,
            sign_pub=keys.sign_pub.hex(),
            enc_pub=keys.enc_pub.hex(),
        )
    )


async def _relay_settle_v2(
    db: AsyncSession, request: Request, req: bytes
) -> tuple[bytes, bool, int]:
    """v2 요청을 복호화·검증·결제한다. (암호화된 응답, 결정 재사용 여부, status).

    Raises:
        relay_v2.RelayV2Error: 길이·버전·복호화 오류(400).
    """
    keys = _v2_keys()
    opened = relay_v2.open_request(keys, req)
    qr_body = opened.qr_signed

    def seal(
        status: int,
        decision: tuple[int, int, int, int, int] = (0, 0, 0, 0, 0),
        ack_seq: int = 0,
    ) -> bytes:
        _, price, applied, cash, txid = decision
        return relay_v2.seal_response(
            keys,
            opened.k_rsp,
            relay_v2.Response(
                status=status,
                rnonce=opened.rnonce,
                reader_id=opened.reader_id,
                ticket_id=qr_body[2:18],
                qn=qr_body[18:26],
                server_time=int(time.time()),
                txid=txid,
                meal_price=price,
                ticket_applied=applied,
                cash=cash,
                ack_seq=ack_seq,
            ),
        )

    reader = await db.get(RestaurantReader, opened.reader_id)
    restaurant = None
    if reader is not None:
        restaurant = await db.scalar(
            select(Restaurant).where(
                Restaurant.id == reader.restaurant_id,
                Restaurant.is_active.is_(True),
            )
        )
    if (
        reader is None
        or reader.pubkey is None
        or restaurant is None
        or not relay_v2.reader_signature_ok(bytes.fromhex(reader.pubkey), opened)
    ):
        return seal(RELAY_DEVICE_AUTH), False, RELAY_DEVICE_AUTH

    ack_seq = await _relay_offline_settle(
        db, request, reader, restaurant, opened.offline
    )
    ticket = relay_v2.read_qr(keys, qr_body)
    if ticket is None:
        return seal(RELAY_INVALID_QR, ack_seq=ack_seq), False, RELAY_INVALID_QR
    if ticket.exp < time.time():
        return seal(RELAY_QR_EXPIRED, ack_seq=ack_seq), False, RELAY_QR_EXPIRED
    decision, reused = await _relay_decide(
        db,
        request,
        restaurant,
        str(ticket.ticket_id),
        action="meal_ticket_usage_request.ble_relay_v2",
    )
    return seal(decision[0], decision, ack_seq), reused, decision[0]


async def _relay_offline_settle(
    db: AsyncSession,
    request: Request,
    reader: RestaurantReader,
    restaurant: Restaurant,
    records: tuple[relay_v2.OfflineRecord, ...],
) -> int:
    """오프라인 기록을 ``(리더, seq)``당 한 번만 정산하고 ack_seq(가장 큰 seq)를 돌려준다."""
    for record in records:
        done = await db.scalar(
            select(RelayOfflineRecord.id).where(
                RelayOfflineRecord.reader_id == reader.id,
                RelayOfflineRecord.seq == record.seq,
            )
        )
        if done is None:
            await _relay_offline_record(db, request, reader, restaurant, record)
    return max((record.seq for record in records), default=0)


async def _relay_offline_record(
    db: AsyncSession,
    request: Request,
    reader: RestaurantReader,
    restaurant: Restaurant,
    record: relay_v2.OfflineRecord,
) -> None:
    """오프라인 기록 한 건을 판정하고, 청구 대상이면 결제한다.

    ponytail: 같은 QR을 두 리더가 동시에 올리는 경합은 막지 않는다(워커 1개·드문 경우).
    막아야 하면 (ticket_id, qn) 부분 유니크 인덱스로 옮긴다.
    """
    ticket_id = str(record.ticket_id)
    used_at = datetime.fromtimestamp(record.used_at, timezone.utc)
    worker = await db.scalar(select(User).where(User.user_id == ticket_id))
    verdict = await _offline_verdict(db, restaurant, worker, record)
    row = RelayOfflineRecord(
        reader_id=reader.id,
        seq=record.seq,
        ticket_id=ticket_id,
        qn=record.qn.hex(),
        used_at=used_at,
        status=verdict or OFFLINE_CHARGED,
    )
    db.add(row)
    if verdict is None and worker is not None:
        try:
            data = await _charge_wallet(
                db,
                request,
                restaurant=restaurant,
                worker=worker,
                approver_id=restaurant.owner,
                actor_user_id=f"relay:reader:{reader.id}",
                action="meal_ticket_usage_request.ble_offline",
                meal_type=None,
                served_date=used_at.astimezone(Config.TZ).date(),
                meal_price=None,
            )
        except HTTPException as exc:
            row.status = _OFFLINE_REFUSALS[_relay_refusal(exc)]
        else:
            row.usage_request_id = data.id
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    logger.info(
        "relay offline reader=%d seq=%d status=%s", reader.id, record.seq, row.status
    )


async def _offline_verdict(
    db: AsyncSession,
    restaurant: Restaurant,
    worker: User | None,
    record: relay_v2.OfflineRecord,
) -> str | None:
    """청구하면 안 되는 기록의 status. 청구 대상이면 None.

    기록은 온라인 QR(SK2) 또는 오프라인 토큰(SK2O)에서 나온다. 어느 쪽인지는 qn 도메인으로 안다.

    - invalid: 서버가 발급한 QR·토큰이 아니거나(qn 불일치), 미등록 근로자거나, used_at이 유효
      시간에서 ``OFFLINE_CLOCK_SKEW_SECONDS`` 넘게 벗어났다.
    - duplicate: 같은 QR·토큰의 기록이 이미 있거나(토큰은 1회용), 같은 식당에서 이미 결제됐다.
      온라인 QR은 그 유효 시간 앞뒤, 토큰은 used_at 앞뒤 ``RELAY_DECISION_TTL_SECONDS``를 본다.
      온라인 승인 응답을 못 받은 리더가 오프라인으로 다시 승인한 경우와 재스캔을 막는다(온라인
      2단 멱등과 같은 정책: 그 창 안의 진짜 두 번째 식사는 청구되지 않는다).
    """
    secret = Config.RELAY_TOKEN_SECRET
    window = relay_dedup_seconds(restaurant)
    if hmac.compare_digest(
        record.qn, relay_v2.ticket_qn(secret, record.ticket_id, record.iat)
    ):
        lifetime = TICKET_TOKEN_TTL_SECONDS
        charged_from, charged_to = record.iat - window, record.iat + lifetime + window
    elif hmac.compare_digest(
        record.qn,
        relay_v2.ticket_qn(secret, record.ticket_id, record.iat, relay_v2.QN_OFFLINE),
    ):
        # 토큰 exp = 창 끝 = iat + ttl + index(index < 창당 장수)
        lifetime = Config.OFFLINE_TOKEN_TTL_SECONDS + Config.OFFLINE_TOKENS_PER_WINDOW
        charged_from, charged_to = record.used_at - window, record.used_at + window
    else:
        return OFFLINE_INVALID
    valid_from = record.iat - OFFLINE_CLOCK_SKEW_SECONDS
    valid_to = record.iat + lifetime + OFFLINE_CLOCK_SKEW_SECONDS
    if worker is None or not valid_from <= record.used_at <= valid_to:
        return OFFLINE_INVALID

    same_qr = await db.scalar(
        select(RelayOfflineRecord.id)
        .where(
            RelayOfflineRecord.ticket_id == str(record.ticket_id),
            RelayOfflineRecord.qn == record.qn.hex(),
        )
        .limit(1)
    )
    charged_nearby = await db.scalar(
        select(MealTicketUsageRequest.id)
        .where(
            MealTicketUsageRequest.worker_id == worker.id,
            MealTicketUsageRequest.restaurant_id == restaurant.id,
            MealTicketUsageRequest.approved_at
            >= datetime.fromtimestamp(charged_from, timezone.utc),
            MealTicketUsageRequest.approved_at
            <= datetime.fromtimestamp(charged_to, timezone.utc),
        )
        .limit(1)
    )
    if same_qr is not None or charged_nearby is not None:
        return OFFLINE_DUPLICATE
    return None


@router.post(
    "/relay",
    dependencies=[Depends(optional_metrics_x_user_id)],
    responses={Config.HttpStatus.OK: {"model": RelayResponse}},
)
async def relay_scan(  # noqa: PLR0911
    payload: RelayRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    """폰이 중계한 리더 서명 요청을 검증·결제하고, 서명된 응답을 돌려준다.

    인증 헤더는 없다(``X-Scanner-Key``도 받지 않는다). 요청 본문 자체가 식당 스캐너 키로
    서명돼 있고, 거절도 200으로 (서명된 status와 함께) 나간다. 400은 길이·버전·base64
    오류뿐이다. 요청 제한이 아니라 청구 제한이다 — 몇 번을 물어도 답은 주되, 같은
    (식당, 근로자)는 ``RELAY_DECISION_TTL_SECONDS`` 안에 한 번만 청구된다.
    """
    started = time.monotonic()
    try:
        req = _b64url_decode(payload.req)
    except (binascii.Error, ValueError):
        return JSONResponse(
            {"error": "req base64url 디코딩 실패"},
            status_code=Config.HttpStatus.BAD_REQUEST,
        )
    version = req[0] if req else 0
    if version == relay_v2.VERSION:
        return await _relay_scan_v2(db, request, req, started)
    if len(req) != RELAY_REQUEST_LEN:
        return JSONResponse(
            {"error": f"req는 {RELAY_REQUEST_LEN}바이트여야 합니다."},
            status_code=Config.HttpStatus.BAD_REQUEST,
        )
    if version != RELAY_VERSION:
        return JSONResponse(
            {"error": f"지원하지 않는 ver: {version}"},
            status_code=Config.HttpStatus.BAD_REQUEST,
        )

    digest = hashlib.sha256(req).digest()
    cached = _relay_cached(digest)
    if cached is not None:
        logger.info(
            "relay rid=%d nonce=%s status=%d RESENT",
            int.from_bytes(req[1:3], "little"),
            req[3:11].hex(),
            cached[1],
        )
        return JSONResponse({"resp": _b64url(cached)})
    if _relay_rate_limited(_relay_client_key(request)):
        return _relay_too_many()

    response, reused = await _relay_settle(db, request, req)
    _relay_remember(digest, response)
    logger.info(
        "relay rid=%d sid=%s nonce=%s status=%d%s %dms",
        int.from_bytes(req[1:3], "little"),
        req[11:19].hex(),
        req[3:11].hex(),
        response[1],
        " REUSED" if reused else "",
        int((time.monotonic() - started) * 1000),
    )
    return JSONResponse({"resp": _b64url(response)})


def _relay_too_many() -> JSONResponse:
    return JSONResponse(
        {"error": "요청이 너무 잦습니다."},
        status_code=Config.HttpStatus.TOO_MANY_REQUESTS,
    )


async def _relay_scan_v2(
    db: AsyncSession, request: Request, req: bytes, started: float
) -> JSONResponse:
    """v2: 1단 멱등·레이트 리밋은 v1과 같고, 처리만 ``_relay_settle_v2``로 간다."""
    digest = hashlib.sha256(req).digest()
    cached = _relay_cached(digest)
    if cached is not None:
        logger.info("relay v2 digest=%s RESENT", digest[:8].hex())
        return JSONResponse({"resp": _b64url(cached)})
    if _relay_rate_limited(_relay_client_key(request)):
        return _relay_too_many()
    try:
        response, reused, status = await _relay_settle_v2(db, request, req)
    except relay_v2.RelayV2Error as exc:
        return JSONResponse(
            {"error": str(exc)}, status_code=Config.HttpStatus.BAD_REQUEST
        )
    _relay_remember(digest, response)
    logger.info(
        "relay v2 digest=%s status=%d%s %dms",
        digest[:8].hex(),
        status,
        " REUSED" if reused else "",
        int((time.monotonic() - started) * 1000),
    )
    return JSONResponse({"resp": _b64url(response)})
