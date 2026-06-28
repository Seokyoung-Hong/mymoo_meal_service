"""식당 관리 API 모듈

이 모듈은 FastAPI를 기반으로 식당 관련 CRUD API를 제공합니다.
사용자는 식당을 등록, 승인, 거절, 조회, 삭제할 수 있으며,
페이징을 지원하여 여러 식당을 효율적으로 조회할 수 있습니다.

API 목록:
    - `POST /restaurants/requests`: 새로운 식당 등록 요청을 생성합니다.
    - `POST /restaurants/requests/{request_id}/approval`: 식당 등록 요청을 승인합니다.
    - `POST /restaurants/requests/{request_id}/rejection`: 식당 등록 요청을 거절합니다.
    - `GET /restaurants/requests/{request_id}`: 특정 식당 등록 요청을 조회합니다.
    - `DELETE /restaurants/requests/{request_id}`: 특정 식당 등록 요청을 삭제합니다.
    - `GET /restaurants/{restaurant_id}`: 특정 식당 정보를 조회합니다.
    - `DELETE /restaurants/{restaurant_id}`: 특정 식당을 삭제합니다.
    - `GET /restaurants/`: 모든 식당 데이터를 페이징하여 조회합니다.

이 모듈은 다음과 같은 주요 유틸리티 함수를 활용합니다:
    - `build_location_schema`: 위치 정보를 변환하는 함수
    - `build_operating_hours_entries`: 운영시간 데이터를 변환하는 함수
    - `fetch_operating_hours_dict`: 운영시간 데이터를 조회하는 함수
    - `get_restaurant_or_404`: 특정 식당 정보를 조회하고 없을 경우 404 오류를 반환하는 함수
    - `get_submission_or_404`: 특정 식당 등록 요청을 조회하고 없을 경우 404 오류를 반환하는 함수

모든 API는 비동기적으로 동작하며, SQLAlchemy의 `AsyncSession`을 활용하여 데이터베이스와 통신합니다.
"""

from collections.abc import Mapping
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi_pagination import Params, add_pagination, paginate
from sqlalchemy import case, delete, false, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from httpx import AsyncClient

from app.config import logger, Config
from app.models.meals import Meal
from app.models.restaurants import (
    OperatingHours,
    Restaurant,
    RestaurantSubmission,
)
from app.models.user import User
from app.schemas.base import BaseSchema
from app.schemas.meals import MealResponse
from app.schemas.meals import MealType as MealTypeSchema
from app.schemas.pagination import CustomPage
from app.schemas.restaurants import (
    ApproverResponse,
    EstablishmentType,
    ESTABLISHMENT_TYPE_DESCRIPTION,
    RestaurantCreateRequest,
    RestaurantManagerRequest,
    RestaurantManagerResponse,
    RestaurantRequest,
    RestaurantResponse,
    SubmissionResponse,
    TimeRange,
    RejectRestaurantRequest,
    RestaurantSubmission as RestaurantSubmissionSchema,
    RestaurantStatusUpdateRequest,
    RestaurantUpdateRequest,
)
from app.schemas.users import AdminUserSchema
from app.utils.db import (
    get_admin_user,
    get_current_user,
    get_db,
    check_admin_user,
    resolve_user_ids,
)
from app.utils.restaurants import (
    build_location_schema,
    build_operating_hours_entries,
    build_restaurant_model,
    build_restaurant_schema,
    fetch_owner_user_id,
    fetch_operating_hours_dict,
    fetch_restaurant_submission,
    get_restaurant_or_404,
    get_restaurant_with_permission,
    get_submission_or_404,
    get_submission_with_permission,
    replace_restaurant_operating_hours,
)
from app.utils.meals import apply_date_filter, parse_served_date
from app.utils.http import get_async_client
from app.services.audit import AuditLogEntry, add_audit_log, request_id_from_request

router = APIRouter(prefix="/restaurants", tags=["Restaurant"])


def _restaurant_meal_response(meal: Meal) -> MealResponse:
    """Build the restaurant-scoped meal response using the Mymoo menu contract."""
    return MealResponse(
        id=meal.id,
        served_date=meal.served_date,
        main_menu=meal.main_menu,
        side_menus=meal.side_menus,
        image_url=meal.image_url,
        meal_type=MealTypeSchema(meal.meal_type.name),
        restaurant_id=meal.restaurant_id,
        restaurant_name=meal.restaurant.name,
        registered_at=meal.registered_at,
        updated_at=meal.updated_at,
    )


async def _ensure_restaurant_owner_or_admin(
    restaurant: Restaurant,
    current_user: User,
) -> None:
    """Allow only the restaurant owner or a global/meal admin."""
    if restaurant.owner == current_user.id:
        return

    admin_user = await check_admin_user(current_user)
    if admin_user.is_admin:
        return

    raise HTTPException(
        status_code=Config.HttpStatus.FORBIDDEN,
        detail="식당 소유자 또는 관리자 권한이 필요합니다.",
    )


def _normalized_manager_user_id(user_id: str) -> str:
    """Normalize and validate public Keycloak user_id values."""
    normalized = user_id.strip()
    if not normalized:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="user_id는 비어 있을 수 없습니다.",
        )
    return normalized


def _manager_response(manager: User) -> RestaurantManagerResponse:
    """Build a manager response without exposing local numeric IDs."""
    return RestaurantManagerResponse(user_id=manager.user_id)


