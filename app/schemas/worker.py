"""Worker meal ticket and cash wallet request/response schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.base import Timestamp as Tsp
from app.schemas.meals import MealType


Timestamp = Annotated[datetime, Tsp]
MealTicketStatus = Literal["available", "pending", "used", "expired"]
TicketUsageRequestStatus = Literal["pending", "used"]
CashTransactionType = Literal["mock_card_charge", "ticket_shortfall_payment"]


class MealTicketCreate(BaseModel):
    """Register a one-use meal ticket by code."""

    code: str = Field(min_length=1, max_length=64)
    amount: int = Field(gt=0)
    expires_on: date

    @model_validator(mode="after")
    def normalize_code(self) -> "MealTicketCreate":
        """Store compact ticket codes without surrounding whitespace."""
        self.code = self.code.strip()
        if not self.code:
            raise ValueError("code must not be empty")
        return self


class MealTicketResponse(BaseModel):
    """Meal ticket response."""

    id: int
    code: str
    amount: int
    expires_on: date
    status: MealTicketStatus
    registered_at: Timestamp
    used_at: Timestamp | None = None


class TicketUsageRequestCreate(BaseModel):
    """Create a pending ticket usage request for restaurant approval."""

    ticket_code: str = Field(min_length=1, max_length=64)
    restaurant_id: int = Field(gt=0)
    meal_type: MealType | None = None
    served_date: date | None = None
    meal_price: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def normalize_ticket_code(self) -> "TicketUsageRequestCreate":
        """Store compact ticket codes without surrounding whitespace."""
        self.ticket_code = self.ticket_code.strip()
        if not self.ticket_code:
            raise ValueError("ticket_code must not be empty")
        return self


class TicketUsageRequestResponse(BaseModel):
    """Ticket usage request response for workers and restaurants."""

    id: int
    ticket_id: int
    ticket_code: str
    worker_user_id: str
    restaurant_id: int
    restaurant_name: str
    meal_type: MealType | None = None
    served_date: date
    meal_price: int
    ticket_amount_applied: int
    cash_amount_required: int
    status: TicketUsageRequestStatus
    requested_at: Timestamp
    approved_at: Timestamp | None = None
    approved_by_user_id: str | None = None


class CashBalanceResponse(BaseModel):
    """Current cash-like wallet balance."""

    balance: int


class MockCardChargeCreate(BaseModel):
    """Mock card charge request that credits worker cash."""

    amount: int = Field(gt=0)
    card_last4: str | None = Field(default=None, min_length=4, max_length=4)


class CashTransactionResponse(BaseModel):
    """Cash wallet ledger response."""

    id: int
    amount: int
    transaction_type: CashTransactionType
    status: Literal["succeeded"]
    description: str | None = None
    usage_request_id: int | None = None
    created_at: Timestamp
