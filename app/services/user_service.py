"""Authentication Service Module."""

from fastapi import HTTPException
from keycloak import KeycloakGetError, KeycloakOpenID, KeycloakAdmin
from keycloak.exceptions import KeycloakError

from app.config import Config, logger
from app.schemas.users import AdminUserSchema, UserSchema
from app.models.user import User


def get_keycloak_client() -> KeycloakOpenID:
    """동기 KeycloakOpenID 인스턴스를 생성합니다."""
    return KeycloakOpenID(
        server_url=Config.KC_SERVER_URL,
        realm_name=Config.KC_REALM,
        client_id=Config.KC_CLIENT_ID,
        client_secret_key=Config.KC_CLIENT_SECRET,
        timeout=10,
    )


def get_local_keycloak_admin_client() -> KeycloakAdmin:
    """로컬 KeycloakAdmin 인스턴스를 생성합니다."""
    return KeycloakAdmin(
        server_url=Config.KC_LOCAL_URL,
        realm_name=Config.KC_REALM,
        client_id=Config.KC_CLIENT_ID,
        client_secret_key=Config.KC_CLIENT_SECRET,
        verify=True,
        timeout=10,
    )


def get_keycloak_admin_client() -> KeycloakAdmin:
    """KeycloakAdmin 인스턴스를 생성합니다."""
    return KeycloakAdmin(
        server_url=Config.KC_SERVER_URL,
        realm_name=Config.KC_REALM,
        client_id=Config.KC_CLIENT_ID,
        client_secret_key=Config.KC_CLIENT_SECRET,
        verify=True,
        timeout=10,
    )


async def keycloak_user_exists_by_id(user_id: str) -> bool:
    """
    user_id(=Keycloak user_id)로 사용자 존재 여부만 확인합니다.

    - 존재: True
    - 404: False
    - 그 외: 예외
    """
    admin = get_local_keycloak_admin_client()
    try:
        await admin.a_get_user(user_id=user_id)  # sub가 Keycloak user UUID라는 전제
        return True
    except KeycloakGetError as e:
        if getattr(e, "response_code", None) == Config.HttpStatus.NOT_FOUND:
            return False
        logger.error("Keycloak 사용자 조회 중 오류 발생", exc_info=e)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="사용자 조회 중 오류가 발생했습니다.",
        ) from e
    except KeycloakError as e:
        logger.error("Keycloak 사용자 조회 중 오류 발생", exc_info=e)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="사용자 조회 중 오류가 발생했습니다.",
        ) from e


async def check_admin_user(user: User) -> AdminUserSchema:
    """global_admin(realm) OR meal_admin(client) 여부 확인"""
    return AdminUserSchema(
        id=user.id,
        user_id=user.user_id,
        global_admin=bool(getattr(user, "auth_global_admin", False)),
        meal_admin=bool(getattr(user, "auth_meal_admin", False)),
        created_at=user.created_at,
    )
