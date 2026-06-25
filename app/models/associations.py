"""이 모듈은 SQLAlchemy를 사용하여 데이터베이스 테이블 간의 관계를 정의합니다.

특히, Restaurant와 User 사이의 Many-to-Many 관계를 정의하는 테이블을 포함합니다.
"""

from sqlalchemy import Column, BigInteger, Table, ForeignKey

from app.database import Base

# Restaurant와 User 사이의 Many-to-Many 관계를 정의하는 테이블
restaurant_manager_association = Table(
    "RestaurantManager",
    Base.metadata,
    Column(
        "restaurant_id",
        BigInteger,
        ForeignKey("Restaurant.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "user_id",
        BigInteger,
        ForeignKey("User.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)
