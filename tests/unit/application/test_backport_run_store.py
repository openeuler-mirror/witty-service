import re
import threading
from pathlib import Path

from witty_service.application.backport_run_store import BackportRunStore


def test_create_task_id_ends_with_8_hex_digits(tmp_path: Path) -> None:
    excel = tmp_path / "backport.xlsx"
    excel.write_text("fixture", encoding="utf-8")
    store = BackportRunStore(tmp_path / "runs")

    task_dir = store.create_task(
        excel_path=excel,
        target_repository="https://github.com/example/kernel.git",
    )

    # 新命名规范:task ID 随机后缀为 8 位 hex,供 patchflow 文件名 run_id8 取尾 8 位
    assert re.fullmatch(r".+-[0-9a-f]{8}", task_dir.name)
    assert len(task_dir.name) <= 96


def test_create_task_oversized_names_bounded_within_96(tmp_path: Path) -> None:
    excel = tmp_path / (f"{'e' * 100}.xlsx")
    excel.write_text("fixture", encoding="utf-8")
    store = BackportRunStore(tmp_path / "runs")

    task_dir = store.create_task(
        excel_path=excel,
        target_repository=f"https://github.com/example/{'r' * 100}.git",
    )

    assert len(task_dir.name) <= 96
    assert re.fullmatch(r".+-[0-9a-f]{8}", task_dir.name)
    # 超长输入下生成的 ID 仍可通过 _task_dir 定位,set_run_id 不截断
    assert store._task_dir(task_dir.name) == task_dir
    store.set_run_id(task_dir.name)
    assert store._run_id == task_dir.name


# ── task 级日志分层:cvekit_run_id 与 witty/ 聚合 ────────────────────────────


def _new_task(store: BackportRunStore, tmp_path: Path) -> Path:
    excel = tmp_path / "backport.xlsx"
    excel.write_text("fixture", encoding="utf-8")
    return store.create_task(
        excel_path=excel,
        target_repository="https://github.com/example/kernel.git",
    )


def _run_all_log(tmp_path: Path, ctx) -> Path:
    return (
        tmp_path
        / ".patchflow"
        / "logs"
        / "batch"
        / f"patchflow-{ctx.cvekit_run_id[:14]}-{ctx.cvekit_run_id[-8:]}-all.log"
    )


def _task_all_log(tmp_path: Path, task_id: str) -> Path:
    return (
        tmp_path
        / ".patchflow"
        / "logs"
        / "witty"
        / task_id
        / f"patchflow-{task_id[:8]}-{task_id[-8:]}-task-all.log"
    )


