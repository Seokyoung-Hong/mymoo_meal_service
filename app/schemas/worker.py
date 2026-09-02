"""Worker meal allowance wallet and cash wallet request/response schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.base import Timestamp as Tsp
from app.schemas.meals import MealType


Timestamp = Annotated[datetime, Tsp]
MealTicketStatus = Literal["available", "used", "expired"]
TicketUsageRequestStatus = Literal["pending", "used"]
CashTransactionType = Literal["mock_card_charge", "ticket_shortfall_payment"]


class MealTicketResponse(BaseModel):
    """Meal allowance credit bucket response."""

    id: int
    code: str
    amount: int
    remaining_amount: int
    expires_on: date
    status: MealTicketStatus
    registered_at: Timestamp
    used_at: Timestamp | None = None


class MealAllowanceCreate(BaseModel):
    """Issue meal allowance tickets to workers as an admin."""

    worker_user_ids: list[str] = Field(min_length=1)
    amount: int = Field(gt=0)
    expires_on: date


class AllowanceTicketResponse(MealTicketResponse):
    """Meal ticket response enriched with the owning worker's user_id."""

    worker_user_id: str


class AllowanceBalanceResponse(BaseModel):
    """Sum of the worker's unexpired allowance bucket balances."""

    balance: int


class WorkerQrResponse(BaseModel):
    """URL to encode in the worker's QR code; scanner devices GET it as-is."""

    url: str


class ScannerKeyResponse(BaseModel):
    """Newly issued restaurant scanner key. Only the sha256 is stored server-side."""

    scanner_key: str
    header: str


class TicketScanCreate(BaseModel):
    """Charge a scanned worker's allowance wallet in one step from a restaurant scanner."""

    worker_user_id: str = Field(min_length=1, max_length=255)
    meal_type: MealType | None = None
    served_date: date | None = None
    meal_price: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def normalize_worker_user_id(self) -> "TicketScanCreate":
        """Store compact worker ids without surrounding whitespace."""
        self.worker_user_id = self.worker_user_id.strip()
        if not self.worker_user_id:
            raise ValueError("worker_user_id must not be empty")
        return self


class TicketUsageRequestResponse(BaseModel):
    """Ticket usage request response for workers and restaurants."""

    id: int
    ticket_id: int | None = None
    ticket_code: str | None = None
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


class RevenueRow(BaseModel):
    """Payment totals for one served date."""

    served_date: date
    transaction_count: int
    total_amount: int
    allowance_amount: int
    cash_amount: int


class RestaurantRevenueResponse(BaseModel):
    """Restaurant revenue over a served-date range with a per-day breakdown."""

    restaurant_id: int
    date_from: date
    date_to: date
    transaction_count: int
    total_amount: int
    allowance_amount: int
    cash_amount: int
    by_day: list[RevenueRow]


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