def _restaurant_managers(restaurant: Restaurant) -> list[User]:
    """Return restaurant managers as concrete User models for type checkers."""
    return [cast(User, manager) for manager in restaurant.managers]


def _restaurant_audit_payload(
    restaurant: Restaurant,
    operating_hours: Mapping[str, TimeRange | None] | None = None,
    *,
    owner_user_id: str | None = None,
) -> dict[str, object]:
    """Return stable restaurant fields for audit before/after payloads."""
    return {
        "id": restaurant.id,
        "name": restaurant.name,
        "owner": restaurant.owner,
        "owner_user_id": owner_user_id,
        "is_active": restaurant.is_active,
        "establishment_type": restaurant.establishment_type,
        "price": restaurant.price,
        "location": {
            "is_campus": restaurant.is_campus,
            "building": restaurant.building_name,
            "map_links": {
                key: value
                for key, value in {
                    "naver": restaurant.naver_map_link,
                    "kakao": restaurant.kakao_map_link,
                }.items()
                if value is not None
            }
            or None,
            "latitude": restaurant.latitude,
            "longitude": restaurant.longitude,
        },
        "operating_hours": {
            key: {"start": value.start, "end": value.end}
            for key, value in (operating_hours or {}).items()
            if value is not None
        },
    }


def _submission_audit_payload(submission: RestaurantSubmission) -> dict[str, object]:
    """Return stable restaurant-submission fields for audit payloads."""
    return {
        "id": submission.id,
        "name": submission.name,
        "status": submission.status,
        "submitter": submission.submitter,
        "establishment_type": submission.establishment_type,
        "price": submission.price,
    }


async def _get_manager_user_or_404(
    user_id: str,
    db: AsyncSession,
) -> User:
    """Return an existing local User row for owner/manager assignment."""
    manager = await db.scalar(select(User).where(User.user_id == user_id))
    if manager is not None:
        return manager

    raise HTTPException(
        status_code=Config.HttpStatus.NOT_FOUND,
        detail=(
            "해당 user_id의 로컬 사용자를 찾을 수 없습니다. "
            "사용자가 먼저 로그인하거나 관리자가 /users에 등록해야 합니다."
        ),
    )


@router.get("/requests", response_model=CustomPage[RestaurantSubmissionSchema])
async def restaurant_submit_get_requests(
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[Params, Depends()],
    client: Annotated[AsyncClient, Depends(get_async_client)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """모든 식당 등록 요청을 페이징하여 조회합니다.

    사용자는 자신의 등록 요청을 조회할 수 있으며,
    관리자는 모든 등록 요청을 조회할 수 있습니다.

    Args:
        db (AsyncSession): 비동기 DB 세션 객체입니다.
        params (Params): 페이징 처리를 위한 FastAPI Pagination 객체입니다.
        current_user (User): 요청을 보낸 현재 사용자 객체입니다.
        client (AsyncClient): 비동기 HTTP 클라이언트 객체입니다.

    Returns:
        CustomPage[RestaurantSubmissionSchema]: 등록 요청 데이터 목록을 포함한 페이징된 응답 객체입니다.
    """
    logger.info("Get requests received by user: %s", current_user.id)

    # 요청자가 관리자인 경우 모든 요청 조회
    admin_user: AdminUserSchema = await check_admin_user(current_user)
    if admin_user.is_admin:
        result = await db.execute(select(RestaurantSubmission))
        submissions = result.scalars().all()
    else:
        # 요청자가 일반 사용자일 경우 자신의 요청만 조회
        result = await db.execute(
            select(RestaurantSubmission).filter(
                RestaurantSubmission.submitter == current_user.id
            )
        )
        submissions = result.scalars().all()

    # 2️⃣ ORM 객체 → Pydantic 변환
    submission_schemas = [
        await fetch_restaurant_submission(submission, db) for submission in submissions
    ]

    # 예시로 client를 사용한 로깅
    logger.debug("HTTP client base URL: %s", client.base_url)

    return paginate(submission_schemas, params)


@router.post("/requests", status_code=Config.HttpStatus.CREATED)
async def restaurant_submit_request(
    request_body: RestaurantRequest,
    http_request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """새로운 식당 등록 요청을 생성합니다.

    사용자는 식당 정보를 입력하여 등록 요청을 제출할 수 있으며,
    관리자가 승인하기 전까지 `pending` 상태로 유지됩니다.
    등록 요청에는 기본적인 식당 정보(이름, 위치, 운영시간 등)가 포함됩니다.

    Args:
        request_body (RestaurantRequest): 새로 등록할 식당 정보를 포함한 요청 객체입니다.
        http_request (Request): 요청 ID 감사 로그를 위한 FastAPI 요청 객체입니다.
        db (AsyncSession): 비동기 DB 세션 객체입니다.
        current_user (User): 요청을 보낸 현재 사용자 객체입니다.

    Returns:
        BaseSchema[SubmissionResponse]: 제출된 요청 정보를 포함한 응답 객체입니다.

    Raises:
        HTTPException(400): `location` 또는 `opening_time`이 누락된 경우 발생합니다.
        HTTPException(500): 서버 내부 오류로 인해 요청 처리가 실패한 경우 발생합니다.
    """
    if request_body.location is None or request_body.opening_time is None:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="location, opening_time 필드는 필수입니다.",
        )

    new_submission = RestaurantSubmission(
        name=request_body.name,
        status="pending",
        submitter=current_user.id,
        establishment_type=request_body.establishment_type,
        price=request_body.price,
        is_campus=request_body.location.is_campus,
        building_name=request_body.location.building,
        naver_map_link=(
            request_body.location.map_links.get("naver")
            if request_body.location.map_links
            else None
        ),
        kakao_map_link=(
            request_body.location.map_links.get("kakao")
            if request_body.location.map_links
            else None
        ),
        latitude=request_body.location.latitude,
        longitude=request_body.location.longitude,
    )

    try:
        db.add(new_submission)
        await db.flush()

        operation_hours_dict = {
            "opening_time": request_body.opening_time,
            "break_time": request_body.break_time,
            "breakfast_time": request_body.breakfast_time,
            "lunch_time": request_body.lunch_time,
            "dinner_time": request_body.dinner_time,
        }

        operating_hours_entries = build_operating_hours_entries(
            operation_hours_dict, submission_id=new_submission.id
        )

        db.add_all(operating_hours_entries)
        add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(http_request),
                actor_user_id=current_user.user_id,
                action="restaurant.request.create",
                resource_type="restaurant_request",
                resource_id=new_submission.id,
                before=None,
                after=_submission_audit_payload(new_submission),
            ),
        )
        await db.commit()
        await db.refresh(new_submission)

    except Exception as e:
        await db.rollback()
        logger.error("Submission 요청 처리 중 예외 발생: %s", e)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류 발생",
        ) from e

    return BaseSchema[SubmissionResponse](
        data=SubmissionResponse(request_id=new_submission.id)
    )


