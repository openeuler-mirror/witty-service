from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase


def utc_ms_now() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        }
    )
