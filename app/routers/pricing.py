"""Flexible restaurant pricing policy API."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Config, logger
from app.models.pricing import RestaurantPricingPolicy
from app.models.restaurants import Restaurant
from app.models.user import User
from app.schemas.base import BaseSchema
from app.schemas.meals import MealType as MealTypeSchema
from app.schemas.pricing import (
    PriceResolutionResponse,
    PricingPolicyCreate,
    PricingPolicyResponse,
    PricingPolicyStatusUpdate,
    PricingPolicyUpdate,
    PricingPolicyType,
)
from app.services.audit import AuditLogEntry, add_audit_log, request_id_from_request
from app.services.pricing import (
    ensure_no_active_duplicate,
    get_meal_type_by_name,
    pricing_policy_to_dict,
    resolve_price,
)
from app.utils.auth import optional_metrics_x_user_id
from app.utils.db import get_current_user, get_db
from app.utils.restaurants import get_restaurant_or_404, get_restaurant_with_permission

router = APIRouter(prefix="/restaurants", tags=["Pricing"])


def pricing_policy_response(policy: RestaurantPricingPolicy) -> PricingPolicyResponse:
    """Convert a pricing ORM row to its public schema."""
    return PricingPolicyResponse(
        id=policy.id,
        restaurant_id=policy.restaurant_id,
        policy_type=cast(PricingPolicyType, policy.policy_type),
        price=policy.price,
        meal_type=MealTypeSchema(policy.meal_type.name) if policy.meal_type else None,
        served_date=policy.served_date,
        is_active=policy.is_active,
        created_at=policy.created_at,
        updated_at=policy.updated_at,
    )


async def get_pricing_policy_or_404(
    db: AsyncSession,
    restaurant_id: int,
    pricing_policy_id: int,
) -> RestaurantPricingPolicy:
    """Load a restaurant pricing policy or return 404."""
    policy = await db.scalar(
        select(RestaurantPricingPolicy)
        .where(
            RestaurantPricingPolicy.id == pricing_policy_id,
            RestaurantPricingPolicy.restaurant_id == restaurant_id,
        )
        .options(selectinload(RestaurantPricingPolicy.meal_type))
    )
    if policy is None:
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND,
            detail="가격 정책을 찾을 수 없습니다.",
        )
    return policy


@router.get(
    "/{restaurant_id}/price",
    dependencies=[Depends(optional_metrics_x_user_id)],
)
async def get_restaurant_price(
    restaurant_id: int,
    restaurant: Annotated[Restaurant, Depends(get_restaurant_or_404)],
    db: Annotated[AsyncSession, Depends(get_db)],
    meal_type: Annotated[MealTypeSchema | None, Query(description="식사 유형")] = None,
    served_date: Annotated[date | None, Query(description="제공 날짜")] = None,
) -> BaseSchema[PriceResolutionResponse]:
    """Resolve a restaurant price with nullable no-match behavior."""
    _ = restaurant_id
    if not restaurant.is_active:
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND,
            detail="해당 식당이 존재하지 않습니다.",
        )

    resolved = await resolve_price(
        db,
        restaurant_id=restaurant.id,
        meal_type=meal_type,
        served_date=served_date,
    )
    return BaseSchema[PriceResolutionResponse](
        data=PriceResolutionResponse(
            restaurant_id=resolved.restaurant_id,
            price=resolved.price,
            policy_type=resolved.policy_type,
            pricing_policy_id=resolved.pricing_policy_id,
            meal_type=resolved.meal_type,
            served_date=resolved.served_date,
            source=cast(
                Literal["pricing_policy", "legacy_restaurant_price"] | None,
                resolved.source,
            ),
        )
    )


@router.get("/{restaurant_id}/pricing")
async def list_pricing_policies(
    restaurant_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[list[PricingPolicyResponse]]:
    """List pricing policies for a restaurant visible to its owner/manager/admin."""
    _ = await get_restaurant_with_permission(restaurant_id, db, current_user)
    result = await db.execute(
        select(RestaurantPricingPolicy)
        .where(RestaurantPricingPolicy.restaurant_id == restaurant_id)
        .options(selectinload(RestaurantPricingPolicy.meal_type))
        .order_by(RestaurantPricingPolicy.id)
    )
    return BaseSchema[list[PricingPolicyResponse]](
        data=[pricing_policy_response(policy) for policy in result.scalars().all()]
    )


@router.post("/{restaurant_id}/pricing", status_code=Config.HttpStatus.CREATED)
async def create_pricing_policy(
    restaurant_id: int,
    payload: PricingPolicyCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[PricingPolicyResponse]:
    """Create a pricing policy for a restaurant owner/manager/admin."""
    _ = await get_restaurant_with_permission(restaurant_id, db, current_user)
    meal_type = await get_meal_type_by_name(db, payload.meal_type)
    meal_type_id = meal_type.id if meal_type else None
    if payload.is_active:
        await ensure_no_active_duplicate(
            db,
            policy_type=payload.policy_type,
            restaurant_id=restaurant_id,
            meal_type_id=meal_type_id,
            served_date=payload.served_date,
        )

    policy = RestaurantPricingPolicy(
        restaurant_id=restaurant_id,
        policy_type=payload.policy_type,
        price=payload.price,
        meal_type_id=meal_type_id,
        served_date=payload.served_date,
        is_active=payload.is_active,
    )

    try:
        db.add(policy)
        await db.flush()
        await db.refresh(policy, attribute_names=["meal_type"])
        _ = add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=current_user.user_id,
                action="pricing_policy.create",
                resource_type="pricing_policy",
                resource_id=policy.id,
                after=pricing_policy_to_dict(policy),
            ),
        )
        await db.commit()
        await db.refresh(policy)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=Config.HttpStatus.CONFLICT,
            detail="활성 가격 정책이 같은 범위에 이미 존재합니다.",
        ) from exc
    except Exception as exc:
        await db.rollback()
        logger.error("Pricing policy 생성 중 예외 발생: %s", exc)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류 발생",
        ) from exc

    return BaseSchema[PricingPolicyResponse](data=pricing_policy_response(policy))


@router.patch("/{restaurant_id}/pricing/{pricing_policy_id}")
async def update_pricing_policy(  # noqa: PLR0913
    restaurant_id: int,
    pricing_policy_id: int,
    payload: PricingPolicyUpdate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[PricingPolicyResponse]:
    """Replace a pricing policy for a restaurant owner/manager/admin."""
    _ = await get_restaurant_with_permission(restaurant_id, db, current_user)
    policy = await get_pricing_policy_or_404(db, restaurant_id, pricing_policy_id)
    meal_type = await get_meal_type_by_name(db, payload.meal_type)
    meal_type_id = meal_type.id if meal_type else None
    if payload.is_active:
        await ensure_no_active_duplicate(
            db,
            policy_type=payload.policy_type,
            restaurant_id=restaurant_id,
            meal_type_id=meal_type_id,
            served_date=payload.served_date,
            exclude_policy_id=policy.id,
        )

    before = pricing_policy_to_dict(policy)
    policy.policy_type = payload.policy_type
    policy.price = payload.price
    policy.meal_type_id = meal_type_id
    policy.served_date = payload.served_date
    policy.is_active = payload.is_active

    try:
        db.add(policy)
        await db.flush()
        await db.refresh(policy, attribute_names=["meal_type"])
        _ = add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=current_user.user_id,
                action="pricing_policy.update",
                resource_type="pricing_policy",
                resource_id=policy.id,
                before=before,
                after=pricing_policy_to_dict(policy),
            ),
        )
        await db.commit()
        await db.refresh(policy)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=Config.HttpStatus.CONFLICT,
            detail="활성 가격 정책이 같은 범위에 이미 존재합니다.",
        ) from exc
    except Exception as exc:
        await db.rollback()
        logger.error("Pricing policy 수정 중 예외 발생: %s", exc)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류 발생",
        ) from exc

    return BaseSchema[PricingPolicyResponse](data=pricing_policy_response(policy))


@router.patch("/{restaurant_id}/pricing/{pricing_policy_id}/status")
async def update_pricing_policy_status(  # noqa: PLR0913
    restaurant_id: int,
    pricing_policy_id: int,
    payload: PricingPolicyStatusUpdate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BaseSchema[PricingPolicyResponse]:
    """Activate or deactivate a pricing policy."""
    _ = await get_restaurant_with_permission(restaurant_id, db, current_user)
    policy = await get_pricing_policy_or_404(db, restaurant_id, pricing_policy_id)
    if payload.is_active:
        await ensure_no_active_duplicate(
            db,
            policy_type=cast(PricingPolicyType, policy.policy_type),
            restaurant_id=restaurant_id,
            meal_type_id=policy.meal_type_id,
            served_date=policy.served_date,
            exclude_policy_id=policy.id,
        )

    before = pricing_policy_to_dict(policy)
    policy.is_active = payload.is_active

    try:
        db.add(policy)
        await db.flush()
        await db.refresh(policy, attribute_names=["meal_type"])
        _ = add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=current_user.user_id,
                action="pricing_policy.status.update",
                resource_type="pricing_policy",
                resource_id=policy.id,
                before=before,
                after=pricing_policy_to_dict(policy),
            ),
        )
        await db.commit()
        await db.refresh(policy)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=Config.HttpStatus.CONFLICT,
            detail="활성 가격 정책이 같은 범위에 이미 존재합니다.",
        ) from exc
    except Exception as exc:
        await db.rollback()
        logger.error("Pricing policy status 수정 중 예외 발생: %s", exc)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류 발생",
        ) from exc

    return BaseSchema[PricingPolicyResponse](data=pricing_policy_response(policy))


@router.delete(
    "/{restaurant_id}/pricing/{pricing_policy_id}",
    status_code=Config.HttpStatus.NO_CONTENT,
    response_model=None,
)
async def delete_pricing_policy(
    restaurant_id: int,
    pricing_policy_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Delete a pricing policy for a restaurant owner/manager/admin."""
    _ = await get_restaurant_with_permission(restaurant_id, db, current_user)
    policy = await get_pricing_policy_or_404(db, restaurant_id, pricing_policy_id)
    before = pricing_policy_to_dict(policy)

    try:
        await db.delete(policy)
        _ = add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=current_user.user_id,
                action="pricing_policy.delete",
                resource_type="pricing_policy",
                resource_id=pricing_policy_id,
                before=before,
            ),
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.error("Pricing policy 삭제 중 예외 발생: %s", exc)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류 발생",
        ) from exc
