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
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import date, datetime, timezone
from typing import Annotated
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.config import Config
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
    MockCardChargeCreate,
    RestaurantRevenueResponse,
    RevenueRow,
    ScannerKeyResponse,
    TicketScanCreate,
    TicketUsageRequestResponse,
    TicketUsageRequestStatus,
    WorkerQrResponse,
)
from app.services.audit import AuditLogEntry, add_audit_log, request_id_from_request
from app.services.pricing import resolve_price
from app.utils.auth import optional_metrics_x_user_id
from app.utils.db import get_admin_user, get_current_user, get_db
from app.utils.restaurants import get_restaurant_with_permission


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
) -> int:
    """근로자 지갑에서 식사 금액을 차감하고 사용 내역 id를 돌려준다."""
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
    return usage_request_id


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


@router.get("/worker/qr")
async def get_worker_qr(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[WorkerQrResponse]:
    """Return the scan URL to encode in the worker's QR code."""
    base = Config.PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    url = f"{base}/scan?{urlencode({'worker': current_user.user_id})}"
    return BaseSchema[WorkerQrResponse](data=WorkerQrResponse(url=url))


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

    return BaseSchema[CashTransactionResponse](
        data=_cash_transaction_response(transaction)
    )


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
    usage_request_id = await _charge_wallet(
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
    usage_request = await _load_usage_request_or_404(db, usage_request_id)
    return BaseSchema[TicketUsageRequestResponse](
        data=_usage_request_response(usage_request)
    )


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
                after={"rotated": rotated},
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
    restaurant = await db.scalar(
        select(Restaurant).where(
            Restaurant.scanner_key_hash == _hash_key(x_scanner_key),
            Restaurant.is_active.is_(True),
        )
    )
    if restaurant is None:
        raise HTTPException(
            status_code=Config.HttpStatus.UNAUTHORIZED,
            detail="유효하지 않은 스캐너 키입니다.",
        )
    worker_user = await _load_worker_or_404(db, worker.strip())
    usage_request_id = await _charge_wallet(
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
    usage_request = await _load_usage_request_or_404(db, usage_request_id)
    return BaseSchema[TicketUsageRequestResponse](
        data=_usage_request_response(usage_request)
    )
