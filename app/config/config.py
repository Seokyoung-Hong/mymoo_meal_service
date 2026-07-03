"""FastAPI 앱의 설정을 정의하는 모듈입니다.

이 모듈은 환경 변수를 로드하고, 로깅을 설정하며, FastAPI 애플리케이션의 설정 값을 관리하는 Config 클래스를 제공합니다.
또한, meal_types.json 파일에서 식사 유형을 불러오는 기능도 포함되어 있습니다.
"""

import os
import logging
import json
from dotenv import load_dotenv
from pytz import timezone

from fastapi_pagination.utils import disable_installed_extensions_check

# 환경 변수 로딩
load_dotenv()

disable_installed_extensions_check()

# 현재 파일이 위치한 디렉터리 (config 폴더의 절대 경로)
CONFIG_DIR = os.path.abspath(os.path.dirname(__file__))

SERVICE_DIR = os.path.abspath(os.path.join(CONFIG_DIR, "../.."))

# tmp_dir make
if not os.path.exists(os.path.join(SERVICE_DIR, "tmp")):
    os.makedirs(os.path.join(SERVICE_DIR, "tmp"))

# 로깅 설정
logger = logging.getLogger("mymoo_meal_service")
logger.setLevel(logging.DEBUG)  # 모든 로그 기록

console_handler = logging.StreamHandler()
if os.getenv("DEBUG", "False").lower() == "true":
    console_handler.setLevel(logging.DEBUG)  # DEBUG 이상 출력
else:
    # DEBUG 모드가 아닐 때는 INFO 이상만 출력
    console_handler.setLevel(logging.INFO)  # INFO 이상만 출력
console_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
console_handler.setFormatter(console_formatter)

# 로거에 핸들러 추가
logger.addHandler(console_handler)


def database_url():
    """데이터베이스 URL을 반환하는 함수.

    우선순위:
    1) DATABASE_URL (명시된 전체 URL)
    2) POSTGRES_* 환경 변수 조합
    """
    explicit_url = os.getenv("DATABASE_URL")
    if explicit_url:
        explicit_url = explicit_url.strip()
        if explicit_url.startswith("postgresql://"):
            return explicit_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if explicit_url.startswith("postgresql+psycopg2://"):
            return explicit_url.replace(
                "postgresql+psycopg2://", "postgresql+asyncpg://", 1
            )
        return explicit_url

    postgres_db = os.getenv("POSTGRES_DB", "meal_service")
    postgres_user = os.getenv("POSTGRES_USER", "postgres")
    postgres_password = os.getenv("POSTGRES_PASSWORD", "postgres")
    postgres_host = os.getenv("POSTGRES_HOST", "mymoo-meal-service-db")
    postgres_port = os.getenv("POSTGRES_PORT", "5432")
    return (
        f"postgresql+asyncpg://{postgres_user}:{postgres_password}@"
        f"{postgres_host}:{postgres_port}/{postgres_db}"
    )


def comma_separated_env(name: str, default: str = "") -> list[str]:
    """쉼표로 구분된 환경 변수 값을 리스트로 반환합니다."""
    return [
        item.strip()
        for item in os.getenv(name, default).split(",")
        if item.strip()
    ]


class Config:
    """FastAPI 설정 값을 관리하는 클래스

    이 클래스는 환경 변수에서 설정 값을 로드하고, 기본 값을 제공합니다.
    또한, meal_types.json 파일에서 식사 유형을 불러오는 기능도 포함되어 있습니다.
    """

    debug = os.getenv("DEBUG", "False").lower() == "true"
    ENV = os.getenv("ENV", "production")

    SERVICE_ACCOUNT_SUB: str | None = os.getenv("SERVICE_ACCOUNT_SUB")
    SERVICE_ACCOUNT_TOKEN: str | None = os.getenv("SERVICE_ACCOUNT_TOKEN")
    SERVICE_ACCOUNT_TOKEN_TYPE: str = os.getenv("SERVICE_ACCOUNT_TOKEN_TYPE", "Bearer")

    SERVICE_DIR = SERVICE_DIR
    CONFIG_DIR = CONFIG_DIR
    TMP_DIR = os.path.join(SERVICE_DIR, "tmp")

    USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://user-service:8000").rstrip(
        "/"
    )
    DATABASE_URL = database_url()
    TIMEZONE = os.getenv("TIMEZONE", "Asia/Seoul")
    TZ = timezone(TIMEZONE)
    CORS_ALLOW_ORIGINS = comma_separated_env(
        "CORS_ALLOW_ORIGINS",
        "http://localhost:5601,http://127.0.0.1:5601,https://mymoo.quanect.kr",
    )

    MIN_TEST_USERS = 2
    KC_SERVER_URL = os.getenv("KC_SERVER_URL", "https://auth.quanect.kr/")
    KC_CLIENT_ID = os.getenv("KC_CLIENT_ID", "mymoo-meal-service")
    KC_REALM = os.getenv("KC_REALM", "replace-with-keycloak-realm")

    AUTH_DEV_HEADER_FALLBACK_ENABLED = (
        os.getenv("AUTH_DEV_HEADER_FALLBACK_ENABLED", "false").lower() == "true"
    )
    KEYCLOAK_DISCOVERY_URL = os.getenv(
        "KEYCLOAK_DISCOVERY_URL",
        f"{KC_SERVER_URL.rstrip('/')}/realms/{KC_REALM}/.well-known/openid-configuration",
    )
    JWT_ISSUER = os.getenv(
        "JWT_ISSUER", f"{KC_SERVER_URL.rstrip('/')}/realms/{KC_REALM}"
    )
    JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", KC_CLIENT_ID)
    JWT_ALLOWED_ALGORITHMS = [
        algorithm.strip()
        for algorithm in os.getenv("JWT_ALLOWED_ALGORITHMS", "RS256").split(",")
        if algorithm.strip()
    ]
    JWT_CLIENT_ID = os.getenv("JWT_CLIENT_ID", KC_CLIENT_ID)

    REALM_GLOBAL_ADMIN_ROLE = "global_admin"
    MEAL_CLIENT_ADMIN_ROLE = "meal_admin"

    class HttpStatus:
        """HTTP 상태 코드를 정의하는 클래스"""

        OK = 200
        CREATED = 201
        NO_CONTENT = 204
        BAD_REQUEST = 400
        UNAUTHORIZED = 401
        FORBIDDEN = 403
        NOT_FOUND = 404
        CONFLICT = 409
        INTERNAL_SERVER_ERROR = 500

    @staticmethod
    def get_meal_types_file():
        """meal_types.json 파일 경로 반환

        Returns:
            str: meal_types.json 파일의 절대 경로
        """
        return os.path.join(
            CONFIG_DIR, os.getenv("MEAL_TYPES_FILE_NAME", "meal_types.json")
        )

    @staticmethod
    def load_meal_types():
        """meal_types.json에서 식사 유형을 불러옴

        Returns:
            list: meal_types.json 파일에서 불러온 식사 유형 리스트. 파일이 없거나 손상된 경우 빈 리스트 반환.
        """
        meal_types_file = Config.get_meal_types_file()
        try:
            with open(meal_types_file, "r", encoding="utf-8") as file:
                data = json.load(file)
                return data.get("meal_types", [])
        except (FileNotFoundError, json.JSONDecodeError):
            logger.warning(
                "⚠️ %s 파일이 없거나 손상됨. 빈 리스트 반환.", meal_types_file
            )
            return []