@router.post("/", status_code=Config.HttpStatus.CREATED)
async def create_restaurant(
    request: RestaurantCreateRequest,
    http_request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_admin_user)],
):
    """관리자가 특정 owner_user_id를 가진 식당을 직접 생성합니다."""
    _ = current_user
    if request.location is None or request.opening_time is None:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="location, opening_time 필드는 필수입니다.",
        )

    normalized_owner_user_id = request.owner_user_id.strip()
    if not normalized_owner_user_id:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="owner_user_id는 필수입니다.",
        )

    operation_hours_dict = {
        "opening_time": request.opening_time,
        "break_time": request.break_time,
        "breakfast_time": request.breakfast_time,
        "lunch_time": request.lunch_time,
        "dinner_time": request.dinner_time,
    }

    try:
        owner_user = await _get_manager_user_or_404(normalized_owner_user_id, db)
        new_restaurant = build_restaurant_model(request, owner_id=owner_user.id)
        db.add(new_restaurant)
        await db.flush()

        operating_hours_entries = build_operating_hours_entries(
            operation_hours_dict,
            restaurant_id=new_restaurant.id,
        )
        db.add_all(operating_hours_entries)
        add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(http_request),
                actor_user_id=current_user.user_id,
                action="restaurant.create",
                resource_type="restaurant",
                resource_id=new_restaurant.id,
                before=None,
                after=_restaurant_audit_payload(
                    new_restaurant,
                    operation_hours_dict,
                    owner_user_id=owner_user.user_id,
                ),
            ),
        )

        await db.commit()
        await db.refresh(new_restaurant)
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as e:
        await db.rollback()
        logger.error("Restaurant 생성 중 예외 발생: %s", e)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류 발생",
        ) from e

    operating_hours = await fetch_operating_hours_dict(
        db, restaurant_id=new_restaurant.id
    )
    return BaseSchema[RestaurantResponse](
        data=build_restaurant_schema(
            new_restaurant,
            operating_hours,
            owner_user_id=owner_user.user_id,
        )
    )


@router.post("/requests/{request_id}/approval")
async def restaurant_submit_approval(
    request_id: int,
    request: Request,
    submission: Annotated[RestaurantSubmission, Depends(get_submission_or_404)],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[AdminUserSchema, Depends(get_admin_user)],
):
    """식당 등록 요청을 승인합니다.

    관리자는 `pending` 상태의 식당 등록 요청을 승인하여 실제 식당 데이터로 저장할 수 있습니다.
    승인된 식당은 `restaurants` 테이블에 저장되며, 요청의 운영시간도 함께 복사됩니다.

    Args:
        request_id (int): 승인할 식당 등록 요청의 고유 ID입니다.
        request (Request): 요청 ID 감사 로그를 위한 FastAPI 요청 객체입니다.
        submission (RestaurantSubmission): 승인할 제출 요청 객체입니다.
        db (AsyncSession): 비동기 DB 세션 객체입니다.
        current_user (AdminUserSchema): 현재 요청을 보낸 관리자 사용자 객체입니다.

    Returns:
        BaseSchema[ApproverResponse]: 승인된 식당 정보를 포함한 응답 객체입니다.

    Raises:
        HTTPException(400): 요청이 이미 승인되었거나 거절된 경우 발생합니다.
        HTTPException(404): 해당 요청을 찾을 수 없는 경우 발생합니다.
        HTTPException(500): 서버 내부 오류로 인해 승인 처리가 실패한 경우 발생합니다.
    """
    if submission.status != "pending":
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="해당 제출 요청은 이미 처리되었습니다.",
        )

    submission.status = "approved"
    submission.reviewer = current_user.id

    new_restaurant = Restaurant(
        name=submission.name,
        owner=submission.submitter,
        establishment_type=submission.establishment_type,
        price=submission.price,
        is_campus=submission.is_campus,
        building_name=submission.building_name,
        naver_map_link=submission.naver_map_link,
        kakao_map_link=submission.kakao_map_link,
        latitude=submission.latitude,
        longitude=submission.longitude,
    )

    try:
        db.add(submission)
        db.add(new_restaurant)
        await db.flush()

        # 운영시간 복제 로직 공통화 적용
        operating_hours_result = await db.execute(
            select(OperatingHours).filter(OperatingHours.submission_id == request_id)
        )
        operating_hours = operating_hours_result.scalars().all()

        operating_hours_entries = [
            OperatingHours(
                type=oh.type,
                start_time=oh.start_time,
                end_time=oh.end_time,
                restaurant_id=new_restaurant.id,
            )
            for oh in operating_hours
        ]

        db.add_all(operating_hours_entries)
        _ = add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=current_user.user_id,
                action="restaurant.request.approve",
                resource_type="restaurant_request",
                resource_id=request_id,
                before={"status": "pending"},
                after={
                    "status": "approved",
                    "restaurant_id": new_restaurant.id,
                },
            ),
        )
        await db.commit()
        await db.refresh(new_restaurant)

    except Exception as e:
        await db.rollback()
        logger.error("Approval 처리 중 예외 발생: %s", e)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류 발생",
        ) from e

    return BaseSchema[ApproverResponse](
        data=ApproverResponse(restaurant_id=new_restaurant.id)
    )


