"""Pricing policy request and response schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.base import Timestamp as Tsp
from app.schemas.meals import MealType


Timestamp = Annotated[datetime, Tsp]
PricingPolicyType = Literal["restaurant_fixed", "meal_type_fixed", "date_specific"]


class PricingPolicyBase(BaseModel):
    """Shared pricing policy fields."""

    policy_type: PricingPolicyType
    price: int = Field(gt=0)
    meal_type: MealType | None = None
    served_date: date | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def validate_scope(self) -> "PricingPolicyBase":
        """Ensure the scope matches the selected policy type."""
        if self.policy_type == "restaurant_fixed":
            if self.meal_type is not None or self.served_date is not None:
                raise ValueError(
                    "restaurant_fixed policies cannot include meal_type or served_date"
                )
        elif self.policy_type == "meal_type_fixed":
            if self.meal_type is None or self.served_date is not None:
                raise ValueError(
                    "meal_type_fixed policies require meal_type and cannot include served_date"
                )
        elif self.served_date is None:
            raise ValueError("date_specific policies require served_date")
        return self


class PricingPolicyCreate(PricingPolicyBase):
    """Create pricing policy request."""


class PricingPolicyUpdate(PricingPolicyBase):
    """Replace pricing policy request."""


class PricingPolicyStatusUpdate(BaseModel):
    """Activate or deactivate an existing pricing policy."""

    is_active: bool


class PricingPolicyResponse(PricingPolicyBase):
    """Persisted pricing policy response."""

    id: int
    restaurant_id: int
    created_at: Timestamp
    updated_at: Timestamp


class PriceResolutionResponse(BaseModel):
    """Resolved price response with nullable no-match behavior."""

    restaurant_id: int
    price: int | None
    policy_type: PricingPolicyType | None = None
    pricing_policy_id: int | None = None
    meal_type: MealType | None = None
    served_date: date | None = None
    source: Literal["pricing_policy", "legacy_restaurant_price"] | None = None
