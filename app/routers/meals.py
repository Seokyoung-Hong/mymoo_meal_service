"""식사 관련 API 모듈

이 모듈은 FastAPI를 기반으로 식사 관련 CRUD API를 제공합니다.
식사 데이터를 등록, 조회, 수정, 삭제할 수 있으며, 페이징 처리를 지원합니다.

API 목록:
    - `GET /meals`: 모든 식사 데이터를 페이징 형태로 조회합니다.
    - `GET /meals/{meal_id}`: 특정 식사 데이터를 조회합니다.
    - `GET /meals/restaurant/{restaurant_id}`: 특정 식당의 식사 데이터를 조회합니다.
    - `POST /meals/{restaurant_id}`: 새로운 식사를 등록합니다.
    - `DELETE /meals/{meal_id}`: 특정 식사 데이터를 삭제합니다.
    - `DELETE /meals/{meal_id}/menus`: 특정 식사의 메뉴를 삭제합니다.
    - `PATCH /meals/{meal_id}/menus`: 특정 식사의 메뉴를 수정합니다.

사용자 인증이 필요한 API의 경우, 요청자의 권한을 검증하며,
식당 관리자인 경우에만 등록, 수정, 삭제 기능을 수행할 수 있습니다.

이 모듈은 다음과 같은 주요 유틸리티 함수를 활용합니다:
    - `apply_date_filter`: 날짜 필터링을 적용하는 함수
    - `check_restaurant_permission`: 사용자의 식당 접근 권한을 확인하는 함수
    - `get_meal_type`: 식사 유형을 조회하는 함수
    - `register_meal_transaction`: 식사를 데이터베이스에 등록하는 트랜잭션 처리 함수
    - `delete_meal_transaction`: 식사를 삭제하는 트랜잭션 처리 함수
    - `update_meal_menu_transaction`: 식사 메뉴를 수정하는 트랜잭션 처리 함수
    - `update_meal_menu`: 식사 메뉴를 추가하는 로직
    - `delete_meal_menu`: 식사 메뉴를 삭제하는 로직

모든 API는 비동기적으로 동작하며, SQLAlchemy의 `AsyncSession`을 활용하여 데이터베이스와 통신합니다.
"""

from typing import Annotated, Optional

from httpx import AsyncClient
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi_pagination import Params, add_pagination, paginate
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import aliased, selectinload
from sqlalchemy.sql import over

from app.config import Config, logger
from app.models.meals import Meal
from app.models.restaurants import Restaurant
from app.models.user import User
from app.schemas.base import BaseSchema
from app.schemas.meals import (
    MealEditResponse,
    MealRegister,
    MealRegisterResponse,
    MealResponse,
    MealUpdate,
    MenuEdit,
)
from app.schemas.meals import MealType as MealTypeSchema
from app.schemas.pagination import CustomPage
from app.services.audit import AuditLogEntry, request_id_from_request
from app.utils.http import get_async_client
from app.utils.db import get_current_user, get_db
from app.utils.meals import (
    apply_date_filter,
    delete_meal_menu,
    delete_meal_transaction,
    get_meal_type,
    parse_served_date,
    register_meal_transaction,
    seoul_business_date,
    update_meal_transaction,
    update_meal_menu,
    update_meal_menu_transaction,
)
from app.utils.restaurants import get_restaurant_with_permission

router = APIRouter(prefix="/meals", tags=["Meals"])


def _meal_audit_payload(meal: Meal) -> dict[str, object]:
    """Return stable meal fields for audit before/after payloads."""
    return {
        "id": meal.id,
        "restaurant_id": meal.restaurant_id,
        "served_date": meal.served_date.isoformat(),
        "main_menu": meal.main_menu,
        "side_menus": list(meal.side_menus),
        "image_url": meal.image_url,
        "meal_type": meal.meal_type.name,
    }


def _legacy_menu_from_parts(main_menu: str, side_menus: list[str]) -> list[str]:
    """Return the transitional legacy menu list from the Mymoo menu columns."""
    return [main_menu, *side_menus]


def _meal_response(meal: Meal) -> MealResponse:
    """Build the normal meal response using the new Mymoo menu contract."""
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