@router.post(
    "/requests/{request_id}/rejection", status_code=Config.HttpStatus.NO_CONTENT
)
async def restaurant_submit_rejection(  # noqa: PLR0913
    request_id: int,
    request: Request,
    submission: Annotated[RestaurantSubmission, Depends(get_submission_or_404)],
    request_body: RejectRestaurantRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[AdminUserSchema, Depends(get_admin_user)],
):
    """식당 등록 요청을 거절합니다.

    관리자는 `pending` 상태의 식당 등록 요청을 거절할 수 있습니다.
    거절된 요청은 `rejected` 상태로 변경되며, 거절 사유(`rejection_message`)를 입력해야 합니다.

    Args:
        request_id (int): 거절할 식당 등록 요청의 고유 ID입니다.
        request (Request): 요청 ID 감사 로그를 위한 FastAPI 요청 객체입니다.
        submission (RestaurantSubmission): 거절할 제출 요청 객체입니다.
        request_body (RejectRestaurantRequest): 거절 사유를 포함한 요청 객체입니다.
        db (AsyncSession): 비동기 DB 세션 객체입니다.
        current_user (UserSchema): 현재 요청을 보낸 관리자 사용자 객체입니다.

    Raises:
        HTTPException(400): 요청이 이미 승인되었거나 거절된 경우 발생합니다.
        HTTPException(404): 해당 요청을 찾을 수 없는 경우 발생합니다.
        HTTPException(400): 거절 사유가 입력되지 않은 경우 발생합니다.
        HTTPException(500): 서버 내부 오류로 인해 거절 처리가 실패한 경우 발생합니다.
    """
    if submission.status != "pending":
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="해당 제출 요청은 이미 처리되었습니다.",
        )

    rejection_message = request_body.message
    if not rejection_message:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="거부 사유는 필수 입력 사항입니다.",
        )

    submission.status = "rejected"
    submission.reviewer = current_user.id
    submission.rejection_message = rejection_message

    try:
        db.add(submission)
        _ = add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=current_user.user_id,
                action="restaurant.request.reject",
                resource_type="restaurant_request",
                resource_id=request_id,
                before={"status": "pending"},
                after={
                    "status": "rejected",
                    "rejection_message": rejection_message,
                },
            ),
        )
        await db.commit()

    except Exception as e:
        await db.rollback()
        logger.error("Approval 처리 중 예외 발생: %s", e)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류 발생",
        ) from e


