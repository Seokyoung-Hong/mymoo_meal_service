"""어디에서도 참조되지 않는 고아 이미지 파일을 정리하는 서비스 모듈입니다.

업로드 후 유예시간(IMAGE_ORPHAN_GRACE_HOURS)이 지나도록 참조되지 않는 이미지의
파일과 DB 행을 삭제합니다. 유예시간은 업로드와 리소스 등록(예: 식단 등록) 사이의
시간차를 흡수하기 위한 안전장치입니다.

참조 여부는 image_type별 검사 함수 레지스트리(REFERENCE_CHECKS)로 판단합니다.
새 이미지 용도를 추가할 때는 해당 용도의 참조 검사 함수를 함께 등록해야 하며,
레지스트리에 없는 용도의 이미지는 안전하게 보존됩니다.
"""

from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Config, logger
from app.database import AsyncSessionLocal
from app.models import Meal, StoredImage
from app.services.images import MEAL_IMAGE_TYPE, delete_image_files


async def _is_meal_image_referenced(session: AsyncSession, stored_name: str) -> bool:
    """식단 사진이 어떤 Meal.image_url에서든 참조되는지 확인합니다.

    Args:
        session (AsyncSession): 활성 비동기 DB 세션.
        stored_name (str): 공개 디렉터리의 webp 파일명.

    Returns:
        bool: 참조 중이면 True.
    """
    return bool(
        await session.scalar(
            select(exists().where(Meal.image_url.endswith(stored_name)))
        )
    )


# image_type별 참조 검사 함수. 새 용도 추가 시 여기에 등록합니다.
REFERENCE_CHECKS: dict[str, Callable[[AsyncSession, str], Awaitable[bool]]] = {
    MEAL_IMAGE_TYPE: _is_meal_image_referenced,
}


async def cleanup_orphan_images(
    session_factory: async_sessionmaker | None = None,
) -> int:
    """유예시간이 지난 미참조 이미지의 파일과 DB 행을 삭제합니다.

    Args:
        session_factory (async_sessionmaker | None): 세션 팩토리.
            생략하면 애플리케이션 기본 세션 팩토리를 사용합니다.

    Returns:
        int: 삭제한 이미지 수.
    """
    if session_factory is None:
        session_factory = AsyncSessionLocal

    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=Config.IMAGE_ORPHAN_GRACE_HOURS
    )
    deleted_count = 0

    async with session_factory() as session:
        result = await session.execute(
            select(StoredImage).where(StoredImage.created_at < cutoff)
        )
        candidates = result.scalars().all()

        for candidate in candidates:
            try:
                reference_check = REFERENCE_CHECKS.get(candidate.image_type)
                if reference_check is None:
                    logger.warning(
                        "참조 검사가 등록되지 않은 이미지 용도라 보존합니다: %s (%s)",
                        candidate.image_type,
                        candidate.stored_name,
                    )
                    continue
                if await reference_check(session, candidate.stored_name):
                    continue
                delete_image_files(candidate.original_name, candidate.stored_name)
                await session.delete(candidate)
                deleted_count += 1
            except (
                Exception
            ) as error:  # noqa: BLE001 - 한 건의 오류가 잡 전체를 멈추지 않도록 함
                logger.error(
                    "고아 이미지 %s 정리 중 예외 발생: %s",
                    candidate.stored_name,
                    error,
                )
        await session.commit()

        # DB 행 없이 남은 파일 회수 (식당 하드 삭제 시 CASCADE로 행만 사라진 경우)
        deleted_count += await _sweep_untracked_files(session, cutoff)

    if deleted_count:
        logger.info("고아 이미지 %d건을 정리했습니다.", deleted_count)
    return deleted_count


_PUBLIC_FILE_PATTERN = re.compile(r"^([0-9a-f]{32})(\.thumb)?\.webp$")


async def _sweep_untracked_files(session: AsyncSession, cutoff: datetime) -> int:
    """DB에 행이 없는 오래된 이미지 파일을 디렉터리에서 삭제합니다.

    전체화면용·썸네일 파생본이 같은 uuid를 공유하므로 uuid 단위로 판단합니다.

    Args:
        session (AsyncSession): 활성 비동기 DB 세션.
        cutoff (datetime): 이 시각보다 오래된 파일만 삭제 대상으로 판단합니다.

    Returns:
        int: 삭제한 이미지(uuid) 수.
    """
    public_dir = Config.IMAGE_PUBLIC_DIR
    if not os.path.isdir(public_dir):
        return 0

    result = await session.execute(select(StoredImage.stored_name))
    tracked_ids = {name.removesuffix(".webp") for name in result.scalars().all()}

    swept_ids: set[str] = set()
    for file_name in os.listdir(public_dir):
        matched = _PUBLIC_FILE_PATTERN.match(file_name)
        if matched is None or matched.group(1) in tracked_ids:
            continue
        image_id = matched.group(1)
        file_path = os.path.join(public_dir, file_name)
        try:
            modified_at = datetime.fromtimestamp(
                os.path.getmtime(file_path), tz=timezone.utc
            )
            if modified_at >= cutoff:
                continue
            os.unlink(file_path)
            # 같은 uuid를 공유하는 원본도 함께 삭제
            for original_name in os.listdir(Config.IMAGE_ORIGINALS_DIR):
                if original_name.startswith(f"{image_id}."):
                    os.unlink(os.path.join(Config.IMAGE_ORIGINALS_DIR, original_name))
            swept_ids.add(image_id)
        except OSError as error:
            logger.error(
                "미추적 이미지 파일 %s 정리 중 예외 발생: %s", file_name, error
            )
    return len(swept_ids)
