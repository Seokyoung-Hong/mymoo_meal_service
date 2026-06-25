"""이 모듈은 사용자 정보를 저장하는 User 클래스를 정의합니다."""

from datetime import datetime, timezone
from typing import List, Protocol

from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import DateTime, Integer, event, String, func
from sqlalchemy.orm.session import object_session

from app.database import Base
from app.models.associations import restaurant_manager_association


class Restaurant(Protocol):
    """User 모델 타입 힌트용 Restaurant 프로토콜입니다."""


class RestaurantSubmission(Protocol):
    """User 모델 타입 힌트용 RestaurantSubmission 프로토콜입니다."""


class User(Base):
    """사용자 정보를 저장하는 클래스 (User API 연동)

    속성:
        id (int): 사용자의 고유 ID
        user_id (str): 사용자의 Keycloak ID
        created_at (datetime): 사용자가 생성된 시간
        owned_restaurants (List[Restaurant]): 사용자가 소유한 레스토랑 목록
        managed_restaurants (List[Restaurant]): 사용자가 관리하는 레스토랑 목록
        submitted_restaurants (List[RestaurantSubmission]): 사용자가 제출한 레스토랑 제출 목록
    """

    __tablename__ = "User"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String, unique=True, nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),  # 앱 레벨
        server_default=func.now(),  # DB 레벨
    )

    owned_restaurants: Mapped[List["Restaurant"]] = relationship(
        "Restaurant", back_populates="owner_user"
    )
    managed_restaurants: Mapped[List["Restaurant"]] = relationship(
        "Restaurant",
        secondary=restaurant_manager_association,
        back_populates="managers",
        cascade="all",
        passive_deletes=True,
    )
    submitted_restaurants: Mapped[List["RestaurantSubmission"]] = relationship(
        "RestaurantSubmission",
        back_populates="submitter_user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


@event.listens_for(User, "before_delete")
def _user_before_delete(_, __, target):
    """사용자 삭제 시 관련 레스토랑을 소프트 삭제하고 관리자 목록을 정리합니다."""
    session = object_session(target)
    if session is None:
        return

    for restaurant in list(target.owned_restaurants):
        restaurant.soft_delete()
        session.add(restaurant)

    for restaurant in list(target.managed_restaurants):
        if target in restaurant.managers:
            restaurant.managers.remove(target)
            session.add(restaurant)
