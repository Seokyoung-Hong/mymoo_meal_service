"""이 모듈은 기본 스키마를 정의합니다.

Pydantic을 사용하여 데이터 스키마를 정의하고, KST(서울 시간)으로 자동 변환되는 datetime 필드를 제공합니다.
"""

from typing import Generic, TypeVar
from datetime import datetime, timezone

from pydantic import BaseModel, GetCoreSchemaHandler
from pydantic_core import core_schema

from app.config import Config, logger


T = TypeVar("T")


class MetaData(BaseModel):
    """메타데이터를 나타내는 클래스.

    Attributes:
        total (int): 총 개수. 기본값은 1.
    """

    total: int = 1


class BaseSchema(BaseModel, Generic[T]):
    """기본 스키마를 나타내는 제네릭 클래스.

    Attributes:
        status (str): 상태를 나타내는 문자열. 기본값은 "success".
        meta (MetaData): 메타데이터 객체. 기본값은 MetaData().
        data (T): 데이터 객체.
    """

    status: str = "success"
    meta: MetaData = MetaData()
    data: T


class Timestamp:
    """KST(서울 시간)으로 자동 변환되는 datetime 필드"""

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler: GetCoreSchemaHandler):
        """Pydantic이 사용할 스키마를 정의합니다.

        Args:
            source_type (type): 원본 타입.
            handler (GetCoreSchemaHandler): 스키마 핸들러.

        Returns:
            core_schema: Pydantic 코어 스키마.
        """
        logger.debug("Generating pydantic core schema for Timestamp")
        return core_schema.no_info_after_validator_function(
            cls.convert_to_kst, handler.generate_schema(datetime)
        )

    @classmethod
    def convert_to_kst(cls, value: str | datetime) -> datetime:
        """ISO 8601 문자열 또는 datetime을 받아 KST로 변환합니다.

        Args:
            value (str | datetime): 변환할 값.

        Returns:
            datetime: KST로 변환된 datetime 객체.

        Raises:
            ValueError: 유효하지 않은 ISO 8601 형식의 문자열인 경우.
            TypeError: str 또는 datetime이 아닌 타입인 경우.
        """
        logger.debug(f"Converting value to KST: {value}")
        if isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value)
                if dt.tzinfo is None:
                    # ✅ 타임존이 없는 경우, 기본적으로 UTC로 간주한 후 KST로 변환
                    logger.debug(f"Naive string: {dt.isoformat()}")
                    logger.debug(f"Timezone info: {Config.TIMEZONE}")
                    dt = dt.replace(tzinfo=timezone.utc).astimezone(Config.TZ)
                    logger.debug(
                        f"Converted naive string to KST datetime: {dt.isoformat()}"
                    )
                else:
                    # ✅ 타임존이 있는 경우, KST로 변환
                    dt = dt.astimezone(Config.TZ)
                logger.debug(f"Converted string to KST datetime: {dt.isoformat()}")
                return dt
            except ValueError as err:
                logger.error(f"Invalid ISO 8601 format: {value}")
                raise ValueError(f"Invalid ISO 8601 format: {value}") from err

        elif isinstance(value, datetime):
            if value.tzinfo is None:
                # ✅ datetime 객체에 타임존이 없으면 KST로 간주
                logger.debug(f"Naive datetime: {value.isoformat()}")
                logger.debug(f"Timezone info: {Config.TIMEZONE}")
                dt = value.replace(tzinfo=timezone.utc).astimezone(Config.TZ)
                logger.debug(f"Converted naive datetime to KST: {dt.isoformat()}")
                return dt
            dt = value.astimezone(Config.TZ)
            logger.debug(f"Converted aware datetime to KST: {dt.isoformat()}")
            return dt
        else:
            logger.error(f"Expected str or datetime, got {type(value)}")
            raise TypeError(f"Expected str or datetime, got {type(value)}")
