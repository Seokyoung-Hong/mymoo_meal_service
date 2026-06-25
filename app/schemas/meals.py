"""식사 관련 데이터 모델 스키마 모듈

이 모듈은 식사 관련 데이터 모델을 정의합니다. Pydantic을 사용하여 데이터 유효성 검사를 수행하며,
식사 종류, 식사 등록, 식사 응답, 메뉴 수정 등의 다양한 모델을 포함합니다.

클래스 목록:
    - MealType: 식사 종류를 나타내는 Enum 클래스
    - BaseMeal: 공통 Meal 모델
    - MealRegister: 식사 등록 모델
    - MealRegisterResponse: 식사 등록 응답 모델
    - MealResponse: 개별 식사 응답 모델
    - MenuEdit: 메뉴 수정 모델
    - MealEditResponse: 식사 수정 응답 모델
"""

from datetime import date, datetime
from typing import Annotated
from enum import Enum
from pydantic import AnyUrl, BaseModel, TypeAdapter, field_validator

from app.schemas.base import Timestamp as Tsp

Timestamp = Annotated[datetime, Tsp]
url_adapter = TypeAdapter(AnyUrl)


class MealType(str, Enum):
    """식사 종류를 나타내는 Enum 클래스

    Attributes:
        breakfast (str): 아침 식사
        lunch (str): 점심 식사
        dinner (str): 저녁 식사
    """

    breakfast = "breakfast"
    lunch = "lunch"
    dinner = "dinner"


class BaseMeal(BaseModel):
    """공통 Meal 모델

    Attributes:
        served_date (date): 식사가 제공되는 날짜
        main_menu (str): 대표 메뉴
        side_menus (list[str]): 보조 메뉴 목록
        image_url (str | None): 메뉴 이미지 URL
        meal_type (MealType): 식사 종류
    """

    served_date: date
    main_menu: str
    side_menus: list[str]
    image_url: str | None = None
    meal_type: MealType

    @field_validator("main_menu")
    @classmethod
    def validate_main_menu(cls, value: str) -> str:
        """대표 메뉴는 빈 문자열일 수 없습니다."""
        if not value.strip():
            raise ValueError("main_menu must not be empty")
        return value

    @field_validator("side_menus")
    @classmethod
    def validate_side_menus(cls, value: list[str]) -> list[str]:
        """보조 메뉴는 문자열 리스트이며 빈 리스트는 허용합니다."""
        for menu in value:
            if not menu.strip():
                raise ValueError("side_menus must not contain empty items")
        return value

    @field_validator("image_url")
    @classmethod
    def validate_image_url(cls, value: str | None) -> str | None:
        """이미지 URL은 null이거나 유효한 URL이어야 합니다."""
        if value is None:
            return value
        return str(url_adapter.validate_python(value))


class MealRegister(BaseMeal):
    """식사 등록 모델

    BaseMeal을 상속받아 추가적인 필드를 포함하지 않음
    """


class MealUpdate(BaseModel):
    """식사 부분 수정 모델."""

    restaurant_id: int | None = None
    served_date: date | None = None
    main_menu: str | None = None
    side_menus: list[str] | None = None
    image_url: str | None = None
    meal_type: MealType | None = None

    @field_validator("main_menu")
    @classmethod
    def validate_main_menu(cls, value: str | None) -> str | None:
        """대표 메뉴가 제공되면 빈 문자열일 수 없습니다."""
        if value is not None and not value.strip():
            raise ValueError("main_menu must not be empty")
        return value

    @field_validator("side_menus")
    @classmethod
    def validate_side_menus(cls, value: list[str] | None) -> list[str] | None:
        """보조 메뉴가 제공되면 빈 항목을 허용하지 않습니다."""
        if value is None:
            return value
        for menu in value:
            if not menu.strip():
                raise ValueError("side_menus must not contain empty items")
        return value

    @field_validator("image_url")
    @classmethod
    def validate_image_url(cls, value: str | None) -> str | None:
        """이미지 URL은 null이거나 유효한 URL이어야 합니다."""
        if value is None:
            return value
        return str(url_adapter.validate_python(value))


class MealRegisterResponse(BaseModel):
    """식사 등록 응답 모델

    Attributes:
        id (int): 식사 ID
        restaurant_id (int): 식당 ID
        meal_type (MealType): 식사 종류
        registered_at (Timestamp): 등록 시간
    """

    id: int
    restaurant_id: int
    meal_type: MealType
    served_date: date
    main_menu: str
    side_menus: list[str]
    image_url: str | None = None
    registered_at: Timestamp


class MealResponse(BaseMeal):
    """개별 식사 응답 모델

    Attributes:
        id (int): 식사 ID
        registered_at (Timestamp): 등록 시간
        restaurant_id (int): 식당 ID
        restaurant_name (str): 식당 이름
        updated_at (Timestamp): 수정 시간
    """

    id: int
    registered_at: Timestamp
    restaurant_id: int
    restaurant_name: str
    updated_at: Timestamp


class MenuEdit(BaseModel):
    """메뉴 수정 모델

    Attributes:
        menu (str | list[str]): 수정할 메뉴 목록
    """

    menu: str | list[str]


class MealEditResponse(BaseModel):
    """식사 수정 응답 모델

    Attributes:
        id (int): 식사 ID
        restaurant_id (int): 식당 ID
        meal_type (MealType): 식사 종류
        menu (list[str]): 메뉴 목록
    """

    id: int
    restaurant_id: int
    meal_type: MealType
    menu: list[str]
