"""Migration safety checks for meal schema changes."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import cast
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.meals import Meal

SEOUL_TZ = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class MealDuplicateCandidate:
    """Meal fields needed before adding a served-date uniqueness constraint."""

    restaurant_id: int
    meal_type_id: int
    timestamp: datetime


@dataclass(frozen=True)
class DuplicateMealGroup:
    """Duplicate meal group that would violate future served-date uniqueness."""

    restaurant_id: int
    meal_type_id: int
    served_date: date
    count: int


def served_date_from_timestamp(timestamp: datetime) -> date:
    """Return the Asia/Seoul business date for an existing meal timestamp.

    Backfill rule: ``served_date = timestamp.astimezone(Asia/Seoul).date()``.
    Existing meal timestamps are expected to be timezone-aware UTC values. If a
    legacy value is naive, it is interpreted as UTC before converting to the
    Asia/Seoul business date.
    """
    if timestamp.tzinfo is None or timestamp.tzinfo.utcoffset(timestamp) is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    return timestamp.astimezone(SEOUL_TZ).date()


def find_duplicate_meal_candidates(
    candidates: Iterable[MealDuplicateCandidate],
) -> list[DuplicateMealGroup]:
    """Find duplicate meals grouped by restaurant, meal type, and served date."""
    counts = Counter(
        (
            candidate.restaurant_id,
            candidate.meal_type_id,
            served_date_from_timestamp(candidate.timestamp),
        )
        for candidate in candidates
    )

    return [
        DuplicateMealGroup(
            restaurant_id=restaurant_id,
            meal_type_id=meal_type_id,
            served_date=served_date,
            count=count,
        )
        for (restaurant_id, meal_type_id, served_date), count in sorted(counts.items())
        if count > 1
    ]


async def find_duplicate_meal_groups(db: AsyncSession) -> list[DuplicateMealGroup]:
    """Report existing meals that duplicate a future served-date key.

    The future key is ``(restaurant_id, meal_type_id, served_date)``, where the
    backfilled served date is derived from ``Meal.registered_at`` using
    ``served_date = timestamp.astimezone(Asia/Seoul).date()``.
    """
    result = await db.execute(
        select(Meal.restaurant_id, Meal.meal_type_id, Meal.registered_at)
    )
    rows = cast("list[tuple[int, int, datetime]]", result.all())

    return find_duplicate_meal_candidates(
        MealDuplicateCandidate(
            restaurant_id=restaurant_id,
            meal_type_id=meal_type_id,
            timestamp=registered_at,
        )
        for restaurant_id, meal_type_id, registered_at in rows
    )