@router.get("", response_model=CustomPage[MealResponse])
async def list_meals(
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[Params, Depends()],
    date_query: Optional[str] = Query(
        None, alias="date", description="제공 날짜 (YYYY-MM-DD)"
    ),
    start_date: Optional[str] = Query(None, description="검색 시작 날짜 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="검색 종료 날짜 (YYYY-MM-DD)"),
    restaurant_name: Optional[str] = Query(None, description="식당 이름 (부분 일치)"),
    meal_type: Optional[MealTypeSchema] = Query(None, description="식사 유형"),
    type_alias: Optional[MealTypeSchema] = Query(
        None, alias="type", description="식사 유형 (meal_type 별칭)"
    ),
) -> CustomPage[MealResponse]:
    """모든 식사 데이터를 페이징 형태로 반환합니다.

    특정 기간(start_date ~ end_date)에 해당하는 식사 데이터를 조회하고자 한다면
    해당 쿼리 파라미터를 사용하여 필터링할 수 있습니다. 날짜 형식은 "YYYY-MM-DD"를 사용하며,
    만약 start_date나 end_date 중 하나만 입력하면 해당 날짜 기준으로 조회가 이뤄집니다.

    Args:
        start_date (str, optional): 검색 시작 날짜 (YYYY-MM-DD). 기본값은 None입니다.
        end_date (str, optional): 검색 종료 날짜 (YYYY-MM-DD). 기본값은 None입니다.
        db (AsyncSession): 비동기 DB 세션 객체입니다.
        params (Params): 페이징 처리를 위한 파라미터로, 페이지 번호와 페이지 크기를 지정합니다.
        restaurant_name (str, optional): 식당 이름 (부분 일치). 기본값은 None입니다.
        meal_type (MealTypeSchema, optional): 식사 유형. 기본값은 None입니다.

    Returns:
        CustomPage[MealResponse]: 페이징된 MealResponse 객체 목록입니다.

    Raises:
        HTTPException: start_date 또는 end_date가 잘못된 형식일 경우 400 에러가 발생합니다.
    """
    logger.info(
        "Fetching all meals with filters: start_date=%s, end_date=%s, restaurant_name=%s, meal_type=%s",
        start_date,
        end_date,
        restaurant_name,
        meal_type,
    )

    query = select(Meal).options(
        selectinload(Meal.restaurant), selectinload(Meal.meal_type)
    )
    query = query.where(Meal.restaurant.has(Restaurant.is_active.is_(True)))

    if restaurant_name:
        query = query.where(
            Meal.restaurant.has(Restaurant.name.contains(restaurant_name))
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

    response_data = [_meal_response(meal) for meal in meals]
    return paginate(response_data, params)


@router.get("/today", response_model=CustomPage[MealResponse])
async def list_today_meals(
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[Params, Depends()],
    restaurant_name: Optional[str] = Query(None, description="식당 이름 (부분 일치)"),
    meal_type: Optional[MealTypeSchema] = Query(None, description="식사 유형"),
    type_alias: Optional[MealTypeSchema] = Query(
        None, alias="type", description="식사 유형 (meal_type 별칭)"
    ),
) -> CustomPage[MealResponse]:
    """Asia/Seoul business date에 해당하는 식사 데이터를 반환합니다."""
    today = seoul_business_date()
    logger.info("Fetching today's meals for Seoul business date: %s", today)

    query = select(Meal).options(
        selectinload(Meal.restaurant), selectinload(Meal.meal_type)
    )
    query = query.where(Meal.restaurant.has(Restaurant.is_active.is_(True)))

    if restaurant_name:
        query = query.where(
            Meal.restaurant.has(Restaurant.name.contains(restaurant_name))
        )

    selected_meal_type = type_alias or meal_type
    if selected_meal_type:
        query = query.where(Meal.meal_type.has(name=selected_meal_type.value))

    query = query.where(Meal.served_date == today)

    result = await db.execute(query)
    meals = result.scalars().all()

    response_data = [_meal_response(meal) for meal in meals]
    return paginate(response_data, params)


@router.get("/latest", response_model=CustomPage[MealResponse])
async def latest_meals_by_restaurant(
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[Params, Depends()],
    start_date: Optional[str] = Query(None, description="검색 시작 날짜 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="검색 종료 날짜 (YYYY-MM-DD)"),
    restaurant_name: Optional[str] = Query(None, description="식당 이름 (부분 일치)"),
    meal_type: Optional[MealTypeSchema] = Query(None, description="식사 유형"),
):
    """각 식당별 + 식사 유형별로 최신 식사 데이터를 1개씩 조회합니다.

    특정 기간(start_date ~ end_date)에 해당하는 식사 데이터를 조회하고자 한다면
    해당 쿼리 파라미터를 사용하여 필터링할 수 있습니다. 날짜 형식은 "YYYY-MM-DD"를 사용하며,
    만약 start_date나 end_date 중 하나만 입력하면 해당 날짜 기준으로 조회가 이뤄집니다.

    Args:
        start_date (str, optional): 검색 시작 날짜 (YYYY-MM-DD). 기본값은 None입니다.
        end_date (str, optional): 검색 종료 날짜 (YYYY-MM-DD). 기본값은 None입니다.
        db (AsyncSession): 비동기 DB 세션 객체입니다.
        params (Params): 페이징 처리를 위한 파라미터로, 페이지 번호와 페이지 크기를 지정합니다.
        restaurant_name (str, optional): 식당 이름 (부분 일치). 기본값은 None입니다.
        meal_type (MealTypeSchema, optional): 식사 유형. 기본값은 None입니다.

    Returns:
        CustomPage[MealResponse]: 페이징된 MealResponse 객체 목록입니다.

    Raises:
        HTTPException: start_date 또는 end_date가 잘못된 형식일 경우 400 에러가 발생합니다.
    """
    logger.info("Fetching latest meal per restaurant + meal_type")

    row_number = over(
        func.row_number(),
        partition_by=(Meal.restaurant_id, Meal.meal_type_id),
        order_by=(Meal.served_date.desc(), Meal.registered_at.desc()),
    ).label("rnum")

    selected = select(Meal, row_number).where(
        Meal.restaurant.has(Restaurant.is_active.is_(True))
    )

    if restaurant_name:
        selected = selected.where(
            Meal.restaurant.has(Restaurant.name.contains(restaurant_name))
        )
    if meal_type:
        selected = selected.where(Meal.meal_type.has(name=meal_type.value))

    selected = await apply_date_filter(selected, start_date, end_date)
    subquery = selected.subquery()

    meal_alias = aliased(Meal, subquery)
    query = (
        select(meal_alias)
        .where(subquery.c.rnum == 1)
        .options(
            selectinload(meal_alias.restaurant), selectinload(meal_alias.meal_type)
        )
    )

    result = await db.execute(query)
    meals = result.scalars().all()

    logger.info("Retrieved %d meals grouped by restaurant and meal_type", len(meals))

    response_data = [_meal_response(meal) for meal in meals]
    return paginate(response_data, params)


@router.get("/{meal_id:int}", response_model=BaseSchema[MealResponse])
async def get_meal(
    meal_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """특정 식사 데이터를 조회합니다.

    식사 ID를 기준으로 식사 데이터를 검색하며, 존재하지 않을 경우 404 오류를 반환합니다.
    반환된 데이터에는 식사의 메뉴, 식사 유형, 식당 정보, 등록 및 수정 날짜 등이 포함됩니다.

    Args:
        meal_id (int): 조회할 식사의 고유 ID입니다.
        db (AsyncSession): 비동기 DB 세션 객체입니다.

    Returns:
        BaseSchema[MealResponse]: 조회된 식사 데이터를 포함하는 응답 객체입니다.

    Raises:
        HTTPException(404): 주어진 ID에 해당하는 식사가 존재하지 않을 경우 발생합니다.
    """
    logger.info("Fetching meal with id: %d", meal_id)

    result = await db.execute(
        select(Meal)
        .where(Meal.id == meal_id)
        .where(Meal.restaurant.has(Restaurant.is_active.is_(True)))
        .options(selectinload(Meal.restaurant))
        .options(selectinload(Meal.meal_type))
    )
    meal = result.scalars().first()

    if not meal:
        logger.warning("Meal with id %d not found", meal_id)
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND, detail="Meal not found"
        )

    logger.info("Meal found: %d", meal.id)

    response_data = _meal_response(meal)

    return BaseSchema[MealResponse](data=response_data)


@router.get(
    "/restaurant/{restaurant_id}/latest", response_model=CustomPage[MealResponse]
)
async def latest_meal_by_restaurant(
    restaurant_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[Params, Depends()],
):
    """식당 ID를 기준으로 각 식사 유형별 최신 식사 데이터를 조회합니다.

    Args:
        restaurant_id (int): 조회할 식당의 고유 ID입니다.
        db (AsyncSession): 비동기 DB 세션 객체입니다.
        params (Params): 페이징 파라미터입니다.

    Returns:
        CustomPage[MealResponse]: 식사 유형별 최신 식사 데이터 목록입니다.

    Raises:
        HTTPException(404): 식사 데이터가 존재하지 않는 경우 발생합니다.
    """
    logger.info("Fetching latest meal for restaurant_id=%d", restaurant_id)

    row_number = over(
        func.row_number(),
        partition_by=Meal.meal_type_id,
        order_by=(Meal.served_date.desc(), Meal.registered_at.desc()),
    ).label("rnum")

    subquery = (
        select(Meal, row_number)
        .where(Meal.restaurant_id == restaurant_id)
        .where(Meal.restaurant.has(Restaurant.is_active.is_(True)))
        .options(selectinload(Meal.restaurant))
        .options(selectinload(Meal.meal_type))
        .subquery()
    )

    meal_alias = aliased(Meal, subquery)

    query = (
        select(meal_alias)
        .select_from(subquery)
        .where(subquery.c.rnum == 1)
        .options(selectinload(meal_alias.restaurant))
        .options(selectinload(meal_alias.meal_type))
    )

    result = await db.execute(query)
    meals = result.scalars().all()

    if not meals:
        raise HTTPException(status_code=404, detail="식사 데이터가 존재하지 않습니다.")

    logger.info(
        "Retrieved %d latest meals for restaurant_id=%d", len(meals), restaurant_id
    )

    response_data = [_meal_response(meal) for meal in meals]

    return paginate(response_data, params)


@router.get("/restaurant/{restaurant_id}", response_model=CustomPage[MealResponse])
async def list_meals_by_restaurant(
    restaurant_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    params: Annotated[Params, Depends()],
    start_date: Optional[str] = Query(None, description="검색 시작 날짜 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="검색 종료 날짜 (YYYY-MM-DD)"),
):
    """특정 식당의 식사 데이터를 페이징 형태로 조회합니다.

    식당 ID를 기준으로 해당 식당의 모든 식사 데이터를 검색합니다.
    또한, `start_date`와 `end_date`를 입력하면 특정 기간 동안 제공된 식사만 조회할 수 있습니다.
    날짜 형식은 `"YYYY-MM-DD"`을 사용하며, `start_date` 또는 `end_date` 중 하나만 입력하면 해당 날짜를 기준으로 조회됩니다.

    Args:
        restaurant_id (int): 조회할 식당의 고유 ID입니다.
        db (AsyncSession): 비동기 DB 세션 객체입니다.
        params (Params): 페이징 처리를 위한 FastAPI Pagination 객체입니다.
        start_date (str, optional): 검색 시작 날짜 (예: `"2024-01-01"`). 기본값은 `None`입니다.
        end_date (str, optional): 검색 종료 날짜 (예: `"2024-01-31"`). 기본값은 `None`입니다.

    Returns:
        CustomPage[MealResponse]: 해당 식당의 식사 데이터 목록을 포함하는 페이징된 응답 객체입니다.

    Raises:
        HTTPException(400): `start_date` 또는 `end_date`가 잘못된 형식일 경우 발생합니다.
    """
    logger.info(
        "Fetching meals for restaurant_id=%d with filters: start_date=%s, end_date=%s",
        restaurant_id,
        start_date,
        end_date,
    )

    query = (
        select(Meal)
        .where(Meal.restaurant_id == restaurant_id)
        .where(Meal.restaurant.has(Restaurant.is_active.is_(True)))
        .options(selectinload(Meal.restaurant))
        .options(selectinload(Meal.meal_type))
    )

    query = await apply_date_filter(query, start_date, end_date)

    result = await db.execute(query)
    meals = result.scalars().all()

    logger.info("Retrieved %d meals for restaurant_id=%d", len(meals), restaurant_id)

    response_data = [_meal_response(meal) for meal in meals]

    return paginate(response_data, params)


@router.delete("/{meal_id:int}", status_code=Config.HttpStatus.NO_CONTENT)
async def delete_meal(
    meal_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    client: Annotated[AsyncClient, Depends(get_async_client)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """특정 식사 데이터를 삭제합니다.

    주어진 `meal_id`에 해당하는 식사를 삭제합니다.
    식사 데이터가 존재하지 않거나, 권한이 없는 사용자가 요청할 경우 예외가 발생합니다.

    Args:
        meal_id (int): 삭제할 식사의 고유 ID입니다.
        db (AsyncSession): 비동기 DB 세션 객체입니다.
        client (AsyncClient): 비동기 HTTP 클라이언트 객체입니다.
        current_user (User): 요청을 보낸 현재 사용자 객체입니다.

    Raises:
        HTTPException(404): 주어진 `meal_id`에 해당하는 식사가 존재하지 않을 경우 발생합니다.
        HTTPException(403): 해당 식사를 삭제할 권한이 없을 경우 발생합니다.
    """
    logger.info("User %d attempting to delete meal %d", current_user.id, meal_id)

    # ✅ 1️⃣ Meal 조회
    result = await db.execute(
        select(Meal).where(Meal.id == meal_id).options(selectinload(Meal.meal_type))
    )
    meal = result.scalars().first()

    if not meal:
        logger.warning("Meal with id %d not found", meal_id)
        raise HTTPException(status_code=404, detail="Meal not found")

    # ✅ 2️⃣ `get_restaurant_with_permission()` 활용 → **권한 검증 & 레스토랑 객체 반환**
    await get_restaurant_with_permission(meal.restaurant_id, db, current_user)

    # ✅ 3️⃣ Meal 삭제 트랜잭션 실행
    before = _meal_audit_payload(meal)
    await delete_meal_transaction(
        db,
        meal,
        AuditLogEntry(
            request_id=request_id_from_request(request),
            actor_user_id=current_user.user_id,
            action="meal.delete",
            resource_type="meal",
            resource_id=meal.id,
            before=before,
            after=None,
        ),
    )
    logger.info("Meal %d successfully deleted by user %d", meal_id, current_user.id)


@router.post("/{restaurant_id:int}", status_code=Config.HttpStatus.CREATED)
async def register_meal(
    restaurant_id: int,
    meal_register: MealRegister,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    client: Annotated[AsyncClient, Depends(get_async_client)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """새로운 식사를 등록합니다.

    주어진 `restaurant_id`에 해당하는 식당에 새로운 식사를 추가합니다.
    식사 등록 시 메뉴, 식사 유형을 지정해야 하며, 권한이 없는 사용자가 요청하면 예외가 발생합니다.

    Args:
        restaurant_id (int): 식사를 등록할 식당의 고유 ID입니다.
        meal_register (MealRegister): 등록할 식사 정보가 포함된 요청 객체입니다.
        db (AsyncSession): 비동기 DB 세션 객체입니다.
        client (AsyncClient): 비동기 HTTP 클라이언트 객체입니다.
        current_user (User): 요청을 보낸 현재 사용자 객체입니다.

    Returns:
        BaseSchema[MealRegisterResponse]: 등록된 식사 정보를 포함한 응답 객체입니다.

    Raises:
        HTTPException(403): 해당 식당에 식사를 등록할 권한이 없을 경우 발생합니다.
        HTTPException(500): 데이터베이스 오류로 인해 식사 등록에 실패한 경우 발생합니다.
    """
    logger.info(
        "User %d attempting to register meal for restaurant %d",
        current_user.id,
        restaurant_id,
    )

    restaurant = await get_restaurant_with_permission(restaurant_id, db, current_user)
    if not restaurant.is_active:
        raise HTTPException(
            status_code=Config.HttpStatus.CONFLICT,
            detail="비활성 식당에는 식사를 등록할 수 없습니다.",
        )
    meal_type = await get_meal_type(db, meal_register.meal_type)

    new_meal = Meal(
        restaurant_id=restaurant_id,
        served_date=meal_register.served_date,
        main_menu=meal_register.main_menu,
        side_menus=meal_register.side_menus,
        image_url=meal_register.image_url,
        menu=_legacy_menu_from_parts(
            meal_register.main_menu,
            meal_register.side_menus,
        ),
        meal_type_id=meal_type.id,
    )

    await register_meal_transaction(
        db,
        new_meal,
        lambda saved_meal: AuditLogEntry(
            request_id=request_id_from_request(request),
            actor_user_id=current_user.user_id,
            action="meal.create",
            resource_type="meal",
            resource_id=saved_meal.id,
            before=None,
            after={
                "id": saved_meal.id,
                "restaurant_id": saved_meal.restaurant_id,
                "served_date": saved_meal.served_date.isoformat(),
                "main_menu": saved_meal.main_menu,
                "side_menus": list(saved_meal.side_menus),
                "image_url": saved_meal.image_url,
                "meal_type": meal_type.name,
            },
        ),
    )

    logger.info(
        "Meal %d successfully registered by user %d", new_meal.id, current_user.id
    )

    response_data = MealRegisterResponse(
        id=new_meal.id,
        restaurant_id=new_meal.restaurant_id,
        meal_type=MealTypeSchema(meal_type.name),
        served_date=new_meal.served_date,
        main_menu=new_meal.main_menu,
        side_menus=new_meal.side_menus,
        image_url=new_meal.image_url,
        registered_at=new_meal.registered_at,
    )

    return BaseSchema[MealRegisterResponse](data=response_data)


@router.patch("/{meal_id:int}", response_model=BaseSchema[MealResponse])
async def update_meal(
    meal_id: int,
    meal_update: MealUpdate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    client: Annotated[AsyncClient, Depends(get_async_client)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """특정 식사 데이터를 전체 수정합니다."""
    logger.info("User %d attempting to update meal %d", current_user.id, meal_id)

    result = await db.execute(
        select(Meal)
        .where(Meal.id == meal_id)
        .options(selectinload(Meal.restaurant))
        .options(selectinload(Meal.meal_type))
    )
    meal = result.scalars().first()

    if not meal:
        logger.warning("Meal with id %d not found", meal_id)
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND,
            detail="Meal not found",
        )

    await get_restaurant_with_permission(meal.restaurant_id, db, current_user)
    if meal_update.restaurant_id is not None:
        await get_restaurant_with_permission(
            meal_update.restaurant_id, db, current_user
        )
    meal_type = None
    if meal_update.meal_type is not None:
        meal_type = await get_meal_type(db, meal_update.meal_type)

    before = _meal_audit_payload(meal)
    after = {
        **before,
        **meal_update.model_dump(
            mode="json",
            exclude_unset=True,
            exclude={"meal_type"},
        ),
    }
    if meal_type is not None:
        after["meal_type"] = meal_type.name

    await update_meal_transaction(
        db,
        meal,
        meal_type_id=meal_type.id if meal_type is not None else None,
        meal_update=meal_update,
        audit_entry=AuditLogEntry(
            request_id=request_id_from_request(request),
            actor_user_id=current_user.user_id,
            action="meal.update",
            resource_type="meal",
            resource_id=meal.id,
            before=before,
            after=after,
        ),
    )

    result = await db.execute(
        select(Meal)
        .where(Meal.id == meal_id)
        .options(selectinload(Meal.restaurant))
        .options(selectinload(Meal.meal_type))
    )
    updated_meal = result.scalars().first()
    if updated_meal is None:
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND,
            detail="Meal not found",
        )

    response_data = _meal_response(updated_meal)
    return BaseSchema[MealResponse](data=response_data)


@router.delete("/{meal_id:int}/menus", status_code=Config.HttpStatus.NO_CONTENT)
async def delete_menu(
    meal_id: int,
    menu_delete: MenuEdit,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    client: Annotated[AsyncClient, Depends(get_async_client)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """특정 식사의 메뉴를 삭제합니다.

    `meal_id`에 해당하는 식사에서 지정된 메뉴를 삭제합니다.
    메뉴 삭제 후 식사 데이터를 업데이트하며, 권한이 없는 사용자가 요청하면 예외가 발생합니다.

    Args:
        meal_id (int): 메뉴를 삭제할 식사의 고유 ID입니다.
        menu_delete (MenuEdit): 삭제할 메뉴 목록을 포함한 요청 객체입니다.
        db (AsyncSession): 비동기 DB 세션 객체입니다.
        client (AsyncClient): 비동기 HTTP 클라이언트 객체입니다.
        current_user (User): 요청을 보낸 현재 사용자 객체입니다.

    Raises:
        HTTPException(404): 주어진 `meal_id`에 해당하는 식사가 존재하지 않을 경우 발생합니다.
        HTTPException(403): 해당 식사의 메뉴를 삭제할 권한이 없을 경우 발생합니다.
        HTTPException(500): 데이터베이스 오류로 인해 메뉴 삭제에 실패한 경우 발생합니다.
    """
    logger.info(
        "User %d attempting to delete menu for meal %d", current_user.id, meal_id
    )

    result = await db.execute(
        select(Meal).where(Meal.id == meal_id).options(selectinload(Meal.meal_type))
    )
    meal = result.scalars().first()

    if not meal:
        logger.warning("Meal with id %d not found", meal_id)
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND, detail="Meal not found"
        )

    await get_restaurant_with_permission(meal.restaurant_id, db, current_user)

    before = _meal_audit_payload(meal)
    updated_menu = delete_meal_menu(meal, menu_delete.menu)
    after = {
        **before,
        "main_menu": updated_menu[0] if updated_menu else "",
        "side_menus": updated_menu[1:],
    }
    await update_meal_menu_transaction(
        db,
        meal,
        updated_menu,
        AuditLogEntry(
            request_id=request_id_from_request(request),
            actor_user_id=current_user.user_id,
            action="meal.menu.delete",
            resource_type="meal",
            resource_id=meal.id,
            before=before,
            after=after,
        ),
    )

    logger.info("Menu successfully deleted for meal %d", meal.id)


@router.patch("/{meal_id:int}/menus")
async def edit_meal_menu(
    meal_id: int,
    menu_edit: MenuEdit,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    client: Annotated[AsyncClient, Depends(get_async_client)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """특정 식사의 메뉴를 수정합니다.

    `meal_id`에 해당하는 식사의 기존 메뉴에 새로운 메뉴를 추가하거나 변경할 수 있습니다.
    변경된 메뉴가 즉시 반영되며, 권한이 없는 사용자가 요청하면 예외가 발생합니다.

    Args:
        meal_id (int): 메뉴를 수정할 식사의 고유 ID입니다.
        menu_edit (MenuEdit): 수정할 메뉴 목록을 포함한 요청 객체입니다.
        db (AsyncSession): 비동기 DB 세션 객체입니다.
        client (AsyncClient): 비동기 HTTP 클라이언트 객체입니다.
        current_user (User): 요청을 보낸 현재 사용자 객체입니다.

    Returns:
        BaseSchema[MealEditResponse]: 수정된 식사 정보를 포함한 응답 객체입니다.

    Raises:
        HTTPException(404): 주어진 `meal_id`에 해당하는 식사가 존재하지 않을 경우 발생합니다.
        HTTPException(403): 해당 식사의 메뉴를 수정할 권한이 없을 경우 발생합니다.
        HTTPException(500): 데이터베이스 오류로 인해 메뉴 수정에 실패한 경우 발생합니다.
    """
    logger.info("User %d attempting to edit menu for meal %d", current_user.id, meal_id)

    result = await db.execute(
        select(Meal).where(Meal.id == meal_id).options(selectinload(Meal.meal_type))
    )
    meal = result.scalars().first()

    if not meal:
        logger.warning("Meal with id %d not found", meal_id)
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND, detail="Meal not found"
        )

    await get_restaurant_with_permission(meal.restaurant_id, db, current_user)

    before = _meal_audit_payload(meal)
    meal_type_name = meal.meal_type.name
    updated_menu = update_meal_menu(meal, menu_edit.menu)
    after = {
        **before,
        "main_menu": updated_menu[0] if updated_menu else "",
        "side_menus": updated_menu[1:],
    }
    await update_meal_menu_transaction(
        db,
        meal,
        updated_menu,
        AuditLogEntry(
            request_id=request_id_from_request(request),
            actor_user_id=current_user.user_id,
            action="meal.menu.update",
            resource_type="meal",
            resource_id=meal.id,
            before=before,
            after=after,
        ),
    )

    logger.info("Menu successfully updated for meal %d", meal.id)

    response_data = MealEditResponse(
        id=meal.id,
        restaurant_id=meal.restaurant_id,
        meal_type=MealTypeSchema(meal_type_name),
        menu=meal.menu,
    )

    return BaseSchema[MealEditResponse](data=response_data)


add_pagination(router)
