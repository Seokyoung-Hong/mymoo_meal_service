"""Pricing policy SQLAlchemy models."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class RestaurantPricingPolicy(Base):
    """Flexible price policy scoped to a restaurant, meal type, or served date."""

    __tablename__ = "restaurant_pricing_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    restaurant_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("Restaurant.id", ondelete="CASCADE"),
        nullable=False,
    )
    policy_type: Mapped[str] = mapped_column(String(32), nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    meal_type_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("meal_type.id"),
        nullable=True,
    )
    served_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    created_at: Mapped[datetime] = mapped_column(
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

    restaurant = relationship("Restaurant")
    meal_type = relationship("MealType")

    __table_args__ = (
        CheckConstraint("price > 0", name="pricing_policy_price_positive_check"),
        CheckConstraint(
            "policy_type IN ('restaurant_fixed', 'meal_type_fixed', 'date_specific')",
            name="pricing_policy_type_check",
        ),
        CheckConstraint(
            "(policy_type = 'restaurant_fixed' AND meal_type_id IS NULL AND served_date IS NULL) OR "
            "(policy_type = 'meal_type_fixed' AND meal_type_id IS NOT NULL AND served_date IS NULL) OR "
            "(policy_type = 'date_specific' AND served_date IS NOT NULL)",
            name="pricing_policy_scope_check",
        ),
        Index("pricing_policy_restaurant_index", "restaurant_id"),
        Index("pricing_policy_meal_type_index", "meal_type_id"),
        Index("pricing_policy_served_date_index", "served_date"),
        Index(
            "pricing_active_restaurant_fixed_unique",
            "restaurant_id",
            unique=True,
            sqlite_where=text("is_active = 1 AND policy_type = 'restaurant_fixed'"),
            postgresql_where=text(
                "is_active = true AND policy_type = 'restaurant_fixed'"
            ),
        ),
        Index(
            "pricing_active_meal_type_fixed_unique",
            "restaurant_id",
            "meal_type_id",
            unique=True,
            sqlite_where=text("is_active = 1 AND policy_type = 'meal_type_fixed'"),
            postgresql_where=text(
                "is_active = true AND policy_type = 'meal_type_fixed'"
            ),
        ),
        Index(
            "pricing_active_date_type_unique",
            "restaurant_id",
            "meal_type_id",
            "served_date",
            unique=True,
            sqlite_where=text(
                "is_active = 1 AND policy_type = 'date_specific' AND meal_type_id IS NOT NULL"
            ),
            postgresql_where=text(
                "is_active = true AND policy_type = 'date_specific' AND meal_type_id IS NOT NULL"
            ),
        ),
        Index(
            "pricing_active_date_unique",
            "restaurant_id",
            "served_date",
            unique=True,
            sqlite_where=text(
                "is_active = 1 AND policy_type = 'date_specific' AND meal_type_id IS NULL"
            ),
            postgresql_where=text(
                "is_active = true AND policy_type = 'date_specific' AND meal_type_id IS NULL"
            ),
        ),
    )
