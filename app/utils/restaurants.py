"""Restaurants 유틸리티 모듈.

이 모듈은 식당과 관련된 다양한 유틸리티 함수들을 포함하고 있습니다.
"""

from typing import Annotated, Literal, cast
from datetime import datetime

from httpx import AsyncClient
from fastapi import HTTPException, Depends
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.user import User
from app.models.restaurants import OperatingHours, Restaurant, RestaurantSubmission
from app.schemas.restaurants import (
    BUFFET_ESTABLISHMENT_TYPES,
    EstablishmentType,
    Location,
    RestaurantSchema,
    RestaurantResponse,
    RestaurantPublicResponse,
    TimeRange,
)
from app.schemas.restaurants import RestaurantSubmission as RestaurantSubmissionSchema
from app.utils.db import get_db, check_admin_user, get_current_user
from app.utils.times import get_datetime_by_string
from app.utils.http import get_async_client
from app.config import logger, Config


async def fetch_operating_hours_dict(
    db: AsyncSession,
    *,
    submission_id: int | None = None,
    restaurant_id: int | None = None,
) -> dict[str, TimeRange]:
    """submission_id나 restaurant_id를 받아 OperatingHours를 조회한 뒤, dict로 변환.

    Args:
        db (AsyncSession): 데이터베이스 세션.
        submission_id (int | None, optional): 제출 ID.
        restaurant_id (int | None, optional): 식당 ID.

    Returns:
        dict[str, TimeRange]: 운영 시간 정보를 담은 딕셔너리.
    """
    if submission_id:
        rows = await db.execute(
            select(OperatingHours).filter(OperatingHours.submission_id == submission_id)
        )
    elif restaurant_id:
        rows = await db.execute(
            select(OperatingHours).filter(OperatingHours.restaurant_id == restaurant_id)
        )
    else:
        return {}

    operating_hours = rows.scalars().all()
    return {
        oh.type: TimeRange(start=oh.start_time, end=oh.end_time)
        for oh in operating_hours
    }


async def fetch_restaurant_submission(
    submission: RestaurantSubmission,
    db: AsyncSession,
) -> RestaurantSubmissionSchema:
    """식당 등록 요청 ORM 객체를 응답 스키마로 변환합니다.

    Args:
        submission (RestaurantSubmission): 변환할 식당 등록 요청 ORM 객체.
        db (AsyncSession): 운영 시간 조회에 사용할 데이터베이스 세션.

    Returns:
        RestaurantSubmissionSchema: 운영 시간까지 포함된 식당 등록 요청 응답 스키마.
    """
    operating_hours_dict = await fetch_operating_hours_dict(
        db, submission_id=submission.id
    )
    logger.debug(
        "Found %s operating hours for submission id %s",
        len(operating_hours_dict),
        submission.id,
    )

    return RestaurantSubmissionSchema(
        status=cast(Literal["pending", "approved", "rejected"], submission.status),
        submitter=submission.submitter,
        submitted_time=submission.submitted_time,
        id=submission.id,
        name=submission.name,
        establishment_type=cast(EstablishmentType, submission.establishment_type),
        price=submission.price,
        location=build_location_schema(
            submission.is_campus,
            building=submission.building_name,
            naver_link=submission.naver_map_link,
            kakao_link=submission.kakao_map_link,
            lat=submission.latitude,
            lon=submission.longitude,
        ),
        opening_time=operating_hours_dict.get("opening_time"),
        break_time=operating_hours_dict.get("break_time"),
        breakfast_time=operating_hours_dict.get("breakfast_time"),
        lunch_time=operating_hours_dict.get("lunch_time"),
        dinner_time=operating_hours_dict.get("dinner_time"),
        reviewer=submission.reviewer,
        reviewed_time=submission.reviewed_time,
        rejection_message=submission.rejection_message,
    )


def build_map_links(
    naver_link: str | None, kakao_link: str | None
) -> dict[str, str] | None:
    """네이버와 카카오 지도 링크를 딕셔너리로 생성.

    Args:
        naver_link (str | None): 네이버 지도 링크.
        kakao_link (str | None): 카카오 지도 링크.

    Returns:
        dict | None: 지도 링크를 담은 딕셔너리 또는 None.
    """
    map_links = {}
    if naver_link:
        map_links["naver"] = naver_link
    if kakao_link:
        map_links["kakao"] = kakao_link
    return map_links if map_links else None


