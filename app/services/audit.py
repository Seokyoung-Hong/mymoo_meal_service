"""Transactional audit logging helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.utils.request_id import REQUEST_ID_HEADER


SENSITIVE_KEY_PARTS = (
    "authorization",
    "client_secret",
    "password",
    "secret",
    "service_account_token",
    "token",
)
REDACTED = "[REDACTED]"


@dataclass(frozen=True)
class AuditLogEntry:
    """Input data for a transactional audit log row."""

    request_id: str
    actor_user_id: str | None
    action: str
    resource_type: str
    resource_id: str | int | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


def request_id_from_request(request: Request) -> str:
    """Return the middleware request ID, falling back to the incoming header."""
    request_id = getattr(request.state, "request_id", None)
    if isinstance(request_id, str) and request_id:
        return request_id
    return request.headers.get(REQUEST_ID_HEADER, "")


def sanitize_audit_payload(value: Any) -> Any:
    """Redact secret-like keys from nested audit payloads."""
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.lower().replace("-", "_")
            if any(part in normalized for part in SENSITIVE_KEY_PARTS):
                sanitized[key_text] = REDACTED
            else:
                sanitized[key_text] = sanitize_audit_payload(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_audit_payload(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_audit_payload(item) for item in value]
    return value


def add_audit_log(
    db: AsyncSession,
    entry: AuditLogEntry,
) -> AuditLog:
    """Add an audit row to the active transaction without committing it."""
    audit_log = AuditLog(
        request_id=entry.request_id,
        actor_user_id=entry.actor_user_id,
        action=entry.action,
        resource_type=entry.resource_type,
        resource_id=None if entry.resource_id is None else str(entry.resource_id),
        before=sanitize_audit_payload(entry.before),
        after=sanitize_audit_payload(entry.after),
    )
    db.add(audit_log)
    return audit_log
