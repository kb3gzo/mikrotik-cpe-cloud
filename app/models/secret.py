"""Zero-touch provisioning secrets, rules, and admin fetch tokens.

Defined in `02-self-provisioning.md` §2.2, §2.3, and §3.4 respectively.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPK


class ProvisioningSecret(Base):
    """Shared secret embedded into the factory-prep installer.

    Postgres stores only hashes. Plaintext lives in a separate secret store
    (systemd LoadCredential, Vault, or similar) — see §3.2's
    `_current_provisioning_secret_plaintext` integration point.

    The server accepts up to two active secrets at any time (`current` and
    `previous`) so in-flight shelf stock survives quarterly rotations.
    """

    __tablename__ = "provisioning_secrets"

    id: Mapped[BigIntPK]
    label: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    secret_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)  # current | previous | retired
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('current', 'previous', 'retired')",
            name="ck_provisioning_secrets_status",
        ),
    )


class ProvisioningRule(Base):
    """Auto-approve rules evaluated on `/auto-enroll` (§2.3)."""

    __tablename__ = "provisioning_rules"

    id: Mapped[BigIntPK]
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # identity_regex | serial_list
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    effect: Mapped[str] = mapped_column(Text, nullable=False)  # active | pending | quarantined
    enabled: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("TRUE")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('identity_regex', 'serial_list')",
            name="ck_provisioning_rules_kind",
        ),
        CheckConstraint(
            "effect IN ('active', 'pending', 'quarantined')",
            name="ck_provisioning_rules_effect",
        ),
        UniqueConstraint("priority", name="uq_provisioning_rules_priority"),
    )


class AdminFetchToken(Base):
    """Short-lived token that authorizes `/factory/self-enroll.rsc` downloads.

    Per `02-self-provisioning.md` §3.4. A token is minted per admin session,
    pasted into the factory-prep script, and revoked/expires when the prep
    run is finished. The server records `use_count` + `last_used_at` for
    audit.
    """

    __tablename__ = "admin_fetch_tokens"

    id: Mapped[BigIntPK]
    label: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. "Aaron prep 2026-04-21"
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    token_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    issued_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    use_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "idx_admin_fetch_tokens_prefix",
            "token_prefix",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
