"""Pydantic schemas for audit log data."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AuditLogSchema(BaseModel):
    """Serializable audit log shape."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    request_id: str
    actor_user_id: str | None
    action: str
    resource_type: str
    resource_id: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    created_at: datetime
