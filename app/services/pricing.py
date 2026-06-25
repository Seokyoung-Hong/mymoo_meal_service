"""Pricing policy resolution and mutation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, cast

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.models.meals import MealType
from app.models.pricing import RestaurantPricingPolicy
from app.models.restaurants import Restaurant
from app.schemas.meals import MealType as MealTypeSchema
from app.schemas.pricing import PricingPolicyType


@dataclass(frozen=True)
class ResolvedPrice:
    """Resolved price with matched policy and scope metadata."""

    restaurant_id: int
    price: int | None
    policy_type: PricingPolicyType | None = None
    pricing_policy_id: int | None = None
    meal_type: MealTypeSchema | None = None
    served_date: date | None = None
    source: str | None = None


async def get_meal_type_by_name(
    db: AsyncSession,
    meal_type: MealTypeSchema | None,
) -> MealType | None:
    """Resolve an optional meal type enum to its ORM row."""
    if meal_type is None:
        return None
    result = await db.execute(select(MealType).where(MealType.name == meal_type.value))
    found = result.scalar_one_or_none()
    if found is None:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="유효하지 않은 meal_type입니다.",
        )
    return found


def pricing_policy_to_dict(policy: RestaurantPricingPolicy) -> dict[str, object]:
    """Return a JSON-safe audit representation of a pricing policy."""
    return {
        "id": policy.id,
        "restaurant_id": policy.restaurant_id,
        "policy_type": policy.policy_type,
        "price": policy.price,
        "meal_type": policy.meal_type.name if policy.meal_type else None,
        "meal_type_id": policy.meal_type_id,
        "served_date": policy.served_date.isoformat() if policy.served_date else None,
        "is_active": policy.is_active,
    }


def pricing_scope_filter(
    policy_type: PricingPolicyType,
    restaurant_id: int,
    meal_type_id: int | None,
    served_date: date | None,
):
    """Build an exact-scope filter for duplicate active policy checks."""
    conditions = [
        RestaurantPricingPolicy.restaurant_id == restaurant_id,
        RestaurantPricingPolicy.policy_type == policy_type,
        RestaurantPricingPolicy.is_active.is_(True),
    ]
    if meal_type_id is None:
        conditions.append(RestaurantPricingPolicy.meal_type_id.is_(None))
    else:
        conditions.append(RestaurantPricingPolicy.meal_type_id == meal_type_id)
    if served_date is None:
        conditions.append(RestaurantPricingPolicy.served_date.is_(None))
    else:
        conditions.append(RestaurantPricingPolicy.served_date == served_date)
    return conditions


async def ensure_no_active_duplicate(  # noqa: PLR0913
    db: AsyncSession,
    *,
    policy_type: PricingPolicyType,
    restaurant_id: int,
    meal_type_id: int | None,
    served_date: date | None,
    exclude_policy_id: int | None = None,
) -> None:
    """Reject active duplicate exact-scope policies deterministically."""
    stmt = select(RestaurantPricingPolicy.id).where(
        *pricing_scope_filter(policy_type, restaurant_id, meal_type_id, served_date)
    )
    if exclude_policy_id is not None:
        stmt = stmt.where(RestaurantPricingPolicy.id != exclude_policy_id)
    duplicate_id = await db.scalar(stmt)
    if duplicate_id is not None:
        raise HTTPException(
            status_code=Config.HttpStatus.CONFLICT,
            detail="활성 가격 정책이 같은 범위에 이미 존재합니다.",
        )


def _policy_result(policy: RestaurantPricingPolicy) -> ResolvedPrice:
    return ResolvedPrice(
        restaurant_id=policy.restaurant_id,
        price=policy.price,
        policy_type=cast(PricingPolicyType, policy.policy_type),
        pricing_policy_id=policy.id,
        meal_type=MealTypeSchema(policy.meal_type.name) if policy.meal_type else None,
        served_date=policy.served_date,
        source="pricing_policy",
    )


async def resolve_price(
    db: AsyncSession,
    *,
    restaurant_id: int,
    meal_type: MealTypeSchema | None = None,
    served_date: date | None = None,
) -> ResolvedPrice:
    """Resolve price using date-specific > meal-type fixed > restaurant fixed precedence."""
    meal_type_row = await get_meal_type_by_name(db, meal_type)

    if served_date is not None:
        stmt = select(RestaurantPricingPolicy).where(
            RestaurantPricingPolicy.restaurant_id == restaurant_id,
            RestaurantPricingPolicy.policy_type == "date_specific",
            RestaurantPricingPolicy.served_date == served_date,
            RestaurantPricingPolicy.is_active.is_(True),
        )
        if meal_type_row is None:
            stmt = stmt.where(RestaurantPricingPolicy.meal_type_id.is_(None))
        else:
            stmt = stmt.where(
                or_(
                    RestaurantPricingPolicy.meal_type_id == meal_type_row.id,
                    RestaurantPricingPolicy.meal_type_id.is_(None),
                )
            ).order_by(RestaurantPricingPolicy.meal_type_id.is_(None))
        date_policy = await db.scalar(stmt)
        if date_policy is not None:
            return _policy_result(date_policy)

    if meal_type_row is not None:
        meal_policy = await db.scalar(
            select(RestaurantPricingPolicy).where(
                RestaurantPricingPolicy.restaurant_id == restaurant_id,
                RestaurantPricingPolicy.policy_type == "meal_type_fixed",
                RestaurantPricingPolicy.meal_type_id == meal_type_row.id,
                RestaurantPricingPolicy.served_date.is_(None),
                RestaurantPricingPolicy.is_active.is_(True),
            )
        )
        if meal_policy is not None:
            return _policy_result(meal_policy)

    restaurant_policy = await db.scalar(
        select(RestaurantPricingPolicy).where(
            RestaurantPricingPolicy.restaurant_id == restaurant_id,
            RestaurantPricingPolicy.policy_type == "restaurant_fixed",
            RestaurantPricingPolicy.meal_type_id.is_(None),
            RestaurantPricingPolicy.served_date.is_(None),
            RestaurantPricingPolicy.is_active.is_(True),
        )
    )
    if restaurant_policy is not None:
        return _policy_result(restaurant_policy)

    restaurant = await db.get(Restaurant, restaurant_id)
    if restaurant is not None and restaurant.price is not None:
        return ResolvedPrice(
            restaurant_id=restaurant_id,
            price=restaurant.price,
            policy_type="restaurant_fixed",
            source=cast(Literal["legacy_restaurant_price"], "legacy_restaurant_price"),
        )

    return ResolvedPrice(restaurant_id=restaurant_id, price=None)