def build_location_schema(  # noqa: PLR0913
    is_campus: bool,
    building: str | None,
    naver_link: str | None,
    kakao_link: str | None,
    lat: float | None,
    lon: float | None,
) -> Location:
    """Location 스키마 생성 중복 제거.

    Args:
        is_campus (bool): 캠퍼스 여부.
        building (str | None): 건물 이름.
        naver_link (str | None): 네이버 지도 링크.
        kakao_link (str | None): 카카오 지도 링크.
        lat (float | None): 위도.
        lon (float | None): 경도.

    Returns:
        Location: Location 스키마.
    """
    return Location(
        is_campus=is_campus,
        building=building,
        map_links=build_map_links(naver_link, kakao_link),
        latitude=lat,
        longitude=lon,
    )


def build_restaurant_schema(
    restaurant: Restaurant,
    operating_hours: dict[str, TimeRange],
    *,
    owner_user_id: str | None = None,
) -> RestaurantResponse:
    """RestaurantResponse 스키마 생성.

    Args:
        restaurant (Restaurant): 식당 객체.
        operating_hours (dict[str, TimeRange]): 운영 시간 정보.

    Returns:
        RestaurantResponse: RestaurantResponse 스키마.
    """
    return RestaurantResponse(
        id=restaurant.id,
        name=restaurant.name,
        owner=restaurant.owner,
        owner_user_id=owner_user_id,
        is_active=restaurant.is_active,
        establishment_type=cast(EstablishmentType, restaurant.establishment_type),
        price=restaurant.price,
        location=Location(
            is_campus=restaurant.is_campus,
            building=restaurant.building_name,
            map_links=build_map_links(
                restaurant.naver_map_link, restaurant.kakao_map_link
            ),
            latitude=restaurant.latitude,
            longitude=restaurant.longitude,
        ),
        opening_time=operating_hours.get("opening_time"),
        break_time=operating_hours.get("break_time"),
        breakfast_time=operating_hours.get("breakfast_time"),
        lunch_time=operating_hours.get("lunch_time"),
        dinner_time=operating_hours.get("dinner_time"),
    )


def build_public_restaurant_schema(
    restaurant: Restaurant,
    operating_hours: dict[str, TimeRange],
) -> RestaurantPublicResponse:
    """Build sanitized public restaurant response data."""
    return RestaurantPublicResponse(
        id=restaurant.id,
        name=restaurant.name,
        establishment_type=cast(EstablishmentType, restaurant.establishment_type),
        price=restaurant.price,
        location=Location(
            is_campus=restaurant.is_campus,
            building=restaurant.building_name,
            map_links=build_map_links(
                restaurant.naver_map_link, restaurant.kakao_map_link
            ),
            latitude=restaurant.latitude,
            longitude=restaurant.longitude,
        ),
        opening_time=operating_hours.get("opening_time"),
        break_time=operating_hours.get("break_time"),
        breakfast_time=operating_hours.get("breakfast_time"),
        lunch_time=operating_hours.get("lunch_time"),
        dinner_time=operating_hours.get("dinner_time"),
    )


async def fetch_owner_user_id(db: AsyncSession, owner_id: int) -> str | None:
    """로컬 owner ID에 대응하는 Keycloak user_id를 조회합니다."""
    result = await db.execute(select(User.user_id).where(User.id == owner_id))
    return result.scalar_one_or_none()


def build_restaurant_model(
    restaurant_data: RestaurantSchema,
    *,
    owner_id: int,
) -> Restaurant:
    """RestaurantSchema를 ORM Restaurant 모델로 변환합니다."""
    if restaurant_data.location is None:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="location 필드는 필수입니다.",
        )

    validate_establishment_type_price(
        restaurant_data.establishment_type,
        restaurant_data.price,
    )

    return Restaurant(
        name=restaurant_data.name,
        owner=owner_id,
        establishment_type=restaurant_data.establishment_type,
        price=restaurant_data.price,
        is_campus=restaurant_data.location.is_campus,
        building_name=restaurant_data.location.building,
        naver_map_link=(restaurant_data.location.map_links or {}).get("naver"),
        kakao_map_link=(restaurant_data.location.map_links or {}).get("kakao"),
        latitude=restaurant_data.location.latitude,
        longitude=restaurant_data.location.longitude,
    )


