"""DB의 meal_type 테이블을 meal_types.json과 동기화"""

import traceback

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import AsyncSessionLocal
from app.models.meals import MealType
from app.models.restaurants import set_service_user_id
from app.models.user import User
from app.config import Config, logger
from app.services.user_service import keycloak_user_exists_by_id


PLACEHOLDER_MARKERS = ("replace-with", "change-this", "your-")


def _is_placeholder_value(value: str | None) -> bool:
    if not value:
        return True

    normalized = value.strip().lower()
    return any(marker in normalized for marker in PLACEHOLDER_MARKERS)


def _has_placeholder_service_account_config() -> bool:
    return any(
        _is_placeholder_value(value)
        for value in (
            Config.SERVICE_ACCOUNT_SUB,
            Config.KC_CLIENT_SECRET,
            Config.KC_REALM,
        )
    )


async def sync_meal_types():
    """DB의 meal_type 테이블을 meal_types.json과 동기화

    meal_types.json 파일에 정의된 meal_type들을 데이터베이스의 meal_type 테이블과 동기화합니다.
    기존에 존재하지 않는 meal_type은 새로 추가됩니다.

    Raises:
        IntegrityError: 중복된 meal_type이 감지된 경우 발생합니다.
    """
    async with AsyncSessionLocal() as db:
        try:
            meal_types = Config.load_meal_types()

            # 기존 DB에 있는 meal_type 조회 (비동기)
            result = await db.execute(select(MealType))
            existing_meal_types = {mt.name for mt in result.scalars().all()}
            logger.debug("기존 meal_type: %s", existing_meal_types)

            # 추가해야 할 meal_type 찾기
            new_meal_types = [
                MealType(name=mt) for mt in meal_types if mt not in existing_meal_types
            ]
            logger.debug(
                "새로 추가할 meal_type: %s", [mt.name for mt in new_meal_types]
            )

            if new_meal_types:
                db.add_all(
                    new_meal_types
                )  # ✅ `bulk_save_objects()` 대신 `add_all()` 사용
                await db.commit()
                logger.info("%s개의 meal_type이 추가되었습니다.", len(new_meal_types))
            else:
                logger.info("meal_type이 최신 상태입니다.")

        except IntegrityError:
            message = traceback.format_exc()
            logger.debug("Error details: %s", message)
            logger.warning("중복된 meal_type이 감지되었습니다.")
            await db.rollback()
            logger.debug("DB 롤백 완료")


async def ensure_service_account_in_db() -> None:
    """서버 실행 전에 service_account를 DB와 동기화합니다.

    - Keycloak에 존재하는 service_account를 DB(User 테이블)에 1회만 등록한다.
    - 이미 있으면 아무 것도 하지 않는다.
    - 로컬 smoke용 placeholder 설정이거나 Keycloak 확인이 불가능하면 건너뛴다.
    - Keycloak 응답이 있고 사용자가 없으면 DB에 추가하지 않는다(오류로 처리).
    - 생성된 User.id를 set_service_user_id로 설정한다.
    """
    service_kc_user_id = Config.SERVICE_ACCOUNT_SUB  # Keycloak UUID (필수)

    if _has_placeholder_service_account_config():
        logger.warning(
            "service_account bootstrap skipped: placeholder or missing Keycloak config"
        )
        return

    if service_kc_user_id is None:
        logger.warning("service_account bootstrap skipped: missing service account sub")
        return

    try:
        # 1) Keycloak 존재 확인 (없으면 DB에 넣으면 안 됨)
        exists_in_kc = await keycloak_user_exists_by_id(service_kc_user_id)
    except Exception as e:
        logger.warning(
            "service_account bootstrap skipped: Keycloak lookup unavailable",
            exc_info=e,
        )
        return

    if not exists_in_kc:
        raise RuntimeError(
            f"Keycloak에 service_account가 없습니다. user_id={service_kc_user_id}"
        )

    async with AsyncSessionLocal() as db:
        new_user = None
        try:
            # 2) DB에 이미 있으면 해당 User.id 설정 후 종료
            result = await db.execute(
                select(User).where(User.user_id == service_kc_user_id)
            )
            row = result.scalar_one_or_none()
            if row:
                logger.info(
                    "service_account 이미 존재: User(id=%s, user_id=%s)",
                    row.id,
                    row.user_id,
                )
                set_service_user_id(row.id)
                return

            # 3) 없으면 insert (권한/role 정보는 저장하지 않음)
            new_user = User(user_id=service_kc_user_id)
            db.add(new_user)
            await db.commit()
            await db.refresh(new_user)
            set_service_user_id(new_user.id)
            logger.info(
                "service_account DB 생성 완료: User(id=%s, user_id=%s)",
                new_user.id,
                new_user.user_id,
            )
        except Exception as e:
            await db.rollback()
            logger.error("service_account DB 생성 중 오류 발생", exc_info=e)
            raise
