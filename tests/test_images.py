"""Generic image upload, public serving, and orphan cleanup tests."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Config
from app.models import AuditLog, Meal, MealType, Restaurant, StoredImage, User
from app.services.image_cleanup import cleanup_orphan_images
from app.utils import db as db_utils
from app.utils.scheduler import create_scheduler


PUBLIC_URL_PATTERN = re.compile(r"^http://testserver/meal/images/[0-9a-f]{32}\.webp$")


@pytest.fixture
def image_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect image storage directories into an isolated temp path."""
    originals = tmp_path / "originals"
    public = tmp_path / "public"
    originals.mkdir()
    public.mkdir()
    monkeypatch.setattr(Config, "IMAGE_DIR", str(tmp_path))
    monkeypatch.setattr(Config, "IMAGE_ORIGINALS_DIR", str(originals))
    monkeypatch.setattr(Config, "IMAGE_PUBLIC_DIR", str(public))
    return tmp_path


def make_image_bytes(
    width: int = 64,
    height: int = 48,
    fmt: str = "JPEG",
) -> bytes:
    """Create deterministic in-memory image bytes for upload tests."""
    image = Image.new("RGB", (width, height), color=(120, 180, 60))
    buffer = BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


async def seed_owned_restaurant(
    db_session: AsyncSession,
    *,
    is_active: bool = True,
) -> Restaurant:
    """Seed a restaurant owned by the default authenticated test user."""
    owner = User(id=1, user_id="test-user-sub")
    restaurant = Restaurant(
        name="Image Restaurant",
        owner_user=owner,
        is_campus=True,
        is_active=is_active,
        establishment_type="fixed_menu_restaurant",
    )
    db_session.add_all([owner, restaurant])
    await db_session.commit()
    return restaurant