def test_begin_cvekit_run_assigns_unique_run_ids_and_sequence(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    store = BackportRunStore(tmp_path / "runs")
    task_dir = _new_task(store, tmp_path)
    task_id = task_dir.name

    ctx1 = store.begin_cvekit_run(task_id, "generate_config")
    ctx2 = store.begin_cvekit_run(task_id, "apply_row")

    # 同 task 多次调用获得不同 run id,格式与 patchflow 自生成一致
    assert ctx1.cvekit_run_id != ctx2.cvekit_run_id
    for ctx in (ctx1, ctx2):
        assert re.fullmatch(r"\d{14}-[0-9a-f]{8}", ctx.cvekit_run_id)
    assert (ctx1.sequence, ctx2.sequence) == (1, 2)
    # task metadata 建立 task → 多 run 映射
    runs = store.read_manifest(task_dir)["logging"]["cvekit_runs"]
    assert [r["sequence"] for r in runs] == [1, 2]
    assert [r["cvekit_run_id"] for r in runs] == [
        ctx1.cvekit_run_id,
        ctx2.cvekit_run_id,
    ]
    assert [r["status"] for r in runs] == ["running", "running"]
    assert store.read_manifest(task_dir)["logging"]["task_all_log"].startswith("witty/")


def test_complete_cvekit_run_merges_entry_logs_in_sequence(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    store = BackportRunStore(tmp_path / "runs")
    task_dir = _new_task(store, tmp_path)
    task_id = task_dir.name
    ctxs = [
        store.begin_cvekit_run(task_id, op) for op in ("generate_config", "apply_row")
    ]

    source_logs = []
    for n, ctx in enumerate(ctxs, start=1):
        log = _run_all_log(tmp_path, ctx)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"run-{n} line api_key=secret-value\n", encoding="utf-8")
        source_logs.append(log)
        store.complete_cvekit_run(ctx, [log], "success")

    # task 目录只有一份 -task-all.log,chunk 顺序与 sequence 一致
    task_all = _task_all_log(tmp_path, task_id)
    assert task_all.is_file()
    content = task_all.read_text(encoding="utf-8")
    assert content.index("run-1") < content.index("run-2")
    for ctx in ctxs:
        assert f"task run {ctx.sequence} | {ctx.cvekit_run_id} |" in content
    # 聚合副本仍脱敏,源日志保持完整未被改写
    assert "secret-value" not in content
    assert source_logs[0].read_text(encoding="utf-8").startswith("run-1")
    # metadata 标记 merged
    runs = store.read_manifest(task_dir)["logging"]["cvekit_runs"]
    assert [r["status"] for r in runs] == ["success", "success"]
    assert all(r["merged"] is True for r in runs)
    assert all(r["ended_at"] for r in runs)


def test_complete_cvekit_run_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    store = BackportRunStore(tmp_path / "runs")
    task_dir = _new_task(store, tmp_path)
    ctx = store.begin_cvekit_run(task_dir.name, "apply_row")
    log = _run_all_log(tmp_path, ctx)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("only-once\n", encoding="utf-8")

    store.complete_cvekit_run(ctx, [log], "success")
    store.complete_cvekit_run(ctx, [log], "success")

    task_all = _task_all_log(tmp_path, task_dir.name)
    assert task_all.read_text(encoding="utf-8").count("only-once") == 1


def test_complete_cvekit_run_concurrent_chunks_not_lost(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    store = BackportRunStore(tmp_path / "runs")
    task_dir = _new_task(store, tmp_path)
    ctxs = [store.begin_cvekit_run(task_dir.name, f"op-{n}") for n in range(1, 9)]

    def complete(ctx) -> None:
        log = _run_all_log(tmp_path, ctx)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"chunk-{ctx.sequence}\n", encoding="utf-8")
        store.complete_cvekit_run(ctx, [log], "success")

    threads = [threading.Thread(target=complete, args=(ctx,)) for ctx in ctxs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    task_all = _task_all_log(tmp_path, task_dir.name)
    content = task_all.read_text(encoding="utf-8")
    # 8 个 chunk 全部到达且按 sequence 顺序(不按完成顺序),字节不交错
    assert content.count("chunk-") == 8
    positions = [content.index(f"chunk-{n}") for n in range(1, 9)]
    assert positions == sorted(positions)


def test_legacy_task_without_logging_field_still_works(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    store = BackportRunStore(tmp_path / "runs")
    task_dir = _new_task(store, tmp_path)
    manifest = store.read_manifest(task_dir)
    assert "logging" not in manifest

    ctx = store.begin_cvekit_run(task_dir.name, "apply_row")
    log = _run_all_log(tmp_path, ctx)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("legacy-ok\n", encoding="utf-8")
    store.complete_cvekit_run(ctx, [log], "success")

    assert (
        store.read_manifest(task_dir)["logging"]["cvekit_runs"][0]["status"]
        == "success"
    )
    # 旧字段仍完整
    assert store.read_manifest(task_dir)["task_id"] == task_dir.name


def test_complete_failure_keeps_source_logs_and_previous_chunks(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    store = BackportRunStore(tmp_path / "runs")
    task_dir = _new_task(store, tmp_path)
    ctx1 = store.begin_cvekit_run(task_dir.name, "op-1")
    log1 = _run_all_log(tmp_path, ctx1)
    log1.parent.mkdir(parents=True, exist_ok=True)
    log1.write_text("first-chunk\n", encoding="utf-8")
    store.complete_cvekit_run(ctx1, [log1], "success")

    ctx2 = store.begin_cvekit_run(task_dir.name, "op-2")
    log2 = _run_all_log(tmp_path, ctx2)
    log2.parent.mkdir(parents=True, exist_ok=True)
    log2.write_text("second-chunk\n", encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "write_json", boom)
    store.complete_cvekit_run(ctx2, [log2], "success")

    # 聚合失败不抛错、不破坏已有 chunk;源日志完整
    task_all = _task_all_log(tmp_path, task_dir.name)
    assert "first-chunk" in task_all.read_text(encoding="utf-8")
    assert log2.read_text(encoding="utf-8") == "second-chunk\n"


def test_complete_cvekit_run_out_of_order_merges_by_sequence(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    store = BackportRunStore(tmp_path / "runs")
    task_dir = _new_task(store, tmp_path)
    ctx1 = store.begin_cvekit_run(task_dir.name, "op-1")
    ctx2 = store.begin_cvekit_run(task_dir.name, "op-2")
    log1 = _run_all_log(tmp_path, ctx1)
    log2 = _run_all_log(tmp_path, ctx2)
    log1.parent.mkdir(parents=True, exist_ok=True)
    log2.parent.mkdir(parents=True, exist_ok=True)
    log1.write_text("chunk-1\n", encoding="utf-8")
    log2.write_text("chunk-2\n", encoding="utf-8")

    # 乱序完成:sequence=2 先 complete,sequence=1 后 complete
    store.complete_cvekit_run(ctx2, [log2], "success")
    store.complete_cvekit_run(ctx1, [log1], "success")

    task_all = _task_all_log(tmp_path, task_dir.name)
    content = task_all.read_text(encoding="utf-8")
    # 聚合顺序必须与 sequence 一致(1 在 2 前),不能按完成顺序
    assert content.index("chunk-1") < content.index("chunk-2")
    assert "chunk-2" in content
    runs = store.read_manifest(task_dir)["logging"]["cvekit_runs"]
    assert all(r["merged"] is True for r in runs)


def test_begin_cvekit_run_registration_failure_still_returns_run_id(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    store = BackportRunStore(tmp_path / "runs")
    task_dir = _new_task(store, tmp_path)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "write_json", boom)
    ctx = store.begin_cvekit_run(task_dir.name, "op-1")

    # 登记失败仍返回可注入的 run ID(解耦"生成+注入"与"登记"),task_dir 为空标记未登记
    assert ctx is not None
    assert re.fullmatch(r"\d{14}-[0-9a-f]{8}", ctx.cvekit_run_id)
    assert ctx.task_dir is None
    # 未登记 run 不参与聚合(complete 直接返回)
    log = _run_all_log(tmp_path, ctx)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("orphan\n", encoding="utf-8")
    store.complete_cvekit_run(ctx, [log], "success")
    assert not _task_all_log(tmp_path, task_dir.name).exists()


def test_update_manifest_uses_same_task_lock_as_cvekit_runs(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    store = BackportRunStore(tmp_path / "runs")
    task_dir = _new_task(store, tmp_path)

    def updater() -> None:
        store.update_manifest(task_dir, {"status": "running"})
        ctx = store.begin_cvekit_run(task_dir.name, "op-1")
        log = _run_all_log(tmp_path, ctx)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("locked\n", encoding="utf-8")
        store.complete_cvekit_run(ctx, [log], "success")

    threads = [threading.Thread(target=updater) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 并发下 update_manifest 与 begin/complete 互不覆盖:状态不回退,新增字段不丢
    manifest = store.read_manifest(task_dir)
    assert manifest["status"] == "running"
    assert len(manifest["logging"]["cvekit_runs"]) == 4
    assert all(r["merged"] is True for r in manifest["logging"]["cvekit_runs"])
    task_all = _task_all_log(tmp_path, task_dir.name)
    assert task_all.read_text(encoding="utf-8").count("locked") == 4
