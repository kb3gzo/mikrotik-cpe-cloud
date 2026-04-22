"""`audit_log` — actions taken on or by a router."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPK


class AuditLog(Base):
    """Append-only log of control-plane actions.

    Phase 1 doesn't write much here, but the table exists from day one so we
    don't need a schema migration later.
    """

    __tablename__ = "audit_log"

    id: Mapped[BigIntPK]
    router_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("routers.id"), nullable=True
    )
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)  # pending | success | failed
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("idx_audit_router", "router_id"),
        Index("idx_audit_created_at", "created_at", postgresql_ops={"created_at": "DESC"}),
    )
