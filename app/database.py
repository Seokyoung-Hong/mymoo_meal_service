"""데이터베이스 관련 모듈입니다.

비동기 SQLAlchemy를 사용하여 데이터베이스를 연결하고, 테이블을 생성합니다.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from app.config import Config

# 비동기 SQLAlchemy 엔진 생성
async_engine = create_async_engine(Config.DATABASE_URL)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# Base 클래스 생성
Base = declarative_base()


async def init_db():
    """비동기로 데이터베이스 테이블을 생성합니다."""
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