@router.get("/requests/{request_id}")
async def restaurant_submit_get(
    request_id: int,
    submission: Annotated[
        RestaurantSubmission, Depends(get_submission_with_permission)
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """특정 식당 등록 요청을 조회합니다.

    사용자는 `request_id`를 이용하여 해당 요청의 상태 및 세부 정보를 확인할 수 있습니다.

    Args:
        request_id (int): 조회할 식당 등록 요청의 고유 ID입니다.
        submission (RestaurantSubmission): 조회된 제출 요청 객체입니다.
        db (AsyncSession): 비동기 DB 세션 객체입니다.
        current_user (User): 요청을 보낸 현재 사용자 객체입니다.

    Returns:
        BaseSchema[RestaurantSubmissionSchema]: 요청된 식당 등록 정보를 포함한 응답 객체입니다.

    Raises:
        HTTPException(404): 해당 요청을 찾을 수 없는 경우 발생합니다.
    """
    logger.info(
        "Get request received for submission_id: %s by user: %s",
        request_id,
        current_user.id,
    )

    response_data = await fetch_restaurant_submission(submission, db)

    return BaseSchema[RestaurantSubmissionSchema](data=response_data)


@router.get("/{restaurant_id}/meals", response_model=CustomPage[MealResponse])
async def list_restaurant_meals(  # noqa: PLR0913
    restaurant_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[Params, Depends()],
    date_query: Annotated[
        str | None, Query(alias="date", description="제공 날짜 (YYYY-MM-DD)")
    ] = None,
    start_date: Annotated[
        str | None, Query(description="검색 시작 날짜 (YYYY-MM-DD)")
    ] = None,
    end_date: Annotated[
        str | None, Query(description="검색 종료 날짜 (YYYY-MM-DD)")
    ] = None,
    meal_type: Annotated[MealTypeSchema | None, Query(description="식사 유형")] = None,
    type_alias: Annotated[
        MealTypeSchema | None,
        Query(alias="type", description="식사 유형 (meal_type 별칭)"),
    ] = None,
) -> CustomPage[MealResponse]:
    """특정 식당의 식사를 served_date 기준으로 조회합니다."""
    logger.info(
        "Fetching restaurant meals for restaurant_id=%d, date=%s, start_date=%s, end_date=%s",
        restaurant_id,
        date_query,
        start_date,
        end_date,
    )

    query = (
        select(Meal)
        .where(Meal.restaurant_id == restaurant_id)
        .where(Meal.restaurant.has(Restaurant.is_active.is_(True)))
        .options(selectinload(Meal.restaurant), selectinload(Meal.meal_type))
    )

    selected_meal_type = type_alias or meal_type
    if selected_meal_type:
        query = query.where(Meal.meal_type.has(name=selected_meal_type.value))

    if date_query:
        query = query.where(Meal.served_date == parse_served_date(date_query))
    else:
        query = await apply_date_filter(query, start_date, end_date)

    result = await db.execute(query)
    meals = result.scalars().all()

    response_data = [_restaurant_meal_response(meal) for meal in meals]
    return paginate(response_data, params)


@router.post("/{restaurant_id}/managers")
async def add_restaurant_manager(  # noqa: PLR0913
    restaurant_id: int,
    manager_request: RestaurantManagerRequest,
    request: Request,
    restaurant: Annotated[Restaurant, Depends(get_restaurant_or_404)],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """식당 소유자 또는 전역 관리자가 Keycloak user_id로 관리자를 추가합니다."""
    _ = restaurant_id
    await _ensure_restaurant_owner_or_admin(restaurant, current_user)
    manager_user_id = _normalized_manager_user_id(manager_request.user_id)

    try:
        manager = await _get_manager_user_or_404(manager_user_id, db)
        await db.refresh(restaurant, attribute_names=["managers"])
        existing_managers = _restaurant_managers(restaurant)

        if any(existing.id == manager.id for existing in existing_managers):
            return BaseSchema[RestaurantManagerResponse](
                data=_manager_response(manager)
            )

        before = {"managers": [existing.user_id for existing in existing_managers]}
        restaurant.managers.append(manager)
        after = {"managers": [*before["managers"], manager.user_id]}
        db.add(restaurant)
        add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=current_user.user_id,
                action="restaurant.manager.add",
                resource_type="restaurant",
                resource_id=restaurant.id,
                before=before,
                after=after,
            ),
        )
        await db.commit()
        await db.refresh(manager)
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        logger.error("Restaurant manager 추가 중 예외 발생: %s", e)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류 발생",
        ) from e

    return BaseSchema[RestaurantManagerResponse](data=_manager_response(manager))


@router.get("/{restaurant_id}/managers")
async def list_restaurant_managers(
    restaurant_id: int,
    restaurant: Annotated[Restaurant, Depends(get_restaurant_with_permission)],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """식당 소유자, 관리자 또는 전역 관리자가 관리자 user_id 목록을 조회합니다."""
    _ = (restaurant_id, db, current_user)
    managers = sorted(
        _restaurant_managers(restaurant), key=lambda manager: manager.user_id
    )
    return BaseSchema[list[RestaurantManagerResponse]](
        data=[_manager_response(manager) for manager in managers]
    )


@router.delete(
    "/{restaurant_id}/managers/{user_id}",
    status_code=Config.HttpStatus.NO_CONTENT,
)
async def remove_restaurant_manager(  # noqa: PLR0913
    restaurant_id: int,
    user_id: str,
    request: Request,
    restaurant: Annotated[Restaurant, Depends(get_restaurant_or_404)],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """식당 소유자 또는 전역 관리자가 Keycloak user_id로 관리자를 제거합니다."""
    _ = restaurant_id
    await _ensure_restaurant_owner_or_admin(restaurant, current_user)
    manager_user_id = _normalized_manager_user_id(user_id)

    try:
        await db.refresh(restaurant, attribute_names=["managers"])
        existing_managers = _restaurant_managers(restaurant)
        manager = next(
            (
                existing
                for existing in existing_managers
                if existing.user_id == manager_user_id
            ),
            None,
        )
        if manager is None:
            raise HTTPException(
                status_code=Config.HttpStatus.NOT_FOUND,
                detail="해당 식당 관리자를 찾을 수 없습니다.",
            )

        before = {"managers": [existing.user_id for existing in existing_managers]}
        restaurant.managers.remove(manager)
        after = {
            "managers": [
                existing.user_id
                for existing in existing_managers
                if existing.user_id != manager_user_id
            ]
        }
        db.add(restaurant)
        add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=current_user.user_id,
                action="restaurant.manager.remove",
                resource_type="restaurant",
                resource_id=restaurant.id,
                before=before,
                after=after,
            ),
        )
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        logger.error("Restaurant manager 삭제 중 예외 발생: %s", e)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류 발생",
        ) from e