async def replace_restaurant_operating_hours(
    db: AsyncSession,
    *,
    restaurant_id: int,
    operation_hours_dict: dict[str, TimeRange | None],
) -> None:
    """식당 운영 시간을 전체 교체합니다."""
    result = await db.execute(
        select(OperatingHours).filter(OperatingHours.restaurant_id == restaurant_id)
    )
    existing_operating_hours = result.scalars().all()
    for operating_hour in existing_operating_hours:
        await db.delete(operating_hour)

    operating_hours_entries = build_operating_hours_entries(
        operation_hours_dict,
        restaurant_id=restaurant_id,
    )
    db.add_all(operating_hours_entries)


def validate_establishment_type_price(
    establishment_type: str,
    price: int | None,
) -> None:
    """식당 유형과 가격 조합을 검증합니다."""
    if establishment_type in BUFFET_ESTABLISHMENT_TYPES and price is None:
        raise ValueError(
            "fixed_korean_buffet 및 variable_korean_buffet 식당은 price 필드가 필수입니다."
        )
    if price is not None and price <= 0:
        raise ValueError("price는 0보다 큰 정수여야 합니다.")


async def get_submission_or_404(
    request_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RestaurantSubmission:
    """Submission을 조회하고, 없으면 404 예외를 발생시킨다.

    Args:
        db (AsyncSession): 데이터베이스 세션.
        request_id (int): 요청 ID.

    Returns:
        RestaurantSubmission: 조회된 제출 객체.

    Raises:
        HTTPException: 제출 객체가 존재하지 않을 때 발생.
    """
    result = await db.execute(
        select(RestaurantSubmission).filter(RestaurantSubmission.id == request_id)
    )
    submission = result.scalars().first()
    if not submission:
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND,
            detail="해당 제출 요청이 존재하지 않습니다.",
        )
    logger.info("Submission with id %s found", request_id)
    logger.debug("Submission: %s", submission)
    return submission


async def get_submission_with_permission(
    request_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    client: Annotated[AsyncClient, Depends(get_async_client)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> RestaurantSubmission:
    """제출 요청을 조회하면서, 존재 여부와 권한을 개별적으로 검증하는 함수.

    - 제출 요청이 존재하지 않으면 404 반환.
    - 사용자가 해당 제출 요청을 작성했거나, 관리자 권한이 있으면 반환.
    - 요청이 존재하지만 권한이 없으면 403 반환.

    Args:
        request_id (int): 요청 ID.
        db (AsyncSession): 비동기 DB 세션.
        client (AsyncClient): 비동기 HTTP 클라이언트.
        current_user (User): 현재 사용자.

    Returns:
        RestaurantSubmission: 조회된 제출 요청 객체.

    Raises:
        HTTPException(404): 제출 요청이 존재하지 않을 때.
        HTTPException(403): 제출 요청이 존재하지만, 권한이 없을 때.
    """
    # 1️⃣ ✅ **제출 요청 존재 여부 확인** (권한 상관없이)
    result = await db.execute(
        select(RestaurantSubmission)
        .filter(RestaurantSubmission.id == request_id)
        .options(joinedload(RestaurantSubmission.submitter_user))
    )
    submission = result.scalars().first()

    if submission is None:
        logger.warning("Submission not found: %s", request_id)
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND,
            detail="해당 제출 요청을 찾을 수 없습니다.",
        )

    # 2️⃣ ✅ **권한 확인 (작성자 or 관리자)**
    admin_user = await check_admin_user(current_user)
    if submission.submitter == current_user.id or admin_user.is_admin:
        logger.info(
            "Permission granted for user %s on submission %s",
            current_user.id,
            request_id,
        )
        return submission

    # 3️⃣ ✅ **요청이 존재하지만 권한이 없는 경우**
    raise HTTPException(
        status_code=Config.HttpStatus.FORBIDDEN,
        detail="해당 제출 요청에 대한 권한이 없습니다.",
    )


async def get_restaurant_or_404(
    db: Annotated[AsyncSession, Depends(get_db)],
    restaurant_id: int,
) -> Restaurant:
    """Restaurant를 조회하고, 없으면 404 예외를 발생시킨다.

    Args:
        db (AsyncSession): 데이터베이스 세션.
        restaurant_id (int): 식당 ID.

    Returns:
        Restaurant: 조회된 식당 객체.

    Raises:
        HTTPException: 식당 객체가 존재하지 않을 때 발생.
    """
    logger.info("Get request received for restaurant_id: %s", restaurant_id)

    result = await db.execute(select(Restaurant).filter(Restaurant.id == restaurant_id))
    restaurant = result.scalars().first()
    if not restaurant:
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND,
            detail="해당 식당이 존재하지 않습니다.",
        )
    return restaurant


