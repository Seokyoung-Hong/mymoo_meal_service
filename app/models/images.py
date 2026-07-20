"""업로드된 이미지의 저장 정보를 추적하는 SQLAlchemy 모델을 정의합니다.

원본과 공개용 webp 파생본의 파일명, 용도(image_type), 업로더, 원본 메타데이터를
기록하며 고아 파일 정리 잡이 이 테이블을 기준으로 미참조 이미지를 정리합니다.
현재는 식단 사진("meal") 용도만 사용하지만, 프로필·리뷰 사진 등으로 확장할 수
있도록 이미지 관리를 특정 도메인에 종속시키지 않습니다.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class StoredImage(Base):
    """업로드된 이미지의 저장 정보를 기록하는 클래스.

    Attributes:
        id (int): 이미지의 고유 ID.
        stored_name (str): 공개 디렉터리에 저장된 webp 파일명 (uuid.webp).
        image_type (str): 이미지 용도 (예: "meal").
        restaurant_id (int | None): 이미지가 속한 식당의 ID (식당 용도일 때만).
        uploader_id (int | None): 업로드한 사용자의 ID (탈퇴 시 NULL).
        original_name (str): 원본 디렉터리에 저장된 파일명 (uuid.<ext>).
        original_format (str): Pillow가 감지한 원본 포맷 (예: jpeg, png).
        original_bytes (int): 원본 파일 크기 (바이트).
        width (int): 원본 이미지 가로 픽셀 수.
        height (int): 원본 이미지 세로 픽셀 수.
        public_url (str): 업로드 응답으로 반환한 공개 URL 원문.
        created_at (datetime): 업로드된 시간.
    """

    __tablename__ = "image"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stored_name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    image_type: Mapped[str] = mapped_column(String(32), nullable=False)
    restaurant_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("Restaurant.id", ondelete="CASCADE"),
        nullable=True,
    )
    uploader_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("User.id", ondelete="SET NULL"),
        nullable=True,
    )
    original_name: Mapped[str] = mapped_column(String(64), nullable=False)
    original_format: Mapped[str] = mapped_column(String(16), nullable=False)
    original_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    public_url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("image_image_type_index", "image_type"),
        Index("image_restaurant_id_index", "restaurant_id"),
        Index("image_created_at_index", "created_at"),
    )
