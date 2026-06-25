"""시간과 관련된 유틸리티 함수를 제공합니다."""

from datetime import datetime

from app.config import Config


def get_datetime_by_string(time_str: str) -> datetime:
    """문자열로 된 시간을 datetime 객체로 변환합니다."""
    return datetime.strptime(time_str, "%H:%M")


def get_string_by_datetime(time: datetime) -> str:
    """datetime 객체를 "HH:MM" 형식의 문자열로 변환합니다."""
    return time.strftime("%H:%M")


def get_now_string() -> str:
    """현재 시간을 "HH:MM" 형식의 문자열로 반환합니다."""
    return get_string_by_datetime(datetime.now())


def get_now_timestamp() -> str:
    """현재 시간을 iso8081형식의 문자열로 반환합니다."""
    return datetime.now().astimezone(Config.TZ).isoformat()
