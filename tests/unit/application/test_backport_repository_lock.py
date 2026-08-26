"""目标仓库跨进程锁(flock)与锁事件发布测试。"""
import threading
import time
from pathlib import Path

from witty_service.application.backport_cvekit_client import BackportCvekitClient


def _make_client(tmp_path: Path, events: list[dict]) -> BackportCvekitClient:
    def callback(event: dict) -> None:
        events.append(event)

    return BackportCvekitClient(runs_root=tmp_path / "runs", progress_callback=callback)


def test_repository_lock_mutual_exclusion_and_events(monkeypatch, tmp_path: Path) -> None:
    """同一 target_path 的两个 client 互斥:竞争方发布 waiting,持有方释放后 acquired。"""
    monkeypatch.setenv("HOME", str(tmp_path))  # 锁文件落在 tmp/.patchflow/locks
    events: list[dict] = []
    c1 = _make_client(tmp_path, events)
    c2 = _make_client(tmp_path, events)
    c1.set_lock_target("/repo/kernel")
    c2.set_lock_target("/repo/kernel")

    held = threading.Event()
    release = threading.Event()
    holder_error: list[Exception] = []

    def holder() -> None:
        try:
            with c1.repository_lock():
                held.set()
                release.wait(5)
        except Exception as exc:
            holder_error.append(exc)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(2), "持有方未获得锁"

    acquired = threading.Event()

    def contender() -> None:
        with c2.repository_lock():
            acquired.set()

    t2 = threading.Thread(target=contender)
    t2.start()
    time.sleep(0.8)
    release.set()
    t.join(3)
    t2.join(3)

    assert not holder_error, holder_error
    assert acquired.is_set(), "竞争方未获得锁"
    assert not t.is_alive() and not t2.is_alive()
    names = [e.get("event") for e in events]
    assert "repository_lock_waiting" in names, names
    assert "repository_lock_acquired" in names, names


def test_repository_lock_uncontended_acquires_immediately(monkeypatch, tmp_path: Path) -> None:
    """无竞争时直接获得锁并发布 acquired。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    events: list[dict] = []
    client = _make_client(tmp_path, events)
    client.set_lock_target("/repo/kernel")
    with client.repository_lock():
        pass
    names = [e.get("event") for e in events]
    assert "repository_lock_acquired" in names, names
    assert "repository_lock_waiting" not in names, names


def test_repository_lock_no_target_skips(monkeypatch, tmp_path: Path) -> None:
    """未设置 lock_target 时不加锁、不发事件。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    events: list[dict] = []
    client = _make_client(tmp_path, events)
    with client.repository_lock():
        pass
    assert events == []


def test_repository_lock_reentrant_held_across_run(monkeypatch, tmp_path: Path) -> None:
    """锁可重入:service 层全程持有(run_all 多阶段),嵌套进入不重复加锁、不重复发事件。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    events: list[dict] = []
    client = _make_client(tmp_path, events)
    client.set_lock_target("/repo/kernel")
    with client.repository_lock():  # service 层持有
        with client.repository_lock():  # _run_cvekit 嵌套进入
            pass
        # 多阶段之间锁仍持有:再嵌套一次
        with client.repository_lock():
            pass
    names = [e.get("event") for e in events]
    # 只加锁一次、只发一次 acquired(无竞争),不因嵌套重复
    assert names.count("repository_lock_acquired") == 1, names
    assert client._lock_held == 0


def test_repository_lock_release_clears_owner_while_held(monkeypatch, tmp_path: Path) -> None:
    """释放时先清空 owner 再解锁:锁文件在释放后为空,竞争者写入的新 owner 不被清空。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    client = _make_client(tmp_path, [])
    client.set_lock_target("/repo/kernel")
    lock_path = tmp_path / ".patchflow" / "locks" / (
        __import__("hashlib").sha1(b"/repo/kernel").hexdigest()[:16] + ".lock"
    )
    with client.repository_lock():
        assert lock_path.read_text(encoding="utf-8").strip() != "{}", "持锁期间应有 owner"
    assert lock_path.read_text(encoding="utf-8").strip() == "{}", "释放后 owner 应为空"
