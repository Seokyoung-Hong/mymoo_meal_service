"""이미지 업로드와 공개 서빙 엔드포인트를 정의하는 모듈입니다.

이미지 관리는 특정 도메인에 종속되지 않는 범용 기능입니다. 용도는 multipart
폼 필드 image_type("meal" 등)으로 구분하며, 용도별 권한 검증을 거쳐 업로드합니다.
현재는 식단 사진("meal") 용도만 지원합니다: 식당 owner/manager가 업로드하고,
근로자 앱은 인증 없이 공개 URL로 이미지를 조회합니다.
검증·변환·저장·트랜잭션 로직은 app/services/images.py에,
라우터 보조 함수는 app/utils/images.py에 위임합니다.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config, logger
from app.models import StoredImage, User
from app.schemas.base import BaseSchema
from app.schemas.images import ImageUploadResponse
from app.services.images import (
    MEAL_IMAGE_TYPE,
    ImageUploadError,
    build_public_image_url,
    prepare_image,
    register_image_transaction,
    store_image,
    thumbnail_name_for,
)
from app.utils.auth import optional_metrics_x_user_id
from app.utils.db import get_current_user, get_db
from app.utils.images import (
    authorize_image_upload,
    image_upload_audit_entry_factory,
    resolve_public_image_path,
    upload_http_error,
)

router = APIRouter(prefix="/images", tags=["Images"])


@router.post("", status_code=Config.HttpStatus.CREATED)
async def upload_image(
    request: Request,
    file: Annotated[UploadFile, File(description="업로드할 이미지 파일")],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    image_type: Annotated[
        str, Form(description='이미지 용도 (현재 "meal"만 지원)')
    ] = MEAL_IMAGE_TYPE,
    restaurant_id: Annotated[
        int | None, Form(description="식단 사진일 때 대상 식당 ID")
    ] = None,
) -> BaseSchema[ImageUploadResponse]:
    """이미지를 업로드하고 공개 접근 가능한 이미지 URL을 반환합니다.

    확장자와 무관하게 실제 이미지로 디코딩 가능한 파일만 허용하며,
    원본은 서버에 비공개 보관하고 webp 파생본 2종(전체화면용·썸네일)의 URL을
    반환합니다. "meal" 용도의 경우 반환된 image_url을 식단 등록·수정 시
    image_url 필드에 그대로 사용하며, 식당 owner/manager만 업로드할 수 있습니다.

    Args:
        request (Request): 현재 요청 객체입니다.
        file (UploadFile): 업로드할 이미지 파일입니다.
        db (AsyncSession): 비동기 DB 세션 객체입니다.
        current_user (User): 요청을 보낸 현재 사용자 객체입니다.
        image_type (str): 이미지 용도입니다 (multipart 폼 필드, 기본 "meal").
        restaurant_id (int | None): 식단 사진일 때 대상 식당 ID입니다 (multipart 폼 필드).

    Returns:
        BaseSchema[ImageUploadResponse]: 업로드된 이미지 정보를 포함한 응답 객체입니다.

    Raises:
        HTTPException(403): 업로드 권한이 없을 경우 발생합니다.
        HTTPException(404): 대상 식당이 존재하지 않을 경우 발생합니다.
        HTTPException(409): 비활성 식당에 업로드를 시도한 경우 발생합니다.
        HTTPException(413): 파일이 허용 용량을 초과한 경우 발생합니다.
        HTTPException(422): 지원하지 않는 용도, 이미지가 아닌 파일,
            픽셀 수 한도 초과의 경우 발생합니다.
        HTTPException(500): 저장 또는 데이터베이스 오류가 발생한 경우 발생합니다.
    """
    logger.info(
        "User %d attempting to upload %s image (restaurant_id=%s)",
        current_user.id,
        image_type,
        restaurant_id,
    )

    await authorize_image_upload(image_type, restaurant_id, db, current_user)

    try:
        data, processed = await prepare_image(file)
    except ImageUploadError as error:
        raise upload_http_error(error) from error

    original_name, stored_name = await store_image(data, processed)
    public_url = build_public_image_url(request, stored_name)
    thumbnail_url = build_public_image_url(request, thumbnail_name_for(stored_name))

    stored_image = StoredImage(
        stored_name=stored_name,
        image_type=image_type,
        restaurant_id=restaurant_id,
        uploader_id=current_user.id,
        original_name=original_name,
        original_format=processed.original_format,
        original_bytes=len(data),
        width=processed.width,
        height=processed.height,
        public_url=public_url,
    )
    await register_image_transaction(
        db,
        stored_image,
        image_upload_audit_entry_factory(request, current_user),
    )

    logger.info(
        "Image %s (%s) uploaded by user %d",
        stored_name,
        image_type,
        current_user.id,
    )

    return BaseSchema[ImageUploadResponse](
        data=ImageUploadResponse(
            image_id=stored_image.id,
            image_type=image_type,
            image_url=public_url,
            thumbnail_url=thumbnail_url,
            width=processed.full.width,
            height=processed.full.height,
            thumbnail_width=processed.thumbnail.width,
            thumbnail_height=processed.thumbnail.height,
        )
    )


@router.get(
    "/{file_name}",
    dependencies=[Depends(optional_metrics_x_user_id)],
    response_class=FileResponse,
)
async def get_image(file_name: str) -> FileResponse:
    """공개된 이미지 파생본을 인증 없이 제공합니다.

    식단 조회 등 공개 API에서 쓰이는 이미지이므로 익명 접근을 허용합니다.
    파일명은 uuid hex + (.thumb) + .webp 형식만 허용해 경로 조작을 차단합니다.

    Args:
        file_name (str): 조회할 webp 파일명입니다.

    Returns:
        FileResponse: webp 이미지 파일 응답입니다.

    Raises:
        HTTPException(404): 파일명이 규칙에 맞지 않거나 파일이 없을 경우 발생합니다.
    """
    return FileResponse(
        resolve_public_image_path(file_name),
        media_type="image/webp",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