async def get_restaurant_with_permission(
    restaurant_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Restaurant:
    """식당을 조회하면서, 동시에 사용자 권한을 확인하는 함수.

    - 식당이 존재하지 않으면 404 반환.
    - 사용자가 식당 소유자, 관리자이거나 전역 관리자 권한이 있으면 반환.
    - 식당이 존재하지만 권한이 없으면 403 반환.

    Args:
        restaurant_id (int): 조회할 식당 ID.
        db (AsyncSession): 비동기 데이터베이스 세션.
        client (AsyncClient): 비동기 HTTP 클라이언트.
        current_user (User): 현재 사용자 객체.

    Returns:
        Restaurant: 조회된 식당 객체.

    Raises:
        HTTPException(404): 해당 식당이 존재하지 않을 경우.
        HTTPException(403): 식당이 존재하지만 접근 권한이 없는 경우.
    """
    logger.info(
        "Checking permission for user %s on restaurant %s",
        current_user.id,
        restaurant_id,
    )

    # ✅ 1️⃣ 식당 존재 여부 확인
    result = await db.execute(
        select(Restaurant)
        .filter(Restaurant.id == restaurant_id)
        .options(joinedload(Restaurant.managers))  # managers 관계 미리 로드
    )
    restaurant = result.scalars().first()

    if restaurant is None:
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND,
            detail="해당 식당이 존재하지 않습니다.",
        )

    # ✅ 2️⃣ 권한 확인 (owner/manager → admin 순서, 필요할 때만 admin 체크)
    is_owner_or_manager = restaurant.owner == current_user.id or any(
        getattr(manager, "id", None) == current_user.id
        for manager in restaurant.managers
    )
    if not is_owner_or_manager:
        is_owner_or_manager = (await check_admin_user(current_user)).is_admin
    if is_owner_or_manager:
        logger.info(
            "Permission granted for user %s on restaurant %s",
            current_user.id,
            restaurant_id,
        )
        return restaurant

    # ✅ 3️⃣ 식당이 존재하지만 접근 권한이 없는 경우
    logger.warning(
        "User %s has no permission for restaurant %s", current_user.id, restaurant_id
    )
    raise HTTPException(
        status_code=Config.HttpStatus.FORBIDDEN,
        detail="해당 식당에 접근할 권한이 없습니다.",
    )


def validate_time_range(key: str, value: TimeRange):
    """TimeRange 유효성 검증.

    Args:
        key (str): 시간 범위 키.
        value (TimeRange): 시간 범위 값.

    Returns:
        OperatingHours: 유효성 검증된 운영 시간 객체.

    Raises:
        HTTPException: 시간 범위가 유효하지 않을 때 발생.
    """
    value.to_datetime()

    if not (isinstance(value.start, datetime) and isinstance(value.end, datetime)):
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail=f"{key}의 시간이 올바르지 않습니다.",
        )
    if value.start >= value.end:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail=f"{key}의 시작 시간이 종료 시간보다 늦습니다.",
        )
    if not (
        get_datetime_by_string("00:00")
        <= value.start
        <= get_datetime_by_string("23:59")
    ) or not (
        get_datetime_by_string("00:00") <= value.end <= get_datetime_by_string("23:59")
    ):
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail=f"{key}의 시간이 올바르지 않습니다.",
        )

    value.to_string()
    return OperatingHours(type=key, start_time=value.start, end_time=value.end)


def build_operating_hours_entries(
    operation_hours_dict, submission_id=None, restaurant_id=None
):
    """운영시간 리스트를 생성.

    Args:
        operation_hours_dict (dict): 운영 시간 정보 딕셔너리.
        submission_id (int | None, optional): 제출 ID.
        restaurant_id (int | None, optional): 식당 ID.

    Returns:
        list: 운영 시간 객체 리스트.
    """
    operating_hours_entries = []

    for key, value in operation_hours_dict.items():
        if value:
            entry = validate_time_range(key, value)
            entry.submission_id = submission_id
            entry.restaurant_id = restaurant_id
            operating_hours_entries.append(entry)

    return operating_hours_entries
