from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields
from enum import StrEnum
from functools import cache
from typing import Any, TypeVar, cast, get_args, get_type_hints
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

RecordT = TypeVar("RecordT")


def new_id() -> str:
    return str(uuid4())


@cache
def _enum_fields(record_type: type[object]) -> dict[str, type[StrEnum]]:
    enums: dict[str, type[StrEnum]] = {}
    for name, hint in get_type_hints(record_type).items():
        for candidate in (hint, *get_args(hint)):
            if isinstance(candidate, type) and issubclass(candidate, StrEnum):
                enums[name] = candidate
                break
    return enums


def record_of(record_type: type[RecordT], row: object) -> RecordT:
    enums = _enum_fields(cast(type[object], record_type))
    values: dict[str, object] = {}
    for field in fields(cast(Any, record_type)):
        value = getattr(row, field.name)
        enum_type = enums.get(field.name)
        values[field.name] = (
            enum_type(value) if enum_type and value is not None else value
        )
    return record_type(**values)


class RepositoryBase:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        with self._session_factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    @contextmanager
    def _read(self) -> Iterator[Session]:
        with self._session_factory() as session:
            yield session
