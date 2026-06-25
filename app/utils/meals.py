"""식사 관련 유틸리티 함수 모음

이 모듈은 식사 관련 데이터베이스 트랜잭션 및 헬퍼 함수들을 포함하고 있습니다.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo
from collections.abc import Callable
from typing import Any
from fastapi import HTTPException
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.config import Config, logger
from app.models.meals import Meal, MealType
from app.services.audit import AuditLogEntry, add_audit_log
from app.schemas.meals import MealUpdate


SEOUL_TIMEZONE = ZoneInfo("Asia/Seoul")


def parse_served_date(value: str) -> date:
    """Parse a strict ISO served-date query parameter."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="날짜 형식이 올바르지 않습니다. (YYYY-MM-DD)",
        ) from exc


def seoul_business_date() -> date:
    """Return the current business date in Asia/Seoul."""
    return datetime.now(SEOUL_TIMEZONE).date()


async def apply_date_filter(
    query: Select[Any],
    start_date: str | None,
    end_date: str | None,
) -> Select[Any]:
    """날짜 필터링을 적용하는 헬퍼 함수

    Args:
        query (Select): SQLAlchemy Select 객체.
        start_date (str | None): 필터링할 시작 날짜 (YYYY-MM-DD 형식).
        end_date (str | None): 필터링할 종료 날짜 (YYYY-MM-DD 형식).

    Returns:
        Select: 날짜 필터링이 적용된 SQLAlchemy Select 객체.

    Raises:
        HTTPException: 날짜 형식이 올바르지 않은 경우.
    """
    logger.debug(
        "apply_date_filter called with start_date=%s, end_date=%s", start_date, end_date
    )

    try:
        start_date_dt = date.fromisoformat(start_date) if start_date else None
        end_date_dt = date.fromisoformat(end_date) if end_date else None
    except ValueError as exc:
        logger.warning(
            "Invalid date format received: start_date=%s, end_date=%s",
            start_date,
            end_date,
        )
        raise HTTPException(
            status_code=Config.HttpStatus.BAD_REQUEST,
            detail="날짜 형식이 올바르지 않습니다. (YYYY-MM-DD)",
        ) from exc

    if start_date_dt and end_date_dt:
        if start_date_dt > end_date_dt:
            logger.info("Reversing date range: %s -> %s", start_date_dt, end_date_dt)
            query = query.where(Meal.served_date.between(end_date_dt, start_date_dt))
        else:
            query = query.where(Meal.served_date.between(start_date_dt, end_date_dt))
    elif start_date_dt:
        query = query.where(Meal.served_date >= start_date_dt)
    elif end_date_dt:
        query = query.where(Meal.served_date <= end_date_dt)

    logger.debug("Query after applying date filter: %s", query)
    return query


async def get_meal_type(db: AsyncSession, meal_type_name: str) -> MealType:
    """식사 유형(MealType)을 가져오는 헬퍼 함수

    Args:
        db (AsyncSession): SQLAlchemy 비동기 세션 객체.
        meal_type_name (str): 가져올 식사 유형의 이름.

    Returns:
        MealType: 요청한 이름의 식사 유형 객체.

    Raises:
        HTTPException: 요청한 이름의 식사 유형을 찾을 수 없는 경우.
    """
    logger.debug("Fetching MealType: %s", meal_type_name)

    meal_type_result = await db.execute(
        select(MealType).where(MealType.name == meal_type_name)
    )
    meal_type = meal_type_result.scalars().first()

    if not meal_type:
        logger.warning("MealType not found: %s", meal_type_name)
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND, detail="Meal type not found"
        )

    logger.info("MealType found: %s", meal_type.name)
    return meal_type


async def register_meal_transaction(
    db: AsyncSession,
    new_meal: Meal,
    audit_entry_factory: Callable[[Meal], AuditLogEntry] | None = None,
):
    """식사를 등록하는 트랜잭션 처리

    Args:
        db (AsyncSession): SQLAlchemy 비동기 세션 객체.
        new_meal (Meal): 등록할 새로운 식사 객체.
        audit_entry_factory (Callable[[Meal], AuditLogEntry] | None): flush 후 감사 로그를 생성하는 함수.

    Raises:
        HTTPException: 식사 등록 중 오류가 발생한 경우.
    """
    logger.info("Registering meal: %s", new_meal)

    try:
        db.add(new_meal)
        await db.flush()
        if audit_entry_factory is not None:
            add_audit_log(db, audit_entry_factory(new_meal))
        await db.commit()
        await db.refresh(new_meal)
        logger.info("Meal successfully registered: %s", new_meal.id)
    except Exception as e:
        await db.rollback()
        logger.error("Meal 등록 중 에러 발생: %s", e)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="식사 등록 중 오류가 발생했습니다.",
        ) from e


