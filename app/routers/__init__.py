"""API 서버를 구성하는 모듈들

API 서버를 구성하는 모듈들이 정의되어 있습니다.
settings.py에는 환경 변수가 설정되어 있습니다.
utils.py에는 API 서버에서 사용하는 유틸리티 함수들이 정의되어 있습니다.
이외의 모듈은 각각의 API 엔드포인트를 정의하고 있습니다.
"""

from .meals import router as meals_router
from .pricing import router as pricing_router
from .restaurants import router as restaurants_router
from .users import router as users_router
from .worker import router as worker_router

__all__ = [
    "meals_router",
    "pricing_router",
    "restaurants_router",
    "users_router",
    "worker_router",
]
