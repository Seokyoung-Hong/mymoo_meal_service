"""백그라운드 주기 작업을 관리하는 APScheduler 설정 모듈입니다."""

from __future__ import annotations

from apscheduler.schedulers.asyncio import (  # type: ignore[import-untyped]
    AsyncIOScheduler,
)
from apscheduler.triggers.interval import (  # type: ignore[import-untyped]
    IntervalTrigger,
)

from app.config import Config
from app.services.image_cleanup import cleanup_orphan_images


def create_scheduler() -> AsyncIOScheduler:
    """설정에 따라 주기 작업이 등록된 스케줄러를 생성합니다.

    Returns:
        AsyncIOScheduler: 시작 전 상태의 스케줄러 인스턴스.
    """
    scheduler = AsyncIOScheduler(timezone=Config.TZ)
    if Config.IMAGE_CLEANUP_ENABLED:
        scheduler.add_job(
            cleanup_orphan_images,
            trigger=IntervalTrigger(hours=Config.IMAGE_CLEANUP_INTERVAL_HOURS),
            id="cleanup_orphan_images",
            coalesce=True,
            max_instances=1,
        )
    return scheduler
