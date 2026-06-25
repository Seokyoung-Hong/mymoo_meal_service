"""이 모듈은 식단 정보와 관련된 SQLAlchemy 모델을 정의합니다.

여기에는 식사 유형과 식단 정보를 저장하는 클래스가 포함됩니다.
"""

from __future__ import annotations
from typing import Any
from datetime import date, datetime, timezone
import json

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    Text,
    Date,
    DateTime,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator, JSON

from app.database import Base
from app.models.restaurants import Restaurant


class NonEscapedJSON(TypeDecorator[Any]):
    """한글이 유니코드로 저장되지 않도록 하는 JSON 타입을 정의하는 클래스.

    Methods:
        process_bind_param(value, dialect): DB에 저장하기 전 변환 (한글이 유니코드 이스케이프 되지 않도록 설정)
        process_result_value(value, dialect): DB에서 가져올 때 변환
    """

    impl = JSON

    def process_bind_param(self, value, dialect):
        """DB에 저장하기 전 변환 (한글이 유니코드 이스케이프 되지 않도록 설정)"""
        if value is not None:
            return json.dumps(value, ensure_ascii=False)
        return value

    def process_result_value(self, value, dialect):
        """DB에서 가져올 때 변환"""
        if value is not None:
            return json.loads(value)
        return value


class MealType(Base):
    """식사 유형을 저장하는 클래스 (예: breakfast, lunch, dinner).

    Attributes:
        id (int): 식사 유형의 고유 ID.
        name (str): 식사 유형의 이름.
    """

    __tablename__ = "meal_type"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 예: "breakfast", "lunch", "dinner"
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class Meal(Base):
    """식단 정보를 저장하는 클래스.

    Attributes:
        id (int): 식단의 고유 ID.
        restaurant_id (int): 식단이 속한 레스토랑의 ID.
        served_date (date): 식사가 제공되는 날짜.
        main_menu (str): 대표 메뉴.
        side_menus (list[str]): 보조 메뉴 목록.
        image_url (str | None): 메뉴 이미지 URL.
        menu (list[str]): 기존 메뉴 리스트(전환기 호환용).
        registered_at (datetime): 식단이 등록된 시간.
        updated_at (datetime): 식단이 마지막으로 업데이트된 시간.
        meal_type_id (int): 식사 유형의 ID.
        restaurant (Restaurant): 식단이 속한 레스토랑 객체와의 관계.
        meal_type (MealType): 식단의 식사 유형 객체와의 관계.
    """

    __tablename__ = "meal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    restaurant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("Restaurant.id"), nullable=False
    )
    served_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        default=date.today,
    )
    main_menu: Mapped[str] = mapped_column(Text, nullable=False, default="")
    side_menus: Mapped[list[str]] = mapped_column(
        NonEscapedJSON,
        nullable=False,
        default=list,
    )
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    menu: Mapped[list[str]] = mapped_column(
        NonEscapedJSON, nullable=False, default=list
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    meal_type_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("meal_type.id"), nullable=False
    )

    # 관계 설정
    restaurant: Mapped[Restaurant] = relationship("Restaurant", back_populates="meals")
    meal_type: Mapped[MealType] = relationship("MealType")

    __table_args__ = (
        Index("meal_restaurant_id_index", "restaurant_id"),
        Index("meal_meal_type_id_index", "meal_type_id"),
        Index("meal_served_date_index", "served_date"),
        Index("meal_restaurant_served_date_index", "restaurant_id", "served_date"),
        Index(
            "meal_restaurant_type_served_date_index",
            "restaurant_id",
            "meal_type_id",
            "served_date",
        ),
        Index("meal_updated_at_index", "updated_at"),
    )
