"""이 모듈은 데이터베이스 세션과 사용자 인증 및 권한 확인을 위한 유틸리티 함수를 제공합니다."""

from datetime import datetime
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload


from app.config import Config, logger
from app.database import AsyncSessionLocal
from app.models.user import User
from app.models.restaurants import Restaurant
from app.services.user_service import check_admin_user
from app.schemas.users import AdminUserSchema
from app.utils.auth import (
    AuthenticatedPrincipal,
    bearer_unauthorized,
    dev_header_fallback_allowed,
    principal_from_authorization,
)


async def get_db():
    """비동기 데이터베이스 세션을 생성하고 반환합니다.

    Yields:
        AsyncSession: 비동기 데이터베이스 세션 객체
    """
    async with AsyncSessionLocal() as db:
        yield db


async def get_user_by_id(db: AsyncSession, user_id: str) -> User | None:
    """주어진 user_id로 사용자를 조회합니다."""
    return await db.scalar(select(User).where(User.user_id == user_id))


async def create_user(user_id: str, db: AsyncSession, check_existance=True) -> User:
    """사용자를 생성하는 비즈니스 로직.

    사용자가 이미 존재하는지 확인하는 옵션을 포함합니다.
    해당 옵션은 가급적 활성화해야 합니다. Keycloak Admin API 조회는 하지 않으며,
    검증된 JWT의 sub 또는 명시적으로 허용된 로컬 등록 흐름을 신뢰합니다.

    Args:
        user_id (str): 생성할 계정의 Keycloak subject
        db (AsyncSession): 비동기 데이터베이스 세션.

    Raises:
        HTTPException: 사용자 정보가 외부 서비스에 존재하지 않거나,
                        이미 존재하는 경우 오류 발생.

    Returns:
        User: 생성된 사용자 객체.
    """
    if check_existance:
        existing = await get_user_by_id(db, user_id)
        if existing:
            raise HTTPException(
                status_code=Config.HttpStatus.CONFLICT,
                detail="User already exists",
            )

    user = User(user_id=user_id)
    db.add(user)
    try:
        await db.commit()
    except SQLAlchemyError as e:
        await db.rollback()
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="DB Commit Failure",
        ) from e
    await db.refresh(user)
    return user


async def delete_user(db: AsyncSession, user_id: str):
    """사용자를 삭제하고 해당 사용자가 소유한 식당을 소프트 삭제합니다.

    Args:
        db (AsyncSession): 비동기 데이터베이스 세션
        user_id (str): 삭제할 사용자의 ID
    Raises:
        HTTPException: 사용자가 존재하지 않을 경우 404 오류 발생
    """
    stmt = select(User).where(User.user_id == user_id)  # ← DB 컬럼명에 맞게
    user = (await db.execute(stmt)).scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    result = await db.execute(
        select(Restaurant)
        .options(selectinload(Restaurant.managers))  # managers 미리 로드
        .where(Restaurant.owner == user.id)
    )
    for restaurant in result.scalars():
        restaurant.soft_delete()

    managed = await db.execute(
        select(Restaurant).join(Restaurant.managers).filter(User.id == user.id)
    )
    for restaurant in managed.scalars():
        if user in restaurant.managers:
            restaurant.managers.remove(user)

    # 💡 중간 flush로 관계 정리
    await db.flush()

    await db.delete(user)


async def get_or_create_user(
    user_id: str,
    db: AsyncSession,
) -> User:
    """DB에서 사용자 조회, 없으면 외부에서 받아와 생성"""
    user = await get_user_by_id(db, user_id)
    if user:
        return user

    logger.info(
        "사용자 정보 없음, 사용자 존재 여부 외부 확인", extra={"user_id": user_id}
    )

    return await create_user(user_id, db, check_existance=False)


async def get_current_user(
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: str | None = Header(None),
    x_user_id: str = Header(None),
) -> User:
    """Bearer JWT를 검증하고 비동기 방식으로 User 객체를 반환합니다.

    Args:
        x_user_id (str): 요청 헤더에서 가져온 사용자 ID
        db (AsyncSession): 비동기 데이터베이스 세션
        client: AsyncClient: 비동기 HTTP 클라이언트

    Returns:
        User: 데이터베이스에서 조회된 사용자 객체

    Raises:
        HTTPException: X-User-ID 헤더가 없거나 사용자가 존재하지 않는 경우
    """
    if authorization:
        principal = await principal_from_authorization(authorization)
        user = await get_or_create_user(principal.user_id, db)
        _attach_auth_principal(user, principal)
        return user

    if x_user_id is None or not dev_header_fallback_allowed():
        raise bearer_unauthorized("Authorization Bearer token is required")

    user = await get_or_create_user(x_user_id, db)
    _attach_auth_principal(
        user,
        AuthenticatedPrincipal(
            user_id=x_user_id,
            global_admin=False,
            meal_admin=False,
            claims={"sub": x_user_id},
        ),
    )
    return user


def _attach_auth_principal(user: User, principal: AuthenticatedPrincipal) -> None:
    user.auth_claims = principal.claims
    user.auth_global_admin = principal.global_admin
    user.auth_meal_admin = principal.meal_admin


async def get_admin_user(
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: str | None = Header(None),
    x_user_id: str = Header(None),
) -> AdminUserSchema:
    """현재 사용자가 관리자 권한을 가지고 있는지 확인하고 UserSchema 객체를 반환합니다.

    Args:
        x_user_id (str): 요청 헤더에서 가져온 사용자 ID
        db (AsyncSession): 비동기 데이터베이스 세션
        client (AsyncClient): 비동기 HTTP 클라이언트

    Returns:
        User: 관리자 권한이 확인된 사용자 DB 객체

    Raises:
        HTTPException: X-User-ID 헤더가 없거나 사용자가 존재하지 않는 경우
    """
    user = await get_current_user(db, authorization, x_user_id)
    admin_user = await check_admin_user(user)
    if not admin_user.is_admin:
        raise HTTPException(
            status_code=Config.HttpStatus.FORBIDDEN,
            detail="관리자 권한이 필요합니다.",
        )
    return admin_user


async def resolve_user_ids(
    db: AsyncSession,
    owner_user_id: str | None = None,
    manager_user_id: str | None = None,
) -> tuple[int | None, int | None]:
    """owner_user_id와 manager_user_id를 각각 User.id로 반환합니다.

    Args:
        db (AsyncSession): db 세션
        owner_user_id (str | None, optional): 식당 소유자 user_id. Defaults to None.
        manager_user_id (str | None, optional): 식당 관리자 user_id. Defaults to None.

    Returns:
        tuple[int | None, int | None]: ownser_id와 manager_id가 담긴 튜플
    """
    owner_id = manager_id = None

    if owner_user_id is not None:
        normalized_owner_user_id = owner_user_id.strip()
        if not normalized_owner_user_id:
            raise HTTPException(
                status_code=Config.HttpStatus.BAD_REQUEST,
                detail="owner_user_id는 비어 있을 수 없습니다.",
            )

        result = await db.execute(
            select(User.id).where(User.user_id == normalized_owner_user_id)
        )
        owner_id = result.scalar_one_or_none()

    if manager_user_id is not None:
        normalized_manager_user_id = manager_user_id.strip()
        if not normalized_manager_user_id:
            raise HTTPException(
                status_code=Config.HttpStatus.BAD_REQUEST,
                detail="manager_user_id는 비어 있을 수 없습니다.",
            )

        result = await db.execute(
            select(User.id).where(User.user_id == normalized_manager_user_id)
        )
        manager_id = result.scalar_one_or_none()

    return owner_id, manager_id
