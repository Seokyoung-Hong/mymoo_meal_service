"""이미지 업로드 API의 응답 스키마를 정의합니다."""

from __future__ import annotations

from pydantic import BaseModel


class ImageUploadResponse(BaseModel):
    """이미지 업로드 결과를 나타내는 스키마.

    Attributes:
        image_id (int): 업로드된 이미지의 고유 ID.
        image_type (str): 이미지 용도 (예: "meal").
        image_url (str): 전체화면용 webp 파생본의 공개 URL (원본 비율 유지).
            식단 등록·수정 시 image_url 필드에 그대로 사용합니다.
        thumbnail_url (str): 썸네일 webp 파생본의 공개 URL (설정 비율 중앙 크롭).
            카드·목록 UI에서 사용합니다.
        width (int): 전체화면용 파생본 가로 픽셀 수.
        height (int): 전체화면용 파생본 세로 픽셀 수.
        thumbnail_width (int): 썸네일 파생본 가로 픽셀 수.
        thumbnail_height (int): 썸네일 파생본 세로 픽셀 수.
    """

    image_id: int
    image_type: str
    image_url: str
    thumbnail_url: str
    width: int
    height: int
    thumbnail_width: int
    thumbnail_height: int