async def upload_image(
    async_client: AsyncClient,
    restaurant_id: int,
    content: bytes,
    file_name: str = "photo.jpg",
) -> dict[str, object]:
    """Upload meal image bytes and return the parsed response data."""
    response = await async_client.post(
        "/images",
        data={"image_type": "meal", "restaurant_id": str(restaurant_id)},
        files={"file": (file_name, content, "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert isinstance(data, dict)
    return data


async def test_upload_image_success(
    async_client: AsyncClient,
    db_session: AsyncSession,
    image_dirs: Path,
) -> None:
    restaurant = await seed_owned_restaurant(db_session)

    data = await upload_image(async_client, restaurant.id, make_image_bytes(640, 480))

    assert PUBLIC_URL_PATTERN.fullmatch(str(data["image_url"]))
    assert data["image_type"] == "meal"
    stored_name = str(data["image_url"]).rsplit("/", 1)[1]
    image_id = stored_name.removesuffix(".webp")
    assert str(data["thumbnail_url"]).endswith(f"/images/{image_id}.thumb.webp")
    assert (image_dirs / "public" / stored_name).is_file()
    assert (image_dirs / "public" / f"{image_id}.thumb.webp").is_file()
    assert (image_dirs / "originals" / f"{image_id}.jpeg").is_file()

    stored_image = await db_session.scalar(
        select(StoredImage).where(StoredImage.stored_name == stored_name)
    )
    assert stored_image is not None
    assert stored_image.image_type == "meal"
    assert stored_image.restaurant_id == restaurant.id
    assert stored_image.uploader_id == 1
    assert stored_image.width == 640
    assert stored_image.height == 480

    audit_log = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "image.upload")
    )
    assert audit_log is not None
    assert audit_log.resource_id == str(stored_image.id)


async def test_upload_defaults_to_meal_type(
    async_client: AsyncClient,
    db_session: AsyncSession,
    image_dirs: Path,
) -> None:
    restaurant = await seed_owned_restaurant(db_session)

    response = await async_client.post(
        "/images",
        data={"restaurant_id": str(restaurant.id)},
        files={"file": ("photo.jpg", make_image_bytes(), "image/jpeg")},
    )

    assert response.status_code == 201, response.text
    assert response.json()["data"]["image_type"] == "meal"


async def test_upload_rejects_unsupported_image_type(
    async_client: AsyncClient,
    db_session: AsyncSession,
    image_dirs: Path,
) -> None:
    response = await async_client.post(
        "/images",
        data={"image_type": "profile"},
        files={"file": ("photo.jpg", make_image_bytes(), "image/jpeg")},
    )

    assert response.status_code == 422


async def test_upload_meal_type_requires_restaurant_id(
    async_client: AsyncClient,
    db_session: AsyncSession,
    image_dirs: Path,
) -> None:
    response = await async_client.post(
        "/images",
        data={"image_type": "meal"},
        files={"file": ("photo.jpg", make_image_bytes(), "image/jpeg")},
    )

    assert response.status_code == 422


async def test_upload_generates_full_and_thumbnail_variants(
    async_client: AsyncClient,
    db_session: AsyncSession,
    image_dirs: Path,
) -> None:
    restaurant = await seed_owned_restaurant(db_session)

    data = await upload_image(async_client, restaurant.id, make_image_bytes(200, 100))

    # 전체화면용: 원본 비율 유지 + 업스케일 없음 (1600px 이하이므로 그대로)
    assert data["width"] == 200
    assert data["height"] == 100
    # 썸네일: 기본 비율 1:1 중앙 크롭, 짧은 변(100px) 기준으로 업스케일 없음
    assert data["thumbnail_width"] == 100
    assert data["thumbnail_height"] == 100

    stored_name = str(data["image_url"]).rsplit("/", 1)[1]
    image_id = stored_name.removesuffix(".webp")
    with Image.open(image_dirs / "public" / stored_name) as full:
        assert full.size == (200, 100)
        assert full.format == "WEBP"
    with Image.open(image_dirs / "public" / f"{image_id}.thumb.webp") as thumb:
        assert thumb.size == (100, 100)
        assert thumb.format == "WEBP"


async def test_upload_shrinks_large_full_variant(
    async_client: AsyncClient,
    db_session: AsyncSession,
    image_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restaurant = await seed_owned_restaurant(db_session)
    monkeypatch.setattr(Config, "IMAGE_FULL_SIZE", 64)
    monkeypatch.setattr(Config, "IMAGE_THUMBNAIL_SIZE", 32)

    data = await upload_image(async_client, restaurant.id, make_image_bytes(200, 100))

    # 전체화면용: 비율(2:1) 유지하며 긴 변 64px로 축소
    assert data["width"] == 64
    assert data["height"] == 32
    # 썸네일: 1:1 크롭 후 32px로 축소
    assert data["thumbnail_width"] == 32
    assert data["thumbnail_height"] == 32


async def test_upload_rejects_oversized_file(
    async_client: AsyncClient,
    db_session: AsyncSession,
    image_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restaurant = await seed_owned_restaurant(db_session)
    monkeypatch.setattr(Config, "IMAGE_MAX_UPLOAD_BYTES", 1024)

    response = await async_client.post(
        "/images",
        data={"image_type": "meal", "restaurant_id": str(restaurant.id)},
        files={"file": ("big.jpg", make_image_bytes(600, 600), "image/jpeg")},
    )

    assert response.status_code == 413


async def test_upload_rejects_too_many_pixels(
    async_client: AsyncClient,
    db_session: AsyncSession,
    image_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restaurant = await seed_owned_restaurant(db_session)
    monkeypatch.setattr(Config, "IMAGE_MAX_PIXELS", 1000)

    response = await async_client.post(
        "/images",
        data={"image_type": "meal", "restaurant_id": str(restaurant.id)},
        files={"file": ("big.jpg", make_image_bytes(100, 100), "image/jpeg")},
    )

    assert response.status_code == 422


async def test_upload_rejects_non_image_payload(
    async_client: AsyncClient,
    db_session: AsyncSession,
    image_dirs: Path,
) -> None:
    restaurant = await seed_owned_restaurant(db_session)

    response = await async_client.post(
        "/images",
        data={"image_type": "meal", "restaurant_id": str(restaurant.id)},
        files={"file": ("not-image.jpg", b"definitely not an image", "image/jpeg")},
    )

    assert response.status_code == 422
    assert not list((image_dirs / "originals").iterdir())
    assert not list((image_dirs / "public").iterdir())
    assert await db_session.scalar(select(StoredImage)) is None


async def test_upload_requires_restaurant_permission(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
    image_dirs: Path,
) -> None:
    stranger = User(id=1, user_id="test-user-sub")
    other_owner = User(id=2, user_id="other-owner-sub")
    restaurant = Restaurant(
        name="Not Mine",
        owner_user=other_owner,
        is_campus=True,
        is_active=True,
        establishment_type="fixed_menu_restaurant",
    )
    db_session.add_all([stranger, other_owner, restaurant])
    await db_session.commit()
    await db_session.refresh(stranger)

    async def override_stranger() -> User:
        return stranger

    test_app.dependency_overrides[db_utils.get_current_user] = override_stranger

    response = await async_client.post(
        "/images",
        data={"image_type": "meal", "restaurant_id": str(restaurant.id)},
        files={"file": ("photo.jpg", make_image_bytes(), "image/jpeg")},
    )

    assert response.status_code == 403


async def test_upload_unknown_restaurant_returns_404(
    async_client: AsyncClient,
    db_session: AsyncSession,
    image_dirs: Path,
) -> None:
    response = await async_client.post(
        "/images",
        data={"image_type": "meal", "restaurant_id": "999"},
        files={"file": ("photo.jpg", make_image_bytes(), "image/jpeg")},
    )

    assert response.status_code == 404


async def test_upload_inactive_restaurant_returns_409(
    async_client: AsyncClient,
    db_session: AsyncSession,
    image_dirs: Path,
) -> None:
    restaurant = await seed_owned_restaurant(db_session, is_active=False)

    response = await async_client.post(
        "/images",
        data={"image_type": "meal", "restaurant_id": str(restaurant.id)},
        files={"file": ("photo.jpg", make_image_bytes(), "image/jpeg")},
    )

    assert response.status_code == 409


async def test_public_image_serving_is_anonymous(
    async_client: AsyncClient,
    db_session: AsyncSession,
    image_dirs: Path,
) -> None:
    restaurant = await seed_owned_restaurant(db_session)
    data = await upload_image(async_client, restaurant.id, make_image_bytes())
    stored_name = str(data["image_url"]).rsplit("/", 1)[1]
    thumbnail_name = str(data["thumbnail_url"]).rsplit("/", 1)[1]

    for file_name in (stored_name, thumbnail_name):
        response = await async_client.get(f"/images/{file_name}")

        assert response.status_code == 200, file_name
        assert response.headers["content-type"] == "image/webp"
        assert response.headers["cache-control"] == (
            "public, max-age=31536000, immutable"
        )
        with Image.open(BytesIO(response.content)) as served:
            assert served.format == "WEBP"


async def test_public_image_serving_rejects_invalid_names(
    async_client: AsyncClient,
    image_dirs: Path,
) -> None:
    for bad_name in (
        "not-a-uuid.webp",
        f"{'a' * 32}.png",
        f"{'0' * 32}.webp",  # 규칙에는 맞지만 존재하지 않는 파일
    ):
        response = await async_client.get(f"/images/{bad_name}")
        assert response.status_code == 404, bad_name


def _write_image_files(image_dirs: Path, image_id: str) -> None:
    """Write dummy original/full/thumbnail files for cleanup tests."""
    (image_dirs / "originals" / f"{image_id}.jpeg").write_bytes(b"original")
    (image_dirs / "public" / f"{image_id}.webp").write_bytes(b"public")
    (image_dirs / "public" / f"{image_id}.thumb.webp").write_bytes(b"thumb")


def _make_stored_image(
    image_id: str,
    restaurant_id: int,
    created_at: datetime,
    *,
    image_type: str = "meal",
) -> StoredImage:
    """Build a StoredImage row for cleanup tests."""
    return StoredImage(
        stored_name=f"{image_id}.webp",
        image_type=image_type,
        restaurant_id=restaurant_id,
        uploader_id=1,
        original_name=f"{image_id}.jpeg",
        original_format="jpeg",
        original_bytes=8,
        width=64,
        height=48,
        public_url=f"http://testserver/meal/images/{image_id}.webp",
        created_at=created_at,
    )


async def test_cleanup_orphan_images(
    db_session: AsyncSession,
    image_dirs: Path,
) -> None:
    restaurant = await seed_owned_restaurant(db_session)
    lunch = MealType(name="lunch")
    db_session.add(lunch)
    await db_session.flush()

    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=48)

    referenced_id = "a" * 32
    orphan_id = "b" * 32
    recent_id = "c" * 32
    unknown_type_id = "e" * 32
    for image_id in (referenced_id, orphan_id, recent_id, unknown_type_id):
        _write_image_files(image_dirs, image_id)
    db_session.add_all(
        [
            _make_stored_image(referenced_id, restaurant.id, old),
            _make_stored_image(orphan_id, restaurant.id, old),
            _make_stored_image(recent_id, restaurant.id, now),
            # 참조 검사가 등록되지 않은 용도는 오래돼도 보존되어야 함
            _make_stored_image(
                unknown_type_id, restaurant.id, old, image_type="future-type"
            ),
        ]
    )
    db_session.add(
        Meal(
            restaurant_id=restaurant.id,
            main_menu="김치찌개",
            side_menus=["밥"],
            image_url=f"http://testserver/meal/images/{referenced_id}.webp",
            menu=["김치찌개"],
            meal_type_id=lunch.id,
        )
    )
    await db_session.commit()

    session_factory = async_sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    deleted = await cleanup_orphan_images(session_factory=session_factory)

    assert deleted == 1
    # 참조 중·유예시간 내·미등록 용도 이미지는 보존, 미참조 오래된 이미지만 삭제
    assert (image_dirs / "public" / f"{referenced_id}.webp").is_file()
    assert (image_dirs / "public" / f"{referenced_id}.thumb.webp").is_file()
    assert (image_dirs / "public" / f"{recent_id}.webp").is_file()
    assert (image_dirs / "public" / f"{unknown_type_id}.webp").is_file()
    assert not (image_dirs / "public" / f"{orphan_id}.webp").exists()
    assert not (image_dirs / "public" / f"{orphan_id}.thumb.webp").exists()
    assert not (image_dirs / "originals" / f"{orphan_id}.jpeg").exists()

    remaining = (
        (await db_session.execute(select(StoredImage.stored_name))).scalars().all()
    )
    assert sorted(remaining) == sorted(
        [
            f"{referenced_id}.webp",
            f"{recent_id}.webp",
            f"{unknown_type_id}.webp",
        ]
    )


async def test_cleanup_sweeps_untracked_files(
    db_session: AsyncSession,
    image_dirs: Path,
) -> None:
    stray_id = "d" * 32
    _write_image_files(image_dirs, stray_id)
    old_timestamp = (datetime.now(timezone.utc) - timedelta(hours=48)).timestamp()
    for path in (
        image_dirs / "public" / f"{stray_id}.webp",
        image_dirs / "public" / f"{stray_id}.thumb.webp",
        image_dirs / "originals" / f"{stray_id}.jpeg",
    ):
        os.utime(path, (old_timestamp, old_timestamp))

    session_factory = async_sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    deleted = await cleanup_orphan_images(session_factory=session_factory)

    assert deleted == 1
    assert not (image_dirs / "public" / f"{stray_id}.webp").exists()
    assert not (image_dirs / "public" / f"{stray_id}.thumb.webp").exists()
    assert not (image_dirs / "originals" / f"{stray_id}.jpeg").exists()


def test_create_scheduler_registers_cleanup_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Config, "IMAGE_CLEANUP_ENABLED", True)
    scheduler = create_scheduler()
    assert scheduler.get_job("cleanup_orphan_images") is not None

    monkeypatch.setattr(Config, "IMAGE_CLEANUP_ENABLED", False)
    disabled_scheduler = create_scheduler()
    assert disabled_scheduler.get_job("cleanup_orphan_images") is None