async def delete_meal_transaction(
    db: AsyncSession,
    meal: Meal,
    audit_entry: AuditLogEntry | None = None,
):
    """식사를 삭제하는 트랜잭션 처리

    Args:
        db (AsyncSession): SQLAlchemy 비동기 세션 객체.
        meal (Meal): 삭제할 식사 객체.
        audit_entry (AuditLogEntry | None): 같은 트랜잭션에 저장할 감사 로그 입력값.

    Raises:
        HTTPException: 식사 삭제 중 오류가 발생한 경우.
    """
    logger.info("Deleting meal: %s", meal.id)

    try:
        await db.delete(meal)
        if audit_entry is not None:
            add_audit_log(db, audit_entry)
        await db.commit()
        logger.info("Meal successfully deleted: %s", meal.id)
    except Exception as e:
        await db.rollback()
        logger.error("Meal 삭제 중 에러 발생: %s", e)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="식사 삭제 중 오류가 발생했습니다.",
        ) from e


async def update_meal_menu_transaction(
    db: AsyncSession,
    meal: Meal,
    updated_menu: list[str],
    audit_entry: AuditLogEntry | None = None,
):
    """식사 메뉴를 수정하는 트랜잭션 처리

    Args:
        db (AsyncSession): SQLAlchemy 비동기 세션 객체.
        meal (Meal): 수정할 식사 객체.
        updated_menu (list[str]): 업데이트할 메뉴 리스트.
        audit_entry (AuditLogEntry | None): 같은 트랜잭션에 저장할 감사 로그 입력값.

    Raises:
        HTTPException: 식사 메뉴 수정 중 오류가 발생한 경우.
    """
    logger.info(
        "Updating meal menu for meal_id=%s with new menu: %s", meal.id, updated_menu
    )

    try:
        meal.menu = updated_menu
        meal.main_menu = updated_menu[0] if updated_menu else ""
        meal.side_menus = updated_menu[1:]
        db.add(meal)
        if audit_entry is not None:
            add_audit_log(db, audit_entry)
        await db.commit()
        await db.refresh(meal)
        logger.info("Meal menu successfully updated: %s", meal.id)
    except Exception as e:
        await db.rollback()
        logger.error("Meal 메뉴 수정 중 에러 발생: %s", e)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="식사 메뉴 수정 중 오류가 발생했습니다.",
        ) from e


async def update_meal_transaction(
    db: AsyncSession,
    meal: Meal,
    *,
    meal_type_id: int | None,
    meal_update: MealUpdate,
    audit_entry: AuditLogEntry | None = None,
):
    """식사 전체 정보를 수정하는 트랜잭션 처리"""
    logger.info(
        "Updating meal %s to restaurant_id=%s meal_type_id=%s",
        meal.id,
        meal_update.restaurant_id,
        meal_type_id,
    )

    try:
        if meal_update.restaurant_id is not None:
            meal.restaurant_id = meal_update.restaurant_id
        if meal_type_id is not None:
            meal.meal_type_id = meal_type_id
        if meal_update.served_date is not None:
            meal.served_date = meal_update.served_date
        if meal_update.main_menu is not None:
            meal.main_menu = meal_update.main_menu
        if meal_update.side_menus is not None:
            meal.side_menus = meal_update.side_menus
        if "image_url" in meal_update.model_fields_set:
            meal.image_url = meal_update.image_url
        meal.menu = [meal.main_menu, *meal.side_menus]
        db.add(meal)
        if audit_entry is not None:
            add_audit_log(db, audit_entry)
        await db.commit()
        await db.refresh(meal)
        logger.info("Meal successfully updated: %s", meal.id)
    except Exception as e:
        await db.rollback()
        logger.error("Meal 전체 수정 중 에러 발생: %s", e)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="식사 수정 중 오류가 발생했습니다.",
        ) from e


def update_meal_menu(meal: Meal, menu_edit_list: str | list[str]) -> list[str]:
    """식사 메뉴를 수정하는 로직

    Args:
        meal (Meal): 수정할 식사 객체.
        menu_edit_list (str | list[str]): 추가할 메뉴 항목(들).

    Returns:
        list[str]: 수정된 메뉴 리스트.
    """
    logger.debug("Updating menu for meal_id=%s. Current menu: %s", meal.id, meal.menu)

    if isinstance(menu_edit_list, str):
        menu_edit_list = [menu_edit_list]
    menu_list = meal.menu.copy()
    for menu in menu_edit_list:
        if menu not in menu_list:
            menu_list.append(menu)

    logger.info("Updated menu for meal_id=%s: %s", meal.id, menu_list)
    return menu_list


def delete_meal_menu(meal: Meal, menu_delete_list: str | list[str]) -> list[str]:
    """식사 메뉴를 삭제하는 로직

    Args:
        meal (Meal): 수정할 식사 객체.
        menu_delete_list (str | list[str]): 삭제할 메뉴 항목(들).

    Returns:
        list[str]: 수정된 메뉴 리스트.
    """
    logger.debug(
        "Deleting menu items from meal_id=%s. Current menu: %s", meal.id, meal.menu
    )

    if isinstance(menu_delete_list, str):
        menu_delete_list = [menu_delete_list]
    menu_list = meal.menu.copy()
    for menu in menu_delete_list:
        if menu in menu_list:
            menu_list.remove(menu)

    logger.info("Updated menu after deletion for meal_id=%s: %s", meal.id, menu_list)
    return menu_list