@router.delete("/requests/{request_id}", status_code=Config.HttpStatus.NO_CONTENT)
async def restaurant_submit_delete(
    request_id: int,
    request: Request,
    submission: Annotated[
        RestaurantSubmission, Depends(get_submission_with_permission)
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """특정 식당 등록 요청을 삭제합니다.

    요청자가 자신의 `pending` 상태인 등록 요청을 삭제할 수 있습니다.
    승인되거나 거절된 요청은 삭제할 수 없습니다.

    Args:
        request_id (int): 삭제할 식당 등록 요청의 고유 ID입니다.
        request (Request): 요청 ID 감사 로그를 위한 FastAPI 요청 객체입니다.
        submission (RestaurantSubmission): 조회된 제출 요청 객체입니다.
        db (AsyncSession): 비동기 DB 세션 객체입니다.
        current_user (User): 요청을 보낸 현재 사용자 객체입니다.

    Raises:
        HTTPException(403): 요청을 삭제할 권한이 없는 경우 발생합니다.
        HTTPException(404): 해당 요청을 찾을 수 없는 경우 발생합니다.
    """
    logger.info(
        "Delete request received for submission_id: %s by user: %s",
        request_id,
        current_user.id,
    )

    before = _submission_audit_payload(submission)
    await db.delete(submission)
    add_audit_log(
        db,
        AuditLogEntry(
            request_id=request_id_from_request(request),
            actor_user_id=current_user.user_id,
            action="restaurant.request.delete",
            resource_type="restaurant_request",
            resource_id=request_id,
            before=before,
            after=None,
        ),
    )
    await db.commit()
    logger.info("Submission with id %s deleted successfully", request_id)


@router.get("/{restaurant_id}")
async def get_restaurant(
    restaurant_id: int,
    restaurant: Annotated[Restaurant, Depends(get_restaurant_or_404)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """특정 식당 정보를 조회합니다.

    `restaurant_id`를 이용하여 해당 식당의 기본 정보와 운영 시간을 조회합니다.

    Args:
        restaurant_id (int): 조회할 식당의 고유 ID입니다.
        restaurant (Restaurant): 조회된 식당 객체입니다.
        db (AsyncSession): 비동기 DB 세션 객체입니다.

    Returns:
        BaseSchema[RestaurantResponse]: 조회된 식당 정보를 포함한 응답 객체입니다.

    Raises:
        HTTPException(404): 해당 식당을 찾을 수 없는 경우 발생합니다.
    """
    if not restaurant.is_active:
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND,
            detail="해당 식당이 존재하지 않습니다.",
        )

    operating_hours_dict = await fetch_operating_hours_dict(
        db, restaurant_id=restaurant_id
    )
    owner_user_id = await fetch_owner_user_id(db, restaurant.owner)
    logger.debug(
        "Found %s operating hours for restaurant id %s",
        len(operating_hours_dict),
        restaurant_id,
    )

    response_data = RestaurantResponse(
        id=restaurant.id,
        name=restaurant.name,
        owner=restaurant.owner,
        owner_user_id=owner_user_id,
        is_active=restaurant.is_active,
        establishment_type=cast(EstablishmentType, restaurant.establishment_type),
        price=restaurant.price,
        location=build_location_schema(
            is_campus=restaurant.is_campus,
            building=restaurant.building_name,
            naver_link=restaurant.naver_map_link,
            kakao_link=restaurant.kakao_map_link,
            lat=restaurant.latitude,
            lon=restaurant.longitude,
        ),
        opening_time=operating_hours_dict.get("opening_time"),
        break_time=operating_hours_dict.get("break_time"),
        breakfast_time=operating_hours_dict.get("breakfast_time"),
        lunch_time=operating_hours_dict.get("lunch_time"),
        dinner_time=operating_hours_dict.get("dinner_time"),
    )

    return BaseSchema[RestaurantResponse](data=response_data)


@router.patch("/{restaurant_id}/status")
async def update_restaurant_status(  # noqa: PLR0913
    restaurant_id: int,
    status_update: RestaurantStatusUpdateRequest,
    request: Request,
    restaurant: Annotated[Restaurant, Depends(get_restaurant_or_404)],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[AdminUserSchema, Depends(get_admin_user)],
):
    """관리자가 식당 활성화 상태를 변경합니다."""
    _ = restaurant_id
    before = {"is_active": restaurant.is_active}
    restaurant.is_active = status_update.is_active
    after = {"is_active": restaurant.is_active}

    try:
        db.add(restaurant)
        add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=current_user.user_id,
                action="restaurant.status.update",
                resource_type="restaurant",
                resource_id=restaurant.id,
                before=before,
                after=after,
            ),
        )
        await db.commit()
        await db.refresh(restaurant)
    except Exception as e:
        await db.rollback()
        logger.error("Restaurant status 수정 중 예외 발생: %s", e)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류 발생",
        ) from e

    operating_hours = await fetch_operating_hours_dict(db, restaurant_id=restaurant.id)
    return BaseSchema[RestaurantResponse](
        data=build_restaurant_schema(
            restaurant,
            operating_hours,
            owner_user_id=await fetch_owner_user_id(db, restaurant.owner),
        )
    )


