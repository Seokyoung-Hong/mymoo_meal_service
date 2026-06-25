"""Restaurant request approval and rejection route tests."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.restaurants import Restaurant, RestaurantSubmission
from app.models.user import User
from app.utils.request_id import REQUEST_ID_HEADER


RESTAURANT_REQUEST_PAYLOAD = {
    "name": "Requested Restaurant",
    "establishment_type": "fixed_menu_restaurant",
    "price": 9000,
    "location": {
        "is_campus": True,
        "building": "Student Center",
        "map_links": {
            "naver": "https://map.naver.com/requested",
            "kakao": "https://map.kakao.com/requested",
        },
        "latitude": 37.1,
        "longitude": 127.1,
    },
    "opening_time": {"start": "09:00", "end": "18:00"},
}


async def seed_default_user(db_session: AsyncSession) -> User:
    """Persist the default authenticated test user."""
    user = User(id=1, user_id="test-user-sub")
    db_session.add(user)
    await db_session.commit()
    return user


async def create_restaurant_request(async_client: AsyncClient) -> int:
    """Create a restaurant request through the public request API."""
    response = await async_client.post(
        "/restaurants/requests",
        json=RESTAURANT_REQUEST_PAYLOAD,
    )

    assert response.status_code == 201
    return int(response.json()["data"]["request_id"])


async def test_admin_approves_restaurant_request_on_new_path(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await seed_default_user(db_session)
    request_id = await create_restaurant_request(async_client)
    incoming_request_id = "restaurant-request-approval-id"

    response = await async_client.post(
        f"/restaurants/requests/{request_id}/approval",
        headers={REQUEST_ID_HEADER: incoming_request_id},
    )

    assert response.status_code == 200
    restaurant_id = response.json()["data"]["restaurant_id"]
    restaurant = await db_session.get(Restaurant, restaurant_id)
    submission = await db_session.get(RestaurantSubmission, request_id)
    audit_log = await db_session.scalar(
        select(AuditLog).where(AuditLog.request_id == incoming_request_id)
    )

    assert restaurant is not None
    assert restaurant.name == RESTAURANT_REQUEST_PAYLOAD["name"]
    assert restaurant.is_active is True
    assert submission is not None
    assert submission.status == "approved"
    assert audit_log is not None
    assert audit_log.actor_user_id == "test-user-sub"
    assert audit_log.action == "restaurant.request.approve"
    assert audit_log.resource_type == "restaurant_request"
    assert audit_log.resource_id == str(request_id)
    assert audit_log.before == {"status": "pending"}
    assert audit_log.after == {
        "status": "approved",
        "restaurant_id": restaurant_id,
    }


async def test_admin_rejects_restaurant_request_on_new_path(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await seed_default_user(db_session)
    request_id = await create_restaurant_request(async_client)
    incoming_request_id = "restaurant-request-rejection-id"

    response = await async_client.post(
        f"/restaurants/requests/{request_id}/rejection",
        json={"message": "Missing required permit."},
        headers={REQUEST_ID_HEADER: incoming_request_id},
    )

    submission = await db_session.get(RestaurantSubmission, request_id)
    audit_log = await db_session.scalar(
        select(AuditLog).where(AuditLog.request_id == incoming_request_id)
    )

    assert response.status_code == 204
    assert submission is not None
    assert submission.status == "rejected"
    assert submission.rejection_message == "Missing required permit."
    assert audit_log is not None
    assert audit_log.actor_user_id == "test-user-sub"
    assert audit_log.action == "restaurant.request.reject"
    assert audit_log.resource_type == "restaurant_request"
    assert audit_log.resource_id == str(request_id)
    assert audit_log.before == {"status": "pending"}
    assert audit_log.after == {
        "status": "rejected",
        "rejection_message": "Missing required permit.",
    }


async def test_old_doubled_restaurant_request_paths_return_not_found(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await seed_default_user(db_session)
    request_id = await create_restaurant_request(async_client)

    approval_response = await async_client.post(
        f"/restaurants/restaurants/{request_id}/approval"
    )
    rejection_response = await async_client.post(
        f"/restaurants/restaurants/{request_id}/rejection",
        json={"message": "Rejected."},
    )

    assert approval_response.status_code == 404
    assert rejection_response.status_code == 404
