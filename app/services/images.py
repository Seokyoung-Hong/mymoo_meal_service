"""이미지 업로드 파일의 검증·변환·저장을 담당하는 서비스 모듈입니다.

업로드 바이트를 검증(용량·디코딩 가능 여부·픽셀 수)하고, EXIF 회전을 보정한 뒤
공개용 webp 파생본 2종을 생성합니다:
- 전체화면용(full): 원본 비율 유지, 긴 변 IMAGE_FULL_SIZE 이하
- 썸네일(thumb): IMAGE_TARGET_RATIO 비율로 중앙 크롭, 긴 변 IMAGE_THUMBNAIL_SIZE 이하
원본은 비공개 디렉터리에 그대로 보관해 추후 정책 변경 시 재처리할 수 있습니다.
`process_image`는 순수 동기 함수이므로 라우터에서는 스레드로 실행해야 합니다.

이미지 용도는 image_type("meal" 등)으로 구분하며, 특정 도메인(식단)에
종속되지 않는 범용 이미지 관리 계층입니다.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from uuid import uuid4

import anyio.to_thread
from fastapi import HTTPException, Request, UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config, logger
from app.models.images import StoredImage
from app.services.audit import AuditLogEntry, add_audit_log


READ_CHUNK_BYTES = 1024 * 1024

# 지원하는 이미지 용도. 새 용도 추가 시 여기와
# app/services/image_cleanup.py의 참조 검사 레지스트리에 함께 등록합니다.
MEAL_IMAGE_TYPE = "meal"
SUPPORTED_IMAGE_TYPES = frozenset({MEAL_IMAGE_TYPE})


class ImageUploadError(Exception):
    """이미지 업로드 처리 중 발생하는 예외의 기반 클래스."""


class ImageTooLargeError(ImageUploadError):
    """업로드 파일이 허용 용량을 초과했을 때 발생하는 예외."""


class ImageTooManyPixelsError(ImageUploadError):
    """이미지 픽셀 수가 허용 한도를 초과했을 때 발생하는 예외."""


class ImageInvalidError(ImageUploadError):
    """이미지로 디코딩할 수 없는 파일일 때 발생하는 예외."""


@dataclass(frozen=True)
class ImageVariant:
    """webp 파생본 한 종의 크기와 바이트.

    Attributes:
        width (int): 파생본 가로 픽셀 수.
        height (int): 파생본 세로 픽셀 수.
        webp_bytes (bytes): 파생본 webp 바이트.
    """

    width: int
    height: int
    webp_bytes: bytes


@dataclass(frozen=True)
class ProcessedImage:
    """검증·변환이 끝난 이미지 처리 결과.

    Attributes:
        original_format (str): Pillow가 감지한 원본 포맷 (소문자, 예: jpeg).
        width (int): EXIF 회전 보정 전 원본 가로 픽셀 수.
        height (int): EXIF 회전 보정 전 원본 세로 픽셀 수.
        full (ImageVariant): 전체화면용 파생본 (원본 비율 유지).
        thumbnail (ImageVariant): 썸네일 파생본 (설정 비율로 중앙 크롭).
    """

    original_format: str
    width: int
    height: int
    full: ImageVariant
    thumbnail: ImageVariant


async def read_upload_within_limit(upload: UploadFile) -> bytes:
    """업로드 파일을 용량 한도 안에서 읽어 바이트로 반환합니다.

    Args:
        upload (UploadFile): 업로드된 multipart 파일 객체.

    Returns:
        bytes: 읽어들인 파일 전체 바이트.

    Raises:
        ImageTooLargeError: 누적 크기가 허용 용량을 초과한 경우.
        ImageInvalidError: 파일 내용이 비어 있는 경우.
    """
    max_bytes = Config.IMAGE_MAX_UPLOAD_BYTES
    if upload.size is not None and upload.size > max_bytes:
        raise ImageTooLargeError

    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(READ_CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            raise ImageTooLargeError
        chunks.append(chunk)

    if total == 0:
        raise ImageInvalidError
    return b"".join(chunks)


def _parse_target_ratio(value: str) -> tuple[int, int]:
    """비율 설정 문자열("W:H" 형식)을 정수 쌍으로 파싱합니다.

    Args:
        value (str): 비율 설정 문자열 (예: "1:1", "4:3").

    Returns:
        tuple[int, int]: (가로 비율, 세로 비율). 파싱 실패 시 (1, 1) 반환.
    """
    try:
        width_text, height_text = value.split(":", 1)
        ratio = (int(width_text), int(height_text))
    except ValueError:
        ratio = (0, 0)
    if ratio[0] <= 0 or ratio[1] <= 0:
        logger.warning(
            "IMAGE_TARGET_RATIO 설정이 올바르지 않아 1:1을 사용합니다: %s", value
        )
        return (1, 1)
    return ratio


def _target_canvas_size(
    width: int,
    height: int,
    max_long_side: int,
) -> tuple[int, int]:
    """원본 크기와 비율 설정으로 크롭 파생본 캔버스 크기를 계산합니다.

    원본 안에 들어가는 최대 크롭 영역을 목표 비율로 잡고,
    긴 변이 max_long_side를 넘지 않도록 축소합니다(업스케일 없음).

    Args:
        width (int): 원본(EXIF 보정 후) 가로 픽셀 수.
        height (int): 원본(EXIF 보정 후) 세로 픽셀 수.
        max_long_side (int): 파생본 긴 변의 최대 픽셀 수.

    Returns:
        tuple[int, int]: 파생본 (가로, 세로) 픽셀 크기.
    """
    ratio_w, ratio_h = _parse_target_ratio(Config.IMAGE_TARGET_RATIO)
    crop_scale = min(width / ratio_w, height / ratio_h)
    crop_long_side = crop_scale * max(ratio_w, ratio_h)
    target_long_side = min(max_long_side, int(crop_long_side))
    target_w = max(1, round(target_long_side * ratio_w / max(ratio_w, ratio_h)))
    target_h = max(1, round(target_long_side * ratio_h / max(ratio_w, ratio_h)))
    return (target_w, target_h)


def _encode_webp(image: Image.Image) -> bytes:
    """이미지를 설정 품질의 webp 바이트로 인코딩합니다.

    Args:
        image (Image.Image): 인코딩할 Pillow 이미지.

    Returns:
        bytes: webp 바이트.
    """
    if image.mode != "RGB":
        image = image.convert("RGB")
    buffer = BytesIO()
    image.save(buffer, format="WEBP", quality=Config.IMAGE_WEBP_QUALITY)
    return buffer.getvalue()


def process_image(data: bytes) -> ProcessedImage:
    """이미지 바이트를 검증하고 공개용 webp 파생본 2종을 생성합니다.

    확장자와 Content-Type은 신뢰하지 않고 실제 디코딩 가능 여부로만 판정하며,
    픽셀 수 검사는 전체 디코딩 전에 헤더 정보로 수행합니다
    (Pillow 기본 MAX_IMAGE_PIXELS는 2차 방어선으로 유지).
    전체화면용은 원본 비율을 유지하고, 썸네일은 설정 비율로 중앙 크롭합니다.
    두 파생본 모두 업스케일하지 않습니다.

    Args:
        data (bytes): 업로드된 원본 이미지 바이트.

    Returns:
        ProcessedImage: 원본 메타데이터와 webp 파생본 2종을 담은 처리 결과.

    Raises:
        ImageInvalidError: 이미지로 디코딩할 수 없는 경우.
        ImageTooManyPixelsError: 픽셀 수가 IMAGE_MAX_PIXELS를 초과한 경우.
    """
    try:
        image = Image.open(BytesIO(data))
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as error:
        raise ImageInvalidError from error

    original_format = (image.format or "unknown").lower()
    width, height = image.size
    if width * height > Config.IMAGE_MAX_PIXELS:
        raise ImageTooManyPixelsError

    try:
        transposed = ImageOps.exif_transpose(image)

        # 전체화면용: 원본 비율 유지, 긴 변 제한 (Image.thumbnail은 업스케일 없음)
        full_image = transposed.copy()
        full_image.thumbnail(
            (Config.IMAGE_FULL_SIZE, Config.IMAGE_FULL_SIZE),
            Image.Resampling.LANCZOS,
        )

        # 썸네일: 설정 비율로 중앙 크롭 + 리사이즈
        thumbnail_image = ImageOps.fit(
            transposed,
            _target_canvas_size(
                transposed.width,
                transposed.height,
                Config.IMAGE_THUMBNAIL_SIZE,
            ),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )

        full_variant = ImageVariant(
            width=full_image.width,
            height=full_image.height,
            webp_bytes=_encode_webp(full_image),
        )
        thumbnail_variant = ImageVariant(
            width=thumbnail_image.width,
            height=thumbnail_image.height,
            webp_bytes=_encode_webp(thumbnail_image),
        )
    except (Image.DecompressionBombError, OSError) as error:
        raise ImageInvalidError from error

    return ProcessedImage(
        original_format=original_format,
        width=width,
        height=height,
        full=full_variant,
        thumbnail=thumbnail_variant,
    )


def thumbnail_name_for(stored_name: str) -> str:
    """전체화면용 파일명에서 썸네일 파일명을 유도합니다.

    Args:
        stored_name (str): 공개 디렉터리의 전체화면용 webp 파일명 (uuid.webp).

    Returns:
        str: 썸네일 webp 파일명 (uuid.thumb.webp).
    """
    return f"{stored_name.removesuffix('.webp')}.thumb.webp"


def save_image_files(
    image_id: str,
    original: bytes,
    processed: ProcessedImage,
) -> tuple[str, str]:
    """원본과 webp 파생본 2종을 각각의 디렉터리에 저장합니다.

    Args:
        image_id (str): 파일들이 공유하는 uuid hex 식별자.
        original (bytes): 업로드된 원본 바이트.
        processed (ProcessedImage): 변환된 이미지 처리 결과.

    Returns:
        tuple[str, str]: (원본 파일명, 공개 전체화면용 파일명).
            썸네일 파일명은 thumbnail_name_for()로 유도합니다.

    Raises:
        OSError: 파일 저장에 실패한 경우 (이미 저장한 파일은 삭제 후 재발생).
    """
    original_name = f"{image_id}.{processed.original_format}"
    stored_name = f"{image_id}.webp"
    os.makedirs(Config.IMAGE_ORIGINALS_DIR, exist_ok=True)
    os.makedirs(Config.IMAGE_PUBLIC_DIR, exist_ok=True)

    targets = (
        (
            os.path.join(Config.IMAGE_ORIGINALS_DIR, original_name),
            original,
        ),
        (
            os.path.join(Config.IMAGE_PUBLIC_DIR, stored_name),
            processed.full.webp_bytes,
        ),
        (
            os.path.join(Config.IMAGE_PUBLIC_DIR, thumbnail_name_for(stored_name)),
            processed.thumbnail.webp_bytes,
        ),
    )
    try:
        for path, content in targets:
            with open(path, "wb") as target_file:
                target_file.write(content)
    except OSError:
        delete_image_files(original_name, stored_name)
        raise
    return original_name, stored_name


def delete_image_files(original_name: str, stored_name: str) -> None:
    """원본과 파생본 2종 파일을 삭제합니다. 이미 없는 파일은 무시합니다.

    Args:
        original_name (str): 원본 디렉터리의 파일명.
        stored_name (str): 공개 디렉터리의 전체화면용 파일명.
    """
    for directory, file_name in (
        (Config.IMAGE_ORIGINALS_DIR, original_name),
        (Config.IMAGE_PUBLIC_DIR, stored_name),
        (Config.IMAGE_PUBLIC_DIR, thumbnail_name_for(stored_name)),
    ):
        try:
            os.unlink(os.path.join(directory, file_name))
        except FileNotFoundError:
            pass


def build_public_image_url(request: Request, stored_name: str) -> str:
    """공개 파생본에 접근할 수 있는 절대 URL을 생성합니다.

    IMAGE_PUBLIC_BASE_URL이 설정되어 있으면 이를 사용하고,
    없으면 요청의 base_url(root_path 포함)을 사용합니다.

    Args:
        request (Request): 현재 요청 객체.
        stored_name (str): 공개 디렉터리의 webp 파일명.

    Returns:
        str: 익명 접근 가능한 이미지 절대 URL.
    """
    base = Config.IMAGE_PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}/images/{stored_name}"


async def prepare_image(upload: UploadFile) -> tuple[bytes, ProcessedImage]:
    """업로드 파일을 읽고 검증·변환까지 수행합니다.

    CPU 바운드인 Pillow 처리는 스레드에서 실행해 이벤트 루프를 막지 않습니다.

    Args:
        upload (UploadFile): 업로드된 multipart 파일 객체.

    Returns:
        tuple[bytes, ProcessedImage]: (원본 바이트, 변환 결과).

    Raises:
        ImageTooLargeError: 파일이 허용 용량을 초과한 경우.
        ImageTooManyPixelsError: 픽셀 수가 허용 한도를 초과한 경우.
        ImageInvalidError: 이미지로 디코딩할 수 없는 경우.
    """
    data = await read_upload_within_limit(upload)
    processed = await anyio.to_thread.run_sync(process_image, data)
    return data, processed


async def store_image(
    data: bytes,
    processed: ProcessedImage,
) -> tuple[str, str]:
    """새 uuid로 원본·파생본 파일 쌍을 저장합니다.

    Args:
        data (bytes): 업로드된 원본 바이트.
        processed (ProcessedImage): 변환된 이미지 처리 결과.

    Returns:
        tuple[str, str]: (원본 파일명, 공개 파생본 파일명).

    Raises:
        HTTPException(500): 파일 저장에 실패한 경우.
    """
    image_id = uuid4().hex
    try:
        return await anyio.to_thread.run_sync(
            save_image_files, image_id, data, processed
        )
    except OSError as error:
        logger.error("이미지 파일 저장 중 예외 발생: %s", error)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="이미지 저장에 실패했습니다.",
        ) from error


async def register_image_transaction(
    db: AsyncSession,
    stored_image: StoredImage,
    audit_entry_factory: Callable[[StoredImage], AuditLogEntry] | None = None,
) -> None:
    """이미지 메타데이터를 감사 로그와 함께 저장하는 트랜잭션 처리.

    실패 시 트랜잭션을 롤백하고 이미 저장된 파일 쌍도 함께 삭제합니다.

    Args:
        db (AsyncSession): SQLAlchemy 비동기 세션 객체.
        stored_image (StoredImage): 저장할 이미지 메타데이터 객체.
        audit_entry_factory (Callable[[StoredImage], AuditLogEntry] | None):
            flush 후 감사 로그를 생성하는 함수.

    Raises:
        HTTPException(500): 데이터베이스 처리 중 오류가 발생한 경우.
    """
    try:
        db.add(stored_image)
        await db.flush()
        if audit_entry_factory is not None:
            add_audit_log(db, audit_entry_factory(stored_image))
        await db.commit()
        await db.refresh(stored_image)
    except Exception as error:
        await db.rollback()
        delete_image_files(stored_image.original_name, stored_image.stored_name)
        logger.error("이미지 업로드 DB 처리 중 예외 발생: %s", error)
        raise HTTPException(
            status_code=Config.HttpStatus.INTERNAL_SERVER_ERROR,
            detail="서버 내부 오류 발생",
        ) from error
