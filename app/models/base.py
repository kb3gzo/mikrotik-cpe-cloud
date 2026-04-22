"""Declarative base + common column helpers."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from sqlalchemy import BigInteger, DateTime, func
from sqlalchemy.orm import DeclarativeBase, mapped_column


# Type aliases for the repetitive column patterns — keeps the model files terse.
BigIntPK = Annotated[
    int,
    mapped_column(BigInteger, primary_key=True, autoincrement=True),
]

TimestampTZ = Annotated[
    datetime,
    mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False),
]

TimestampTZNullable = Annotated[
    datetime | None,
    mapped_column(DateTime(timezone=True), nullable=True),
]


class Base(DeclarativeBase):
    """Single declarative base — all ORM models inherit from this."""
