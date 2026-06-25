"""Audit log SQLAlchemy model."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.meals import NonEscapedJSON


class AuditLog(Base):
    """Transactional audit entry for business mutations."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(NonEscapedJSON, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(NonEscapedJSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("audit_log_request_id_index", "request_id"),
        Index("audit_log_actor_user_id_index", "actor_user_id"),
        Index("audit_log_resource_index", "resource_type", "resource_id"),
        Index("audit_log_created_at_index", "created_at"),
    )