@router.patch("/{restaurant_id}")
async def update_restaurant(  # noqa: PLR0913
    restaurant_id: int,
    request: RestaurantUpdateRequest,
    http_request: Request,
    restaurant: Annotated[Restaurant, Depends(get_restaurant_or_404)],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_admin_user)],
):
    """관리자가 저장된 식당 정보를 수정합니다."""
    _ = (restaurant_id, current_user)
    if request.location is None or request.opening_time is None:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="location, opening_time 필드는 필수입니다.",
        )

    location = request.location
    operation_hours_dict = {
        "opening_time": request.opening_time,
        "break_time": request.break_time,
        "breakfast_time": request.breakfast_time,
        "lunch_time": request.lunch_time,
        "dinner_time": request.dinner_time,
    }
    before_operating_hours = await fetch_operating_hours_dict(
        db, restaurant_id=restaurant.id
    )
    before = _restaurant_audit_payload(
        restaurant,
        before_operating_hours,
        owner_user_id=await fetch_owner_user_id(db, restaurant.owner),
    )

    try:
        owner_user_id = request.owner_user_id.strip() if request.owner_user_id else None
        if owner_user_id is not None and not owner_user_id:
            raise HTTPException(
                status_code=Config.HttpStatus.BAD_REQUEST,
                detail="owner_user_id는 비어 있을 수 없습니다.",
            )

        resolved_owner = None
        if owner_user_id is not None:
            resolved_owner = await _get_manager_user_or_404(owner_user_id, db)
        else:
            resolved_owner = await db.get(User, restaurant.owner)
            if resolved_owner is None:
                raise HTTPException(
                    status_code=Config.HttpStatus.NOT_FOUND,
                    detail="기존 owner 사용자를 찾을 수 없습니다.",
                )

        build_restaurant_model(request, owner_id=resolved_owner.id)

        restaurant.name = request.name
        restaurant.owner = resolved_owner.id
        restaurant.establishment_type = request.establishment_type
        restaurant.price = request.price
        restaurant.is_campus = location.is_campus
        restaurant.building_name = location.building
        restaurant.naver_map_link = (location.map_links or {}).get("naver")
        restaurant.kakao_map_link = (location.map_links or {}).get("kakao")
        restaurant.latitude = location.latitude
        restaurant.longitude = location.longitude

        await replace_restaurant_operating_hours(
            db,
            restaurant_id=restaurant.id,
            operation_hours_dict=operation_hours_dict,
        )

        db.add(restaurant)
        add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(http_request),
                actor_user_id=current_user.user_id,
                action="restaurant.update",
                resource_type="restaurant",
                resource_id=restaurant.id,
                before=before,
                after=_restaurant_audit_payload(
                    restaurant,
                    operation_hours_dict,
                    owner_user_id=resolved_owner.user_id,
                ),
            ),
        )
        await db.commit()
        await db.refresh(restaurant)
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as e:
        await db.rollback()
        logger.error("Restaurant 수정 중 예외 발생: %s", e)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류 발생",
        ) from e

    operating_hours = await fetch_operating_hours_dict(db, restaurant_id=restaurant.id)
    return BaseSchema[RestaurantResponse](
        data=build_restaurant_schema(
            restaurant,
            operating_hours,
            owner_user_id=resolved_owner.user_id,
        )
    )


@router.delete("/{restaurant_id}", status_code=Config.HttpStatus.NO_CONTENT)
async def delete_restaurant(
    restaurant_id: int,
    request: Request,
    restaurant: Annotated[Restaurant, Depends(get_restaurant_or_404)],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """특정 식당을 삭제합니다.

    식당 소유자는 본인이 등록한 식당을 삭제할 수 있습니다.
    식당 삭제 시 운영시간 정보도 함께 삭제됩니다.

    Args:
        restaurant_id (int): 삭제할 식당의 고유 ID입니다.
        request (Request): 요청 ID 감사 로그를 위한 FastAPI 요청 객체입니다.
        restaurant (Restaurant): 조회된 식당 객체입니다.
        db (AsyncSession): 비동기 DB 세션 객체입니다.
        current_user (User): 요청을 보낸 현재 사용자 객체입니다.

    Raises:
        HTTPException(403): 해당 식당을 삭제할 권한이 없는 경우 발생합니다.
        HTTPException(404): 해당 식당을 찾을 수 없는 경우 발생합니다.
        HTTPException(500): 서버 내부 오류로 인해 삭제 처리가 실패한 경우 발생합니다.
    """
    logger.info(
        "Delete request received for restaurant_id: %s by user: %s",
        restaurant_id,
        current_user.id,
    )

    try:
        await _ensure_restaurant_owner_or_admin(restaurant, current_user)
        operating_hours = await fetch_operating_hours_dict(
            db, restaurant_id=restaurant_id
        )
        before = _restaurant_audit_payload(
            restaurant,
            operating_hours,
            owner_user_id=await fetch_owner_user_id(db, restaurant.owner),
        )
        # 운영 시간 한 번에 삭제
        await db.execute(
            delete(OperatingHours).where(OperatingHours.restaurant_id == restaurant_id)
        )

        # 식당 삭제
        await db.execute(delete(Restaurant).where(Restaurant.id == restaurant_id))
        add_audit_log(
            db,
            AuditLogEntry(
                request_id=request_id_from_request(request),
                actor_user_id=current_user.user_id,
                action="restaurant.delete",
                resource_type="restaurant",
                resource_id=restaurant_id,
                before=before,
                after=None,
            ),
        )

        # 트랜잭션 커밋
        await db.commit()

        logger.info(
            "Restaurant %s deleted successfully by user %s",
            restaurant_id,
            current_user.id,
        )

    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        logger.error(
            "Error occurred while deleting restaurant %s by user %s: %s",
            restaurant_id,
            current_user.id,
            e,
        )
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류 발생",
        ) from e


