"""Mymoo의 메인 애플리케이션 파일입니다."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from app.config import logger, Config
from app.routers import meals_router, pricing_router, restaurants_router, users_router
from app.utils.lifespan import (
    sync_meal_types,
    ensure_service_account_in_db,
)
from app.utils.request_id import add_request_id_middleware
from app.database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI의 lifespan 이벤트 핸들러"""
    logger.info("🚀 서비스 시작: 데이터베이스 초기화 및 기본 데이터 동기화")
    logger.debug(
        "Config 정보 로드: %s",
        {
            "debug": Config.debug,
            "timezone": Config.TIMEZONE,
            "user_service_url": Config.USER_SERVICE_URL,
        },
    )

    # 1. DB 초기화
    await init_db()

    # 2. meal_type 동기화
    await sync_meal_types()

    # 3. Service Account를 DB에 등록 (Restaurant owner로 사용)
    await ensure_service_account_in_db()

    yield  # FastAPI 실행 유지

    # 4. 종료 작업
    logger.info("🛑 서비스 종료: 정리 작업 완료")


# lifespan 적용
app = FastAPI(lifespan=lifespan, root_path="/meal")
if Config.CORS_ALLOW_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=Config.CORS_ALLOW_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
add_request_id_middleware(app)

# 라우터 추가
app.include_router(meals_router)
app.include_router(pricing_router)
app.include_router(restaurants_router)
app.include_router(users_router)


@app.get("/")
async def root():
    """루트 엔드포인트입니다."""
    logger.info("Root endpoint accessed")
    return {"test": "Hello Mymoo"}


@app.get("/health")
async def health_check():
    """헬스 체크 엔드포인트입니다."""
    return {"status": "ok"}


if __name__ == "__main__":
    HOST = "0.0.0.0"  # noqa: S104
    PORT = 5600
    logger.info("Starting Mymoo server on %s:%s", HOST, PORT)
    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
