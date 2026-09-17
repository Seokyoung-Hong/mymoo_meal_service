"""Restaurant BLE reader registration model."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RestaurantReader(Base):
    """식당에 등록된 BLE 리더(ESP32-C3) 한 대.

    ``bt_mac``은 구분자 없는 소문자 hex 12자로 정규화해 저장한다. ``last_config_ctr``는
    마지막으로 발급한 설정 QR의 카운터이며, 리더는 저장값보다 큰 ctr만 받아들이므로
    같은 초에 두 번 발급해도 값이 반드시 커져야 한다.
    """

    __tablename__ = "restaurant_reader"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    restaurant_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("Restaurant.id", ondelete="CASCADE"),
        nullable=False,
    )
    bt_mac: Mapped[str] = mapped_column(String(12), nullable=False, unique=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_config_ctr: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    last_config_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (Index("restaurant_reader_restaurant_index", "restaurant_id"),)
