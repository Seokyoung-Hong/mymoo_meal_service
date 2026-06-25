"""Request ID middleware and audit log tests."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.services.audit import AuditLogEntry, add_audit_log
from app.utils.request_id import REQUEST_ID_HEADER


async def test_generated_request_id_appears_in_response(
    async_client: AsyncClient,
) -> None:
    response = await async_client.get("/health")

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER]


async def test_incoming_request_id_is_propagated(
    async_client: AsyncClient,
) -> None:
    request_id = "incoming-request-id-123"

    response = await async_client.get(
        "/health",
        headers={REQUEST_ID_HEADER: request_id},
    )

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == request_id


async def test_audit_helper_writes_transactional_row_with_actor(
    db_session: AsyncSession,
) -> None:
    audit_log = add_audit_log(
        db_session,
        AuditLogEntry(
            request_id="audit-request-id",
            actor_user_id="jwt-sub-user",
            action="meal.create",
            resource_type="meal",
            resource_id=42,
            before=None,
            after={"main_menu": "김치찌개", "Authorization": "Bearer secret-token"},
        ),
    )

    await db_session.commit()

    saved = await db_session.scalar(select(AuditLog).where(AuditLog.id == audit_log.id))
    assert saved is not None
    assert saved.request_id == "audit-request-id"
    assert saved.actor_user_id == "jwt-sub-user"
    assert saved.action == "meal.create"
    assert saved.resource_type == "meal"
    assert saved.resource_id == "42"
    assert saved.before is None
    assert saved.after == {"main_menu": "김치찌개", "Authorization": "[REDACTED]"}


async def test_audit_helper_rolls_back_with_transaction(
    db_session: AsyncSession,
) -> None:
    add_audit_log(
        db_session,
        AuditLogEntry(
            request_id="rolled-back-request-id",
            actor_user_id="jwt-sub-user",
            action="meal.update",
            resource_type="meal",
            resource_id="meal-1",
            after={"main_menu": "rollback"},
        ),
    )
    await db_session.flush()
    await db_session.rollback()

    count = await db_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.request_id == "rolled-back-request-id")
    )

    assert count == 0
