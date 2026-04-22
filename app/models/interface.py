"""`router_interfaces` — snapshot state, last-write-wins."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPK


class RouterInterface(Base):
    """Per-interface snapshot. History lives in InfluxDB."""

    __tablename__ = "router_interfaces"

    id: Mapped[BigIntPK]
    router_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("routers.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # ethernet | wireless | wifi

    ssid: Mapped[str | None] = mapped_column(Text, nullable=True)
    band: Mapped[str | None] = mapped_column(Text, nullable=True)
    frequency_mhz: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channel_width: Mapped[str | None] = mapped_column(Text, nullable=True)
    tx_power_dbm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mode: Mapped[str | None] = mapped_column(Text, nullable=True)
    disabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("router_id", "name", name="uq_router_interfaces_router_name"),
    )
