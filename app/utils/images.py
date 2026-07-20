"""이미지 라우터를 보조하는 유틸리티 함수들을 정의하는 모듈입니다.

용도별 업로드 권한 검증, 도메인 예외의 HTTP 예외 변환, 감사 로그 팩토리,
공개 파일 경로 검증을 담당합니다.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.models import StoredImage, User
from app.services.audit import AuditLogEntry, request_id_from_request
from app.services.images import (
    MEAL_IMAGE_TYPE,
    SUPPORTED_IMAGE_TYPES,
    ImageTooLargeError,
    ImageTooManyPixelsError,
    ImageUploadError,
)
from app.utils.restaurants import get_restaurant_with_permission

PUBLIC_IMAGE_NAME_PATTERN = re.compile(r"^[0-9a-f]{32}(\.thumb)?\.webp$")


def upload_http_error(error: ImageUploadError) -> HTTPException:
    """이미지 업로드 도메인 예외를 HTTP 예외로 변환합니다.

    Args:
        error (ImageUploadError): 서비스 계층에서 발생한 도메인 예외.

    Returns:
        HTTPException: 상태 코드와 한국어 메시지가 매핑된 HTTP 예외.
    """
    if isinstance(error, ImageTooLargeError):
        return HTTPException(
            status_code=Config.HttpStatus.PAYLOAD_TOO_LARGE,
            detail=(
                "이미지 파일이 허용 용량"
                f"({Config.IMAGE_MAX_UPLOAD_BYTES // (1024 * 1024)}MB)을 초과했습니다."
            ),
        )
    if isinstance(error, ImageTooManyPixelsError):
        return HTTPException(
            status_code=Config.HttpStatus.UNPROCESSABLE_ENTITY,
            detail=(
                "이미지 해상도가 허용 한도"
                f"({Config.IMAGE_MAX_PIXELS}픽셀)를 초과했습니다."
            ),
        )
    return HTTPException(
        status_code=Config.HttpStatus.UNPROCESSABLE_ENTITY,
        detail="이미지 파일로 해석할 수 없는 파일입니다.",
    )


async def authorize_image_upload(
    image_type: str,
    restaurant_id: int | None,
    db: AsyncSession,
    current_user: User,
) -> None:
    """이미지 용도별 업로드 권한을 검증합니다.

    "meal" 용도는 restaurant_id가 필수이며 해당 식당의 owner/manager(또는
    관리자)만 업로드할 수 있고, 비활성 식당에는 업로드할 수 없습니다.
    새 용도를 추가할 때는 이 함수에 해당 용도의 권한 규칙을 함께 등록합니다.

    Args:
        image_type (str): 이미지 용도.
        restaurant_id (int | None): 식당 용도일 때 대상 식당 ID.
        db (AsyncSession): 비동기 DB 세션 객체.
        current_user (User): 요청을 보낸 현재 사용자 객체.

    Raises:
        HTTPException(403): 업로드 권한이 없는 경우.
        HTTPException(404): 대상 식당이 존재하지 않는 경우.
        HTTPException(409): 대상 식당이 비활성 상태인 경우.
        HTTPException(422): 지원하지 않는 용도이거나 필수 값이 없는 경우.
    """
    if image_type not in SUPPORTED_IMAGE_TYPES:
        raise HTTPException(
            status_code=Config.HttpStatus.UNPROCESSABLE_ENTITY,
            detail=f"지원하지 않는 이미지 용도입니다: {image_type}",
        )

    if image_type == MEAL_IMAGE_TYPE:
        if restaurant_id is None:
            raise HTTPException(
                status_code=Config.HttpStatus.UNPROCESSABLE_ENTITY,
                detail="식단 사진 업로드에는 restaurant_id가 필요합니다.",
            )
        restaurant = await get_restaurant_with_permission(
            restaurant_id, db, current_user
        )
        if not restaurant.is_active:
            raise HTTPException(
                status_code=Config.HttpStatus.CONFLICT,
                detail="비활성 식당에는 사진을 업로드할 수 없습니다.",
            )


def image_upload_audit_entry_factory(
    request: Request,
    current_user: User,
) -> Callable[[StoredImage], AuditLogEntry]:
    """업로드 감사 로그 엔트리를 생성하는 팩토리를 반환합니다.

    Args:
        request (Request): 현재 요청 객체.
        current_user (User): 요청을 보낸 현재 사용자 객체.

    Returns:
        Callable[[StoredImage], AuditLogEntry]: flush된 StoredImage로 감사 로그를 만드는 함수.
    """

    def factory(stored_image: StoredImage) -> AuditLogEntry:
        return AuditLogEntry(
            request_id=request_id_from_request(request),
            actor_user_id=current_user.user_id,
            action="image.upload",
            resource_type="image",
            resource_id=stored_image.id,
            before=None,
            after={
                "id": stored_image.id,
                "image_type": stored_image.image_type,
                "restaurant_id": stored_image.restaurant_id,
                "stored_name": stored_image.stored_name,
                "original_format": stored_image.original_format,
                "original_bytes": stored_image.original_bytes,
                "width": stored_image.width,
                "height": stored_image.height,
                "public_url": stored_image.public_url,
            },
        )

    return factory


def resolve_public_image_path(file_name: str) -> str:
    """공개 이미지 파일명을 검증하고 실제 파일 경로를 반환합니다.

    파일명은 uuid hex + (.thumb) + .webp 형식만 허용해 경로 조작을 차단합니다.

    Args:
        file_name (str): 조회할 webp 파일명.

    Returns:
        str: 공개 디렉터리 안의 파일 절대 경로.

    Raises:
        HTTPException(404): 파일명이 규칙에 맞지 않거나 파일이 없는 경우.
    """
    if not PUBLIC_IMAGE_NAME_PATTERN.fullmatch(file_name):
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND,
            detail="해당 이미지가 존재하지 않습니다.",
        )

    file_path = os.path.join(Config.IMAGE_PUBLIC_DIR, file_name)
    if not os.path.isfile(file_path):
        raise HTTPException(
            status_code=Config.HttpStatus.NOT_FOUND,
            detail="해당 이미지가 존재하지 않습니다.",
        )
    return file_path
