from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from witty_service.persistence.db import (
    create_session_factory,
    create_sqlite_engine,
    init_db,
)
from witty_service.persistence.orm import ModelORM
from witty_service.persistence.repositories import SqliteRepository


@pytest.fixture()
def repo() -> SqliteRepository:
    engine = create_sqlite_engine("sqlite:///:memory:")
    init_db(engine, auto_create=True)
    factory = create_session_factory(engine)
    try:
        yield SqliteRepository(factory)
    finally:
        engine.dispose()


def _new_file_repo(url: str) -> tuple[SqliteRepository, object]:
    """基于文件的仓库(跨线程/跨会话共享同一 DB),返回 (repo, engine)。"""
    engine = create_sqlite_engine(url)
    init_db(engine, auto_create=True)
    factory = create_session_factory(engine)
    return SqliteRepository(factory), engine


def test_default_model_is_mutually_exclusive(repo: SqliteRepository) -> None:
    """B1: 把某个模型置为默认,会清掉之前默认(单默认约束)。"""
    repo.create_model_with_id(
        model_id="model-a",
        name="A",
        provider="openai",
        api_key="k",
        is_default=True,
    )
    repo.create_model_with_id(
        model_id="model-b",
        name="B",
        provider="openai",
        api_key="k",
        is_default=False,
    )
    # 新模型置为默认,应清掉 model-a
    repo.create_model_with_id(
        model_id="model-c",
        name="C",
        provider="openai",
        api_key="k",
        is_default=True,
    )
    assert [m.id for m in repo.list_models() if m.is_default] == ["model-c"]

    # 通过 update 切换默认,仍只保留一个
    repo.update_model("model-b", is_default=True)
    assert [m.id for m in repo.list_models() if m.is_default] == ["model-b"]

    # 改回另一个模型,仍只保留一个
    repo.update_model("model-a", is_default=True)
    assert [m.id for m in repo.list_models() if m.is_default] == ["model-a"]


def test_clearing_default_does_not_bump_cleared_model_updated_at(
    repo: SqliteRepository,
) -> None:
    """清默认不应改动被清模型的 updated_at(避免隐藏副作用)。"""
    first = repo.create_model_with_id(
        model_id="model-a",
        name="A",
        provider="openai",
        api_key="k",
        is_default=True,
    )
    time.sleep(0.05)  # 让时钟前进,若被清行 updated_at 被刷新就能观测到
    repo.create_model_with_id(
        model_id="model-b",
        name="B",
        provider="openai",
        api_key="k",
        is_default=True,
    )

    after = repo.get_model("model-a")
    assert after is not None
    assert after.is_default is False
    assert after.updated_at == first.updated_at
    assert [m.id for m in repo.list_models() if m.is_default] == ["model-b"]


def test_db_enforces_single_default_via_partial_unique_index(
    tmp_path: Path,
) -> None:
    """DB 层部分唯一索引直接拒绝第二个 is_default=1(绕过应用层清默认逻辑)。"""
    url = f"sqlite:///{tmp_path / 'index.db'}"
    engine = create_sqlite_engine(url)
    init_db(engine, auto_create=True)
    factory = create_session_factory(engine)
    try:
        with factory() as session:
            session.add(
                ModelORM(
                    id="model-a",
                    name="A",
                    provider="openai",
                    api_key="k",
                    is_default=True,
                )
            )
            session.commit()
            with pytest.raises(IntegrityError):
                session.add(
                    ModelORM(
                        id="model-b",
                        name="B",
                        provider="openai",
                        api_key="k",
                        is_default=True,
                    )
                )
                session.commit()
    finally:
        engine.dispose()


def test_concurrent_default_model_assignment_keeps_single_default(
    tmp_path: Path,
) -> None:
    """并发"置默认"仍只留下一个 is_default=1(由唯一索引兜底)。"""
    url = f"sqlite:///{tmp_path / 'concurrent.db'}"
    repo, engine = _new_file_repo(url)
    try:
        repo.create_model_with_id(
            model_id="model-a",
            name="A",
            provider="openai",
            api_key="k",
            is_default=False,
        )
        repo.create_model_with_id(
            model_id="model-b",
            name="B",
            provider="openai",
            api_key="k",
            is_default=False,
        )

        barrier = threading.Barrier(2)

        def set_default(model_id: str) -> None:
            barrier.wait(timeout=10)
            repo.update_model(model_id, is_default=True)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(set_default, model_id)
                for model_id in ("model-a", "model-b")
            ]
            for future in futures:
                future.result(timeout=30)

        defaults = [m.id for m in repo.list_models() if m.is_default]
        assert len(defaults) == 1
    finally:
        engine.dispose()
