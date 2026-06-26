"""Meal service local user projection management APIs.

이 모듈은 검증된 Keycloak JWT의 subject를 meal-service 로컬 DB에 보관하는
사용자 projection을 관리합니다. Keycloak Admin API는 호출하지 않습니다.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.config import Config, logger
from app.models.user import User
from app.schemas.users import UserCreate, UserSchema
from app.utils.db import get_admin_user, get_db, get_user_by_id, create_user, delete_user

router = APIRouter(
    prefix="/users",
    tags=["User"],
    dependencies=[Depends(get_admin_user)],
)


@router.post("/", response_model=UserSchema)
async def register_user(
    payload: UserCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """사용자를 등록합니다.

    Keycloak Admin API 검증 없이 관리자가 명시적으로 로컬 projection을 생성합니다.
    일반 사용자는 인증된 JWT로 서비스에 접근할 때 자동 생성됩니다.
    """
    return UserSchema.model_validate(await create_user(payload.user_id, db))


@router.get("/{user_id}", response_model=UserSchema)
async def get_user(user_id: str, db: Annotated[AsyncSession, Depends(get_db)]):
    """Get a user by ID."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND,
            detail="User not found",
        )
    return UserSchema.model_validate(user)


@router.get("/", response_model=list[UserSchema])
async def list_users(db: Annotated[AsyncSession, Depends(get_db)]):
    """List all users."""
    result = await db.execute(select(User))
    users = result.scalars().all()
    return [UserSchema.model_validate(user) for user in users]


@router.delete("/{user_id:str}", status_code=Config.HttpStatus.NO_CONTENT)
async def remove_user(user_id: str, db: Annotated[AsyncSession, Depends(get_db)]):
    """Delete a user by ID."""
    await delete_user(db, user_id)
    try:
        await db.commit()
    except SQLAlchemyError as e:
        await db.rollback()
        logger.error("사용자 삭제 중 오류 발생 %s", e)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="Database commit failed",
        ) from e
