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
    """Worker QR payloads: the legacy scan URL and the signed BLE relay token."""

    url: str
    # BLE 리더용 36바이트 식권 토큰(base64url). Keycloak sub가 UUID가 아니면 None.
    tok: str | None = None
    # relay v2 QR 문자열 ``SK2.<b64url>`` (docs/ble-relay-v2.md). 앱이 ``.<ble_id>``를 붙인다.
    qr2: str | None = None
    ttl: int


class OfflineTokensResponse(BaseModel):
    """오프라인 전용 1회용 토큰(``SK2O.<b64url>``)과 만료 시각(unix 초, 모든 토큰 공통)."""

    tokens: list[str]
    exp: int


class RelayRequest(BaseModel):
    """BLE 중계 요청 본문. ``req``는 v1 87바이트 또는 v2 암호화 요청(base64url)."""

    req: str


class RelayResponse(BaseModel):
    """BLE 중계 응답 본문. v1 62바이트 / v2 154바이트(base64url)."""

    resp: str


class RelayKeysResponse(BaseModel):
    """relay v2 서버 공개키. 설치 시 리더에 USB로 주입한다(hex)."""

    kid: int
    sign_pub: str
    enc_pub: str


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
