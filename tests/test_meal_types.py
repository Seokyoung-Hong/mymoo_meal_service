"""Meal type schema validation tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app.schemas.meals import MealRegister, MealType, MealUpdate


def meal_payload(meal_type: str) -> dict[str, object]:
    """Build a Task 4 meal schema payload for the requested meal type."""
    return {
        "served_date": "2026-06-24",
        "main_menu": "김치찌개",
        "side_menus": ["쌀밥", "깍두기"],
        "image_url": "https://example.com/meal.jpg",
        "meal_type": meal_type,
    }


@pytest.mark.parametrize("meal_type", ["breakfast", "lunch", "dinner"])
def test_meal_register_accepts_supported_meal_types(meal_type: str) -> None:
    meal = MealRegister.model_validate(meal_payload(meal_type))

    assert meal.meal_type == MealType(meal_type)


@pytest.mark.parametrize("meal_type", ["breakfast", "lunch", "dinner"])
def test_meal_update_accepts_supported_meal_types(meal_type: str) -> None:
    meal = MealUpdate.model_validate(
        {
            **meal_payload(meal_type),
            "restaurant_id": 1,
        }
    )

    assert meal.meal_type == MealType(meal_type)


@pytest.mark.parametrize("schema", [MealRegister, MealUpdate])
def test_meal_schemas_reject_brunch(schema: type[MealRegister | MealUpdate]) -> None:
    payload = meal_payload("brunch")
    if schema is MealUpdate:
        payload["restaurant_id"] = 1

    with pytest.raises(ValidationError):
        _ = schema.model_validate(payload)


async def test_api_rejects_brunch_meal_type(async_client: AsyncClient) -> None:
    response = await async_client.post("/meals/1", json=meal_payload("brunch"))

    assert response.status_code == 422
