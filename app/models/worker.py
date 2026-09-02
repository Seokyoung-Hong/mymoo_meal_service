"""Worker meal allowance wallet, cash wallet, and mock card payment models."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class MealTicket(Base):
    """A meal allowance credit bucket issued to a worker.

    잔액 차감형 지갑의 한 단위다. ``amount``는 발급액, ``remaining_amount``는 남은 잔액이며
    스캔 결제 시 만료일이 빠른 버킷부터 차감된다. 잔액이 0이 되면 ``used``, 만료일이
    지나면 ``expired``가 된다.
    """

    __tablename__ = "meal_ticket"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    owner_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("User.id", ondelete="CASCADE"),
        nullable=False,
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_on: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="available",
        server_default="available",
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    owner = relationship("User")
    usage_requests = relationship(
        "MealTicketUsageRequest",
        back_populates="ticket",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="meal_ticket_amount_positive_check"),
        CheckConstraint(
            "remaining_amount >= 0 AND remaining_amount <= amount",
            name="meal_ticket_remaining_check",
        ),
        CheckConstraint(
            "status IN ('available', 'used', 'expired')",
            name="meal_ticket_status_check",
        ),
        Index("meal_ticket_owner_index", "owner_id"),
        Index("meal_ticket_status_index", "status"),
        Index("meal_ticket_expires_on_index", "expires_on"),
    )


class MealTicketUsageRequest(Base):
    """A meal payment settled from a worker's allowance wallet at a restaurant.

    ``ticket_id``는 차감이 시작된 버킷(참고용)이며, 결제가 여러 버킷에 걸치거나
    전액 캐시로 결제되면 첫 버킷 또는 None이 기록된다.
    """

    __tablename__ = "meal_ticket_usage_request"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("meal_ticket.id", ondelete="CASCADE"),
        nullable=True,
    )
    worker_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("User.id", ondelete="CASCADE"),
        nullable=False,
    )
    restaurant_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("Restaurant.id", ondelete="CASCADE"),
        nullable=False,
    )
    meal_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    served_date: Mapped[date] = mapped_column(Date, nullable=False)
    meal_price: Mapped[int] = mapped_column(Integer, nullable=False)
    ticket_amount_applied: Mapped[int] = mapped_column(Integer, nullable=False)
    cash_amount_required: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    approved_by: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("User.id"),
        nullable=True,
    )

    ticket = relationship("MealTicket", back_populates="usage_requests")
    worker = relationship("User", foreign_keys=[worker_id])
    restaurant = relationship("Restaurant")
    approver = relationship("User", foreign_keys=[approved_by])

    __table_args__ = (
        CheckConstraint("meal_price > 0", name="ticket_request_meal_price_check"),
        CheckConstraint(
            "ticket_amount_applied >= 0",
            name="ticket_request_ticket_amount_check",
        ),
        CheckConstraint(
            "cash_amount_required >= 0",
            name="ticket_request_cash_amount_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'used')",
            name="ticket_request_status_check",
        ),
        Index("ticket_request_worker_index", "worker_id"),
        Index("ticket_request_restaurant_index", "restaurant_id"),
        Index("ticket_request_status_index", "status"),
        Index("ticket_request_ticket_index", "ticket_id"),
    )


class CashWallet(Base):
    """Cash-like wallet balance owned by a worker."""

    __tablename__ = "cash_wallet"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("User.id", ondelete="CASCADE"),
        nullable=False,
    )
    balance: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("user_id", name="cash_wallet_user_unique"),
        CheckConstraint("balance >= 0", name="cash_wallet_balance_check"),
    )


class CashTransaction(Base):
    """Cash wallet ledger row for mock card charges and shortfall payments."""

    __tablename__ = "cash_transaction"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("User.id", ondelete="CASCADE"),
        nullable=False,
    )
    usage_request_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("meal_ticket_usage_request.id", ondelete="SET NULL"),
        nullable=True,
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="succeeded",
        server_default="succeeded",
    )
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    user = relationship("User")
    usage_request = relationship("MealTicketUsageRequest")

    __table_args__ = (
        CheckConstraint("amount != 0", name="cash_transaction_amount_check"),
        CheckConstraint(
            "transaction_type IN ('mock_card_charge', 'ticket_shortfall_payment')",
            name="cash_transaction_type_check",
        ),
        CheckConstraint(
            "status IN ('succeeded')",
            name="cash_transaction_status_check",
        ),
        Index("cash_transaction_user_index", "user_id"),
        Index("cash_transaction_created_at_index", "created_at"),
        Index("cash_transaction_usage_request_index", "usage_request_id"),
    )
