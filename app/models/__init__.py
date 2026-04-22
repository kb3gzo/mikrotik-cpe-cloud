"""SQLAlchemy ORM models.

Every ORM class MUST be imported here so `Base.metadata` is populated before
Alembic's autogenerate or `create_all` runs. Alembic's env.py targets this
Base directly.
"""
from __future__ import annotations

from app.models.base import Base
from app.models.audit import AuditLog
from app.models.backup import RouterBackup
from app.models.interface import RouterInterface
from app.models.router import Router
from app.models.secret import (
    AdminFetchToken,
    ProvisioningRule,
    ProvisioningSecret,
)
from app.models.token import EnrollmentToken, RouterToken

__all__ = [
    "AdminFetchToken",
    "AuditLog",
    "Base",
    "EnrollmentToken",
    "ProvisioningRule",
    "ProvisioningSecret",
    "Router",
    "RouterBackup",
    "RouterInterface",
    "RouterToken",
]
