"""Initial schema — routers, tokens, secrets, interfaces, audit, backups.

Mirrors `01-design-wireguard-and-telemetry.md` §7 plus the self-provisioning
tables from `02-self-provisioning.md` §2.2, §2.3, and §3.4.

Revision ID: 0001
Revises:
Create Date: 2026-04-21
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------ ext
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # -------------------------------------------------------------- routers
    op.create_table(
        "routers",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("identity", sa.Text(), nullable=False),
        sa.Column("serial_number", sa.Text(), nullable=False),
        sa.Column("mac_address", postgresql.MACADDR(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("ros_version", sa.Text(), nullable=True),
        sa.Column("ros_major", sa.SmallInteger(), nullable=True),
        sa.Column("wifi_stack", sa.String(16), nullable=True),
        sa.Column("uisp_client_id", sa.Text(), nullable=True),
        sa.Column("uisp_service_id", sa.Text(), nullable=True),
        sa.Column("uisp_site_id", sa.Text(), nullable=True),
        sa.Column("linked_airmax_id", sa.Text(), nullable=True),
        sa.Column("wg_public_key", sa.Text(), nullable=True),
        sa.Column("wg_overlay_ip", postgresql.INET(), nullable=True),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("identity", name="uq_routers_identity"),
        sa.UniqueConstraint("serial_number", name="uq_routers_serial_number"),
        sa.UniqueConstraint("mac_address", name="uq_routers_mac_address"),
        sa.UniqueConstraint("wg_public_key", name="uq_routers_wg_public_key"),
        sa.UniqueConstraint("wg_overlay_ip", name="uq_routers_wg_overlay_ip"),
        sa.CheckConstraint(
            "wifi_stack IN ('wireless', 'wifi')",
            name="ck_routers_wifi_stack",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'pending', 'decommissioned', 'quarantined')",
            name="ck_routers_status",
        ),
    )
    op.create_index(
        "idx_routers_uisp_client",
        "routers",
        ["uisp_client_id"],
        postgresql_where=sa.text("uisp_client_id IS NOT NULL"),
    )
    op.create_index("idx_routers_last_seen", "routers", ["last_seen_at"])
    op.create_index("idx_routers_status", "routers", ["status"])
    op.create_index(
        "idx_routers_identity_trgm",
        "routers",
        ["identity"],
        postgresql_using="gin",
        postgresql_ops={"identity": "gin_trgm_ops"},
    )

    # --------------------------------------------------------- router_tokens
    op.create_table(
        "router_tokens",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "router_id",
            sa.BigInteger(),
            sa.ForeignKey("routers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("token_prefix", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_router_tokens_prefix",
        "router_tokens",
        ["token_prefix"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # ----------------------------------------------------- enrollment_tokens
    op.create_table(
        "enrollment_tokens",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("issued_by", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "used_by_router_id",
            sa.BigInteger(),
            sa.ForeignKey("routers.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("token_hash", name="uq_enrollment_tokens_token_hash"),
    )

    # ----------------------------------------------------- router_interfaces
    op.create_table(
        "router_interfaces",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "router_id",
            sa.BigInteger(),
            sa.ForeignKey("routers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ssid", sa.Text(), nullable=True),
        sa.Column("band", sa.Text(), nullable=True),
        sa.Column("frequency_mhz", sa.Integer(), nullable=True),
        sa.Column("channel_width", sa.Text(), nullable=True),
        sa.Column("tx_power_dbm", sa.Integer(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=True),
        sa.Column("disabled", sa.Boolean(), nullable=True),
        sa.Column(
            "last_updated",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("router_id", "name", name="uq_router_interfaces_router_name"),
    )

    # ------------------------------------------------------------ audit_log
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("router_id", sa.BigInteger(), sa.ForeignKey("routers.id"), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_audit_router", "audit_log", ["router_id"])
    op.create_index(
        "idx_audit_created_at",
        "audit_log",
        [sa.text("created_at DESC")],
    )

    # ------------------------------------------------------- router_backups
    op.create_table(
        "router_backups",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "router_id",
            sa.BigInteger(),
            sa.ForeignKey("routers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column(
            "taken_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # ------------------------------------------------- provisioning_secrets
    op.create_table(
        "provisioning_secrets",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("label", name="uq_provisioning_secrets_label"),
        sa.UniqueConstraint("secret_hash", name="uq_provisioning_secrets_secret_hash"),
        sa.CheckConstraint(
            "status IN ('current', 'previous', 'retired')",
            name="ck_provisioning_secrets_status",
        ),
    )

    # --------------------------------------------------- provisioning_rules
    op.create_table(
        "provisioning_rules",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column("effect", sa.Text(), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("priority", name="uq_provisioning_rules_priority"),
        sa.CheckConstraint(
            "kind IN ('identity_regex', 'serial_list')",
            name="ck_provisioning_rules_kind",
        ),
        sa.CheckConstraint(
            "effect IN ('active', 'pending', 'quarantined')",
            name="ck_provisioning_rules_effect",
        ),
    )

    # Seed: the identity-pattern rule from 02-self-provisioning.md §2.3.
    op.execute(
        """
        INSERT INTO provisioning_rules (priority, kind, pattern, effect, enabled)
        VALUES (
            10,
            'identity_regex',
            '^hAP .+ - .+, .+$',
            'active',
            TRUE
        )
        """
    )

    # --------------------------------------------------- admin_fetch_tokens
    op.create_table(
        "admin_fetch_tokens",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("token_prefix", sa.Text(), nullable=False),
        sa.Column("issued_by", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "use_count",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("token_hash", name="uq_admin_fetch_tokens_token_hash"),
    )
    op.create_index(
        "idx_admin_fetch_tokens_prefix",
        "admin_fetch_tokens",
        ["token_prefix"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_admin_fetch_tokens_prefix", table_name="admin_fetch_tokens")
    op.drop_table("admin_fetch_tokens")
    op.drop_table("provisioning_rules")
    op.drop_table("provisioning_secrets")
    op.drop_table("router_backups")
    op.drop_index("idx_audit_created_at", table_name="audit_log")
    op.drop_index("idx_audit_router", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("router_interfaces")
    op.drop_table("enrollment_tokens")
    op.drop_index("idx_router_tokens_prefix", table_name="router_tokens")
    op.drop_table("router_tokens")
    op.drop_index("idx_routers_identity_trgm", table_name="routers")
    op.drop_index("idx_routers_status", table_name="routers")
    op.drop_index("idx_routers_last_seen", table_name="routers")
    op.drop_index("idx_routers_uisp_client", table_name="routers")
    op.drop_table("routers")
    # pg_trgm is shared infra — don't drop it on downgrade.
