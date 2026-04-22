"""`routers` — primary inventory table."""
from __future__ import annotations

from datetime import datetime
from ipaddress import IPv4Address, IPv6Address

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, MACADDR
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPK


class Router(Base):
    """One row per physical Mikrotik router.

    `identity` is the human-facing primary label and follows the Bradford
    naming convention `"{model} - {LastName}, {FirstName}"`. The convention
    itself is validated in the app layer so we can relax it without a schema
    migration.
    """

    __tablename__ = "routers"

    id: Mapped[BigIntPK]

    identity: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    serial_number: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    mac_address: Mapped[str] = mapped_column(MACADDR, unique=True, nullable=False)

    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    ros_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    ros_major: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    wifi_stack: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
    )

    # UISP join keys (populated in Phase 2)
    uisp_client_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    uisp_service_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    uisp_site_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    linked_airmax_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Overlay
    wg_public_key: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    wg_overlay_ip: Mapped[IPv4Address | IPv6Address | None] = mapped_column(
        INET, unique=True, nullable=True
    )

    # Lifecycle
    enrolled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'active'"),
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "wifi_stack IN ('wireless', 'wifi')",
            name="ck_routers_wifi_stack",
        ),
        CheckConstraint(
            "status IN ('active', 'pending', 'decommissioned', 'quarantined')",
            name="ck_routers_status",
        ),
        Index(
            "idx_routers_uisp_client",
            "uisp_client_id",
            postgresql_where=text("uisp_client_id IS NOT NULL"),
        ),
        Index("idx_routers_last_seen", "last_seen_at"),
        Index("idx_routers_status", "status"),
        Index(
            "idx_routers_identity_trgm",
            "identity",
            postgresql_using="gin",
            postgresql_ops={"identity": "gin_trgm_ops"},
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Router id={self.id} identity={self.identity!r} status={self.status}>"
