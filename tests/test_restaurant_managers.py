"""Restaurant manager API tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.associations import restaurant_manager_association
from app.models.restaurants import Restaurant
from app.models.user import User
from app.utils import db as db_utils
from app.utils.request_id import REQUEST_ID_HEADER


async def seed_restaurant_with_owner(db_session: AsyncSession) -> Restaurant:
    """Seed a restaurant owned by the default authenticated test user."""
    owner = User(id=1, user_id="test-user-sub")
    restaurant = Restaurant(
        name="Managed Restaurant",
        owner_user=owner,
        is_campus=True,
        is_active=True,
        establishment_type="fixed_menu_restaurant",
    )
    db_session.add_all([owner, restaurant])
    await db_session.commit()
    return restaurant


async def seed_user_projection(db_session: AsyncSession, user_id: str) -> User:
    """Persist a local user projection for owner/manager assignment tests."""
    user = User(user_id=user_id)
    db_session.add(user)
    await db_session.commit()
    return user


async def add_manager_association(
    db_session: AsyncSession,
    restaurant: Restaurant,
    manager: User,
) -> None:
    """Persist a manager link without triggering async lazy loading in tests."""
    db_session.add(manager)
    await db_session.flush()
    await db_session.execute(
        restaurant_manager_association.insert().values(
            restaurant_id=restaurant.id,
            user_id=manager.id,
        )
    )
    await db_session.commit()


async def override_current_user(test_app: FastAPI, current_user: User) -> None:
    """Override current-user dependency for a single test scenario."""

    async def override() -> AsyncGenerator[User, None]:
        yield current_user

    test_app.dependency_overrides[db_utils.get_current_user] = override


async def test_owner_adds_and_lists_manager_by_user_id(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    restaurant = await seed_restaurant_with_owner(db_session)
    await seed_user_projection(db_session, "manager-sub")

    add_response = await async_client.post(
        f"/restaurants/{restaurant.id}/managers",
        json={"user_id": "manager-sub"},
    )
    list_response = await async_client.get(f"/restaurants/{restaurant.id}/managers")

    assert add_response.status_code == 200
    assert add_response.json()["data"] == {"user_id": "manager-sub"}
    assert list_response.status_code == 200
    assert list_response.json()["data"] == [{"user_id": "manager-sub"}]


async def test_owner_removes_manager_by_user_id(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    restaurant = await seed_restaurant_with_owner(db_session)
    manager = User(user_id="remove-manager-sub")
    await add_manager_association(db_session, restaurant, manager)

    response = await async_client.delete(
        f"/restaurants/{restaurant.id}/managers/{manager.user_id}"
    )
    list_response = await async_client.get(f"/restaurants/{restaurant.id}/managers")

    assert response.status_code == 204
    assert list_response.status_code == 200
    assert list_response.json()["data"] == []


async def test_non_owner_manager_cannot_grant_manager_access(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
) -> None:
    restaurant = await seed_restaurant_with_owner(db_session)
    existing_manager = User(user_id="existing-manager-sub")
    await add_manager_association(db_session, restaurant, existing_manager)
    await override_current_user(test_app, existing_manager)

    response = await async_client.post(
        f"/restaurants/{restaurant.id}/managers",
        json={"user_id": "new-manager-sub"},
    )

    assert response.status_code == 403
    manager_count = await db_session.scalar(select(func.count()).select_from(User))
    assert manager_count == 2


async def test_duplicate_manager_add_is_idempotent_without_extra_audit(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    restaurant = await seed_restaurant_with_owner(db_session)
    await seed_user_projection(db_session, "duplicate-manager-sub")
    request_id = "manager-duplicate-request-id"

    first_response = await async_client.post(
        f"/restaurants/{restaurant.id}/managers",
        json={"user_id": "duplicate-manager-sub"},
        headers={REQUEST_ID_HEADER: request_id},
    )
    second_response = await async_client.post(
        f"/restaurants/{restaurant.id}/managers",
        json={"user_id": "duplicate-manager-sub"},
        headers={REQUEST_ID_HEADER: request_id},
    )

    audit_count = await db_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.request_id == request_id)
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json()["data"] == {"user_id": "duplicate-manager-sub"}
    assert audit_count == 1


async def test_manager_add_and_remove_create_audit_logs_with_request_id(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    restaurant = await seed_restaurant_with_owner(db_session)
    await seed_user_projection(db_session, "audited-manager-sub")
    add_request_id = "manager-add-request-id"
    remove_request_id = "manager-remove-request-id"

    add_response = await async_client.post(
        f"/restaurants/{restaurant.id}/managers",
        json={"user_id": "audited-manager-sub"},
        headers={REQUEST_ID_HEADER: add_request_id},
    )
    remove_response = await async_client.delete(
        f"/restaurants/{restaurant.id}/managers/audited-manager-sub",
        headers={REQUEST_ID_HEADER: remove_request_id},
    )

    add_audit = await db_session.scalar(
        select(AuditLog).where(AuditLog.request_id == add_request_id)
    )
    remove_audit = await db_session.scalar(
        select(AuditLog).where(AuditLog.request_id == remove_request_id)
    )

    assert add_response.status_code == 200
    assert remove_response.status_code == 204
    assert add_audit is not None
    assert add_audit.actor_user_id == "test-user-sub"
    assert add_audit.action == "restaurant.manager.add"
    assert add_audit.resource_type == "restaurant"
    assert add_audit.resource_id == str(restaurant.id)
    assert add_audit.before == {"managers": []}
    assert add_audit.after == {"managers": ["audited-manager-sub"]}
    assert remove_audit is not None
    assert remove_audit.actor_user_id == "test-user-sub"
    assert remove_audit.action == "restaurant.manager.remove"
    assert remove_audit.resource_type == "restaurant"
    assert remove_audit.resource_id == str(restaurant.id)
    assert remove_audit.before == {"managers": ["audited-manager-sub"]}
    assert remove_audit.after == {"managers": []}


async def test_admin_can_add_list_and_remove_manager(
    async_client: AsyncClient,
    db_session: AsyncSession,
    test_app: FastAPI,
) -> None:
    restaurant = await seed_restaurant_with_owner(db_session)
    admin = User(user_id="admin-sub")
    manager = User(user_id="admin-added-manager-sub")
    admin.auth_meal_admin = True
    db_session.add_all([admin, manager])
    await db_session.commit()
    await override_current_user(test_app, admin)

    add_response = await async_client.post(
        f"/restaurants/{restaurant.id}/managers",
        json={"user_id": "admin-added-manager-sub"},
    )
    list_response = await async_client.get(f"/restaurants/{restaurant.id}/managers")
    remove_response = await async_client.delete(
        f"/restaurants/{restaurant.id}/managers/admin-added-manager-sub"
    )

    assert add_response.status_code == 200
    assert list_response.status_code == 200
    assert list_response.json()["data"] == [{"user_id": "admin-added-manager-sub"}]
    assert remove_response.status_code == 204
