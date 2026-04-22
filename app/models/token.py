"""Router telemetry tokens + one-shot enrollment tokens."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPK


class RouterToken(Base):
    """Bearer token a router uses to POST telemetry. Hashed at rest."""

    __tablename__ = "router_tokens"

    id: Mapped[BigIntPK]
    router_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("routers.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # First 8 chars of the raw token — used for fast lookup before the hash
    # comparison. Not a secret; having it in the clear is fine.
    token_prefix: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index(
            "idx_router_tokens_prefix",
            "token_prefix",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class EnrollmentToken(Base):
    """One-shot token for the manual enrollment flow (§4.6).

    Distinct from `admin_fetch_tokens` (which authorize installer downloads
    in the zero-touch factory-prep path).
    """

    __tablename__ = "enrollment_tokens"

    id: Mapped[BigIntPK]
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    issued_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    used_by_router_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("routers.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
