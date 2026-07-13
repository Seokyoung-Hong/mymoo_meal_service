"""Worker meal ticket, cash wallet, and restaurant approval API."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
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
from app.schemas.worker import (
    CashBalanceResponse,
    CashTransactionResponse,
    MealTicketCreate,
    MealTicketResponse,
    MockCardChargeCreate,
    TicketUsageRequestCreate,
    TicketUsageRequestResponse,
    TicketUsageRequestStatus,
)
from app.services.audit import AuditLogEntry, add_audit_log, request_id_from_request
from app.services.pricing import resolve_price
from app.utils.db import get_current_user, get_db
from app.utils.restaurants import get_restaurant_with_permission


router = APIRouter(tags=["Worker"])


def _today() -> date:
    return datetime.now(Config.TZ).date()


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
        ticket_code=ticket.code,
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


async def _resolve_meal_price(
    db: AsyncSession,
    payload: TicketUsageRequestCreate,
) -> int:
    if payload.meal_price is not None:
        return payload.meal_price
    served_date = payload.served_date or _today()
    resolved = await resolve_price(
        db,
        restaurant_id=payload.restaurant_id,
        meal_type=payload.meal_type,
        served_date=served_date,
    )
    if resolved.price is not None:
        return resolved.price
    raise HTTPException(
        status_code=Config.HttpStatus.BAD_REQUEST,
        detail="가격 정책이 없어 meal_price를 입력해야 합니다.",
    )


@router.post("/worker/tickets", status_code=Config.HttpStatus.CREATED)
async def register_meal_ticket(
    payload: MealTicketCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[MealTicketResponse]:
    """Register a one-use meal ticket for the current worker."""
    if payload.expires_on < _today():
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="만료된 식권은 등록할 수 없습니다.",
        )
    existing = await db.scalar(select(MealTicket).where(MealTicket.code == payload.code))
    if existing is not None:
        raise HTTPException(
            status_code=Config.HttpStatus.CONFLICT,
            detail="이미 등록된 식권 코드입니다.",
        )

    ticket = MealTicket(
        code=payload.code,
        owner_id=current_user.id,
        amount=payload.amount,
        expires_on=payload.expires_on,
    )
    try:
        db.add(ticket)
        await db.flush()
        add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=current_user.user_id,
                action="meal_ticket.register",
                resource_type="meal_ticket",
                resource_id=ticket.id,
                after={
                    "code": ticket.code,
                    "amount": ticket.amount,
                    "expires_on": ticket.expires_on.isoformat(),
                    "status": ticket.status,
                },
            ),
        )
        await db.commit()
        await db.refresh(ticket)
    except Exception:
        await db.rollback()
        raise

    return BaseSchema[MealTicketResponse](data=_ticket_response(ticket))


@router.get("/worker/tickets")
async def list_meal_tickets(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[list[MealTicketResponse]]:
    """List tickets owned by the current worker."""
    result = await db.execute(
        select(MealTicket)
        .where(MealTicket.owner_id == current_user.id)
        .order_by(MealTicket.registered_at.desc(), MealTicket.id.desc())
    )
    return BaseSchema[list[MealTicketResponse]](
        data=[_ticket_response(ticket) for ticket in result.scalars().all()]
    )


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


@router.post("/worker/ticket-usage-requests", status_code=Config.HttpStatus.CREATED)
async def create_ticket_usage_request(
    payload: TicketUsageRequestCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[TicketUsageRequestResponse]:
    """Create a pending meal ticket usage request for restaurant approval."""
    restaurant = await db.get(Restaurant, payload.restaurant_id)
    if restaurant is None or not restaurant.is_active:
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND,
            detail="해당 식당이 존재하지 않습니다.",
        )

    ticket = await db.scalar(
        select(MealTicket).where(
            MealTicket.code == payload.ticket_code,
            MealTicket.owner_id == current_user.id,
        )
    )
    if ticket is None:
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND,
            detail="사용 가능한 식권을 찾을 수 없습니다.",
        )
    if ticket.status != "available":
        raise HTTPException(
            status_code=Config.HttpStatus.CONFLICT,
            detail="이미 사용 중이거나 사용된 식권입니다.",
        )
    if ticket.expires_on < _today():
        ticket.status = "expired"
        await db.commit()
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="만료된 식권입니다.",
        )

    meal_price = await _resolve_meal_price(db, payload)
    ticket_amount_applied = min(ticket.amount, meal_price)
    cash_amount_required = max(meal_price - ticket_amount_applied, 0)
    served_date = payload.served_date or _today()

    usage_request = MealTicketUsageRequest(
        ticket_id=ticket.id,
        worker_id=current_user.id,
        restaurant_id=restaurant.id,
        meal_type=payload.meal_type.value if payload.meal_type else None,
        served_date=served_date,
        meal_price=meal_price,
        ticket_amount_applied=ticket_amount_applied,
        cash_amount_required=cash_amount_required,
    )
    ticket.status = "pending"

    try:
        db.add(usage_request)
        await db.flush()
        add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=current_user.user_id,
                action="meal_ticket_usage_request.create",
                resource_type="meal_ticket_usage_request",
                resource_id=usage_request.id,
                after={
                    "ticket_id": ticket.id,
                    "restaurant_id": restaurant.id,
                    "meal_price": meal_price,
                    "ticket_amount_applied": ticket_amount_applied,
                    "cash_amount_required": cash_amount_required,
                    "status": usage_request.status,
                },
            ),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    usage_request = await _load_usage_request_or_404(db, usage_request.id)
    return BaseSchema[TicketUsageRequestResponse](
        data=_usage_request_response(usage_request)
    )


@router.get("/worker/ticket-usage-requests")
async def list_worker_ticket_usage_requests(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[list[TicketUsageRequestResponse]]:
    """List ticket usage requests owned by the current worker."""
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
async def list_restaurant_ticket_usage_requests(
    restaurant_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    status: Annotated[TicketUsageRequestStatus | None, Query()] = None,
) -> BaseSchema[list[TicketUsageRequestResponse]]:
    """List ticket usage requests for a restaurant owner, manager, or admin."""
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
    result = await db.execute(stmt)
    return BaseSchema[list[TicketUsageRequestResponse]](
        data=[
            _usage_request_response(usage_request)
            for usage_request in result.scalars().all()
        ]
    )


@router.post("/restaurants/{restaurant_id}/ticket-usage-requests/{request_id}/approval")
async def approve_ticket_usage_request(
    restaurant_id: int,
    request_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[TicketUsageRequestResponse]:
    """Approve a pending ticket usage request and mark the ticket as used."""
    _ = await get_restaurant_with_permission(restaurant_id, db, current_user)
    usage_request = await _load_usage_request_or_404(db, request_id)
    if usage_request.restaurant_id != restaurant_id:
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND,
            detail="식당의 식권 사용 요청을 찾을 수 없습니다.",
        )
    if usage_request.status != "pending":
        raise HTTPException(
            status_code=Config.HttpStatus.CONFLICT,
            detail="이미 처리된 식권 사용 요청입니다.",
        )
    if usage_request.ticket.status != "pending":
        raise HTTPException(
            status_code=Config.HttpStatus.CONFLICT,
            detail="처리할 수 없는 식권 상태입니다.",
        )
    if usage_request.ticket.expires_on < _today():
        usage_request.ticket.status = "expired"
        await db.commit()
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="만료된 식권입니다.",
        )

    wallet = await _get_or_create_wallet(db, usage_request.worker_id)
    if wallet.balance < usage_request.cash_amount_required:
        raise HTTPException(
            status_code=Config.HttpStatus.CONFLICT,
            detail="부족분을 결제할 캐시 잔액이 부족합니다.",
        )

    before = {
        "request_status": usage_request.status,
        "ticket_status": usage_request.ticket.status,
        "cash_balance": wallet.balance,
    }
    usage_request.status = "used"
    usage_request.approved_at = datetime.now(timezone.utc)
    usage_request.approved_by = current_user.id
    usage_request.ticket.status = "used"
    usage_request.ticket.used_at = usage_request.approved_at

    transaction: CashTransaction | None = None
    if usage_request.cash_amount_required > 0:
        wallet.balance -= usage_request.cash_amount_required
        transaction = CashTransaction(
            user_id=usage_request.worker_id,
            usage_request_id=usage_request.id,
            amount=-usage_request.cash_amount_required,
            transaction_type="ticket_shortfall_payment",
            description=f"ticket request #{usage_request.id} shortfall",
        )
        db.add(transaction)

    try:
        if transaction is not None:
            await db.flush()
        add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=current_user.user_id,
                action="meal_ticket_usage_request.approve",
                resource_type="meal_ticket_usage_request",
                resource_id=usage_request.id,
                before=before,
                after={
                    "request_status": usage_request.status,
                    "ticket_status": usage_request.ticket.status,
                    "cash_balance": wallet.balance,
                    "cash_transaction_id": transaction.id if transaction else None,
                },
            ),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    usage_request = await _load_usage_request_or_404(db, request_id)
    return BaseSchema[TicketUsageRequestResponse](
        data=_usage_request_response(usage_request)
    )