@router.get("/", response_model=CustomPage[RestaurantResponse])
async def get_restaurants(  # noqa: C901, PLR0913
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[Params, Depends()],
    owner_user_id: str = Query(None, description="식당 소유자 user_id"),
    manager_user_id: str = Query(None, description="식당 관리자 user_id"),
    name: str = Query(None, description="식당 이름 (부분 일치)"),
    establishment_type: Annotated[
        EstablishmentType | None,
        Query(description=ESTABLISHMENT_TYPE_DESCRIPTION),
    ] = None,
    is_campus: bool = Query(None, description="캠퍼스 내 식당 여부(true|false)"),
    include_inactive: bool = Query(False, description="비활성 식당 포함 여부"),
    authorization: str | None = Header(None),
    x_user_id: str = Header(None),
):
    """모든 식당 데이터를 페이징하여 조회합니다.

    Args:
        db (AsyncSession): 비동기 DB 세션 객체입니다.
        params (Params): 페이징 처리를 위한 FastAPI Pagination 객체입니다.
        owner_user_id (str, optional): 소유자 user_id로 필터링
        manager_user_id (str, optional): 관리자 user_id로 필터링
        name (str, optional): 식당 이름(부분 일치)으로 필터링
        establishment_type (str, optional): 식당 유형(student|fixed_menu_restaurant|fixed_korean_buffet|variable_korean_buffet)
        is_campus (bool, optional): 캠퍼스 내 식당 여부
        include_inactive (bool, optional): 비활성 식당 포함 여부
        authorization (str, optional): 관리자 검증용 Bearer 토큰
        x_user_id (str, optional): 개발 환경 관리자 검증용 사용자 ID

    Returns:
        CustomPage[RestaurantResponse]: 식당 데이터 목록을 포함한 페이징된 응답 객체입니다.
    """
    if include_inactive:
        await get_admin_user(db, authorization, x_user_id)

    owner_filter_requested = owner_user_id is not None
    manager_filter_requested = manager_user_id is not None

    owner_id, manager_id = await resolve_user_ids(
        db,
        owner_user_id,
        manager_user_id,
    )
    stmt = select(Restaurant)
    if not include_inactive:
        stmt = stmt.where(Restaurant.is_active.is_(True))

    user_filters = []
    if owner_filter_requested and owner_id is not None:
        user_filters.append(Restaurant.owner == owner_id)
    if manager_filter_requested and manager_id is not None:
        user_filters.append(Restaurant.managers.any(id=manager_id))

    if owner_filter_requested or manager_filter_requested:
        if user_filters:
            stmt = stmt.where(or_(*user_filters))
        else:
            stmt = stmt.where(false())
    if name:
        stmt = stmt.where(Restaurant.name.contains(name))
        stmt = stmt.order_by(
            case((Restaurant.name == name, 0), else_=1), Restaurant.name.asc()
        )
    if establishment_type:
        stmt = stmt.where(Restaurant.establishment_type == establishment_type)
    if is_campus is not None:
        stmt = stmt.where(Restaurant.is_campus == is_campus)

    result = await db.execute(stmt)
    restaurants = result.scalars().all()

    restaurant_schemas = []
    for restaurant in restaurants:
        operating_hours_result = await db.execute(
            select(OperatingHours).filter(OperatingHours.restaurant_id == restaurant.id)
        )
        operating_hours = operating_hours_result.scalars().all()
        operating_hours_dict = {
            operating_hour.type: TimeRange(
                start=operating_hour.start_time, end=operating_hour.end_time
            )
            for operating_hour in operating_hours
        }

        logger.debug(
            "Found %s operating hours for restaurant id %s",
            len(operating_hours),
            restaurant.id,
        )

        response_data = RestaurantResponse(
            id=restaurant.id,
            name=restaurant.name,
            owner=restaurant.owner,
            owner_user_id=await fetch_owner_user_id(db, restaurant.owner),
            is_active=restaurant.is_active,
            establishment_type=cast(EstablishmentType, restaurant.establishment_type),
            price=restaurant.price,
            location=build_location_schema(
                is_campus=restaurant.is_campus,
                building=restaurant.building_name,
                naver_link=restaurant.naver_map_link,
                kakao_link=restaurant.kakao_map_link,
                lat=restaurant.latitude,
                lon=restaurant.longitude,
            ),
            opening_time=operating_hours_dict.get("opening_time"),
            break_time=operating_hours_dict.get("break_time"),
            breakfast_time=operating_hours_dict.get("breakfast_time"),
            lunch_time=operating_hours_dict.get("lunch_time"),
            dinner_time=operating_hours_dict.get("dinner_time"),
        )
        restaurant_schemas.append(response_data)
    logger.info("Total restaurants found: %d", len(restaurant_schemas))

    return paginate(restaurant_schemas, params)


add_pagination(router)
