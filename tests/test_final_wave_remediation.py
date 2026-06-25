"""Final-wave remediation regression tests."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.meals import Meal, MealType
from app.models.restaurants import OperatingHours, Restaurant, RestaurantSubmission
from app.models.user import User
from app.schemas.meals import MealUpdate
from app.utils.request_id import REQUEST_ID_HEADER
from scripts import check_residue


RESTAURANT_PAYLOAD = {
    "name": "Final Wave Restaurant",
    "establishment_type": "fixed_menu_restaurant",
    "price": 9000,
    "location": {
        "is_campus": True,
        "building": "Main Hall",
        "map_links": {"naver": "https://map.naver.com/final"},
        "latitude": 37.1,
        "longitude": 127.1,
    },
    "opening_time": {"start": "09:00", "end": "18:00"},
    "lunch_time": {"start": "11:30", "end": "13:30"},
}


async def seed_user(db_session: AsyncSession) -> User:
    """Persist the default authenticated user used by test overrides."""
    user = User(
        id=1,
        user_id="test-user-sub",
        created_at=datetime(2026, 6, 24, tzinfo=timezone.utc),
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def seed_meal_context(db_session: AsyncSession) -> tuple[Restaurant, Meal]:
    """Seed a restaurant, meal types, and one editable meal."""
    user = await seed_user(db_session)
    breakfast = MealType(name="breakfast")
    lunch = MealType(name="lunch")
    restaurant = Restaurant(
        name="Meal Audit Restaurant",
        owner=user.id,
        is_campus=True,
        establishment_type="fixed_menu_restaurant",
    )
    db_session.add_all([breakfast, lunch, restaurant])
    await db_session.flush()
    meal = Meal(
        restaurant_id=restaurant.id,
        meal_type_id=lunch.id,
        served_date=date(2026, 6, 24),
        main_menu="Original Lunch",
        side_menus=["rice", "kimchi"],
        image_url=None,
        menu=["Original Lunch", "rice", "kimchi"],
    )
    db_session.add(meal)
    await db_session.commit()
    return restaurant, meal


async def audit_by_request_id(
    db_session: AsyncSession,
    request_id: str,
) -> AuditLog:
    """Fetch one audit row by propagated request ID."""
    audit_log = await db_session.scalar(
        select(AuditLog).where(AuditLog.request_id == request_id)
    )
    assert audit_log is not None
    return audit_log


async def test_meal_write_paths_create_transactional_audit_logs(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    restaurant, meal = await seed_meal_context(db_session)

    create_response = await async_client.post(
        f"/meals/{restaurant.id}",
        json={
            "served_date": "2026-06-25",
            "main_menu": "Created Lunch",
            "side_menus": ["soup"],
            "image_url": None,
            "meal_type": "lunch",
        },
        headers={REQUEST_ID_HEADER: "meal-create-request-id"},
    )
    update_response = await async_client.patch(
        f"/meals/{meal.id}",
        json={"main_menu": "Patched Lunch"},
        headers={REQUEST_ID_HEADER: "meal-update-request-id"},
    )
    menu_update_response = await async_client.patch(
        f"/meals/{meal.id}/menus",
        json={"menu": "salad"},
        headers={REQUEST_ID_HEADER: "meal-menu-update-request-id"},
    )
    menu_delete_response = await async_client.request(
        "DELETE",
        f"/meals/{meal.id}/menus",
        json={"menu": "salad"},
        headers={REQUEST_ID_HEADER: "meal-menu-delete-request-id"},
    )
    delete_response = await async_client.delete(
        f"/meals/{meal.id}",
        headers={REQUEST_ID_HEADER: "meal-delete-request-id"},
    )

    assert create_response.status_code == 201
    assert update_response.status_code == 200
    assert menu_update_response.status_code == 200
    assert menu_delete_response.status_code == 204
    assert delete_response.status_code == 204

    created_audit = await audit_by_request_id(db_session, "meal-create-request-id")
    updated_audit = await audit_by_request_id(db_session, "meal-update-request-id")
    menu_updated_audit = await audit_by_request_id(
        db_session, "meal-menu-update-request-id"
    )
    menu_deleted_audit = await audit_by_request_id(
        db_session, "meal-menu-delete-request-id"
    )
    deleted_audit = await audit_by_request_id(db_session, "meal-delete-request-id")

    assert created_audit.action == "meal.create"
    assert created_audit.resource_type == "meal"
    assert created_audit.actor_user_id == "test-user-sub"
    assert created_audit.after is not None
    assert created_audit.after["main_menu"] == "Created Lunch"
    assert updated_audit.action == "meal.update"
    assert updated_audit.after is not None
    assert updated_audit.after["main_menu"] == "Patched Lunch"
    assert updated_audit.after["served_date"] == "2026-06-24"
    assert menu_updated_audit.action == "meal.menu.update"
    assert menu_updated_audit.after is not None
    assert menu_updated_audit.after["side_menus"] == ["rice", "kimchi", "salad"]
    assert menu_deleted_audit.action == "meal.menu.delete"
    assert menu_deleted_audit.after is not None
    assert menu_deleted_audit.after["side_menus"] == ["rice", "kimchi"]
    assert deleted_audit.action == "meal.delete"
    assert deleted_audit.before is not None
    assert deleted_audit.before["main_menu"] == "Patched Lunch"


async def test_meal_update_schema_and_api_support_patch_semantics(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    _, meal = await seed_meal_context(db_session)

    patch = MealUpdate.model_validate({"image_url": None})
    response = await async_client.patch(
        f"/meals/{meal.id}",
        json={"side_menus": ["pickles"]},
        headers={REQUEST_ID_HEADER: "meal-patch-semantics-request-id"},
    )

    assert patch.image_url is None
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["main_menu"] == "Original Lunch"
    assert data["side_menus"] == ["pickles"]
    assert data["served_date"] == "2026-06-24"


async def test_restaurant_write_paths_create_audit_logs_and_hide_brunch_time(
    async_client: AsyncClient,
    db_session: AsyncSession,
    fake_authenticated_user: User,
) -> None:
    fake_authenticated_user.created_at = datetime(2026, 6, 24, tzinfo=timezone.utc)
    fake_authenticated_user.auth_meal_admin = True
    await seed_user(db_session)

    submission_response = await async_client.post(
        "/restaurants/requests",
        json=RESTAURANT_PAYLOAD,
        headers={REQUEST_ID_HEADER: "restaurant-request-create-id"},
    )
    create_response = await async_client.post(
        "/restaurants/",
        json={**RESTAURANT_PAYLOAD, "owner_user_id": "restaurant-owner-sub"},
        headers={REQUEST_ID_HEADER: "restaurant-create-id"},
    )
    restaurant_id = int(create_response.json()["data"]["id"])
    update_response = await async_client.patch(
        f"/restaurants/{restaurant_id}",
        json={
            **RESTAURANT_PAYLOAD,
            "name": "Updated Final Wave Restaurant",
            "owner_user_id": "restaurant-owner-sub",
        },
        headers={REQUEST_ID_HEADER: "restaurant-update-id"},
    )
    request_id = int(submission_response.json()["data"]["request_id"])
    submission_delete_response = await async_client.delete(
        f"/restaurants/requests/{request_id}",
        headers={REQUEST_ID_HEADER: "restaurant-request-delete-id"},
    )
    delete_response = await async_client.delete(
        f"/restaurants/{restaurant_id}",
        headers={REQUEST_ID_HEADER: "restaurant-delete-id"},
    )

    assert submission_response.status_code == 201
    assert create_response.status_code == 201
    assert update_response.status_code == 200
    assert submission_delete_response.status_code == 204
    assert delete_response.status_code == 204
    assert "brunch_time" not in create_response.json()["data"]
    assert "brunch_time" not in update_response.json()["data"]

    request_create_audit = await audit_by_request_id(
        db_session, "restaurant-request-create-id"
    )
    restaurant_create_audit = await audit_by_request_id(
        db_session, "restaurant-create-id"
    )
    restaurant_update_audit = await audit_by_request_id(
        db_session, "restaurant-update-id"
    )
    request_delete_audit = await audit_by_request_id(
        db_session, "restaurant-request-delete-id"
    )
    restaurant_delete_audit = await audit_by_request_id(
        db_session, "restaurant-delete-id"
    )

    assert request_create_audit.action == "restaurant.request.create"
    assert request_create_audit.resource_type == "restaurant_request"
    assert restaurant_create_audit.action == "restaurant.create"
    assert restaurant_create_audit.resource_type == "restaurant"
    assert restaurant_update_audit.action == "restaurant.update"
    assert restaurant_update_audit.after is not None
    assert restaurant_update_audit.after["name"] == "Updated Final Wave Restaurant"
    assert request_delete_audit.action == "restaurant.request.delete"
    assert restaurant_delete_audit.action == "restaurant.delete"


async def test_restaurant_payload_rejects_brunch_time(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await seed_user(db_session)

    response = await async_client.post(
        "/restaurants/requests",
        json={
            **RESTAURANT_PAYLOAD,
            "brunch_time": {"start": "10:00", "end": "11:00"},
        },
    )

    assert response.status_code == 422


def test_residue_checker_no_longer_masks_brunch_time(tmp_path: Path) -> None:
    residue_file = tmp_path / "residue.py"
    residue_file.write_text("brunch_time = '10:00'\n", encoding="utf-8")

    matches = check_residue.scan_file(residue_file, ["brunch"], tmp_path)

    assert matches == ["residue.py:1: brunch: brunch_time = '10:00'"]


def test_residue_checker_default_paths_remain_production_scoped() -> None:
    assert "tests" not in check_residue.DEFAULT_PATHS
    assert "app" in check_residue.DEFAULT_PATHS


def test_config_default_realm_is_maemoo_safe_placeholder() -> None:
    config_path = Path(__file__).resolve().parents[1] / "app" / "config" / "config.py"

    assert 'KC_REALM = os.getenv("KC_REALM", "replace-with-keycloak-realm")' in (
        config_path.read_text(encoding="utf-8")
    )


async def test_restaurant_responses_do_not_expose_legacy_brunch_rows(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    user = await seed_user(db_session)
    restaurant = Restaurant(
        name="Legacy Brunch Row Restaurant",
        owner=user.id,
        is_campus=True,
        establishment_type="fixed_menu_restaurant",
    )
    db_session.add(restaurant)
    await db_session.flush()
    db_session.add(
        OperatingHours(
            restaurant_id=restaurant.id,
            type="brunch_time",
            start_time="10:00",
            end_time="11:00",
        )
    )
    db_session.add(
        RestaurantSubmission(
            name="Legacy Brunch Submission",
            status="pending",
            submitter=user.id,
            establishment_type="fixed_menu_restaurant",
            is_campus=True,
        )
    )
    await db_session.commit()

    detail_response = await async_client.get(f"/restaurants/{restaurant.id}")
    list_response = await async_client.get("/restaurants/")

    assert detail_response.status_code == 200
    assert list_response.status_code == 200
    assert "brunch_time" not in detail_response.json()["data"]
    assert all("brunch_time" not in item for item in list_response.json()["data"])
