import threading

from test_adapter import Backend, export, png, sticker

from feishu_adapter import FeishuAdapter, import_into_ohmymeme
from feishu_adapter.desktop import ImportJob


def complete(job):
    job._thread.join(timeout=3)
    assert not job._thread.is_alive()
    assert not job.state()["busy"] and job.state()["qr_src"] == ""
    return job.state()


def test_job_reuses_importer_and_reports_result(tmp_path):
    imported = []
    grouped = []

    def importer(paths, **kwargs):
        imported.extend(paths)
        assert kwargs["progress_cb"](1, 1, "unused") is True
        return {"ids": [1], "rejected": 0, "skipped_dup": 0}

    def group(ids, name):
        grouped.append((ids, name))
        return 4

    job = ImportJob(
        importer,
        group,
        tmp_path,
        adapter_factory=lambda: FeishuAdapter(
            backend=Backend(), downloader=lambda *_: png()
        ),
    )
    assert job.start()["ok"]
    state = complete(job)
    assert (
        state["status"] == "done" and len(imported) == 1 and grouped == [([1], "飞书")]
    )
    assert state["collection_completeness"] == "server_limit_unverified"
    assert job.start()["ok"]
    assert complete(job)["status"] == "done"


def test_cancel_clears_qr_prevents_overlap_and_import(tmp_path):
    ready = threading.Event()
    release = threading.Event()

    class WaitingBackend(Backend):
        def login(self, on_qr, cancelled):
            on_qr(b"<svg/>")
            ready.set()
            assert release.wait(timeout=3)

    imports = []
    job = ImportJob(
        lambda *args, **kwargs: imports.append(args),
        None,
        tmp_path,
        adapter_factory=lambda: FeishuAdapter(
            backend=WaitingBackend(), downloader=lambda *_: png()
        ),
    )
    job.start()
    assert ready.wait(timeout=3)
    assert job.state()["status"] == "waiting_scan" and job.state()["qr_src"]
    job.cancel()
    assert job.state()["qr_src"] == "" and not job.start()["ok"]
    release.set()
    assert complete(job)["status"] == "cancelled" and not imports


def test_failed_export_never_reaches_importer(tmp_path):
    item = sticker()
    item["encrypted"] = True
    imports = []
    job = ImportJob(
        lambda *a, **k: imports.append(a),
        None,
        tmp_path,
        adapter_factory=lambda: FeishuAdapter(
            backend=Backend([item]), downloader=lambda *_: png()
        ),
    )
    job.start()
    state = complete(job)
    assert state["status"] == "error" and not imports


def test_start_failure_is_visible_and_clears_busy(tmp_path):
    def factory():
        raise RuntimeError("PRIVATE_SENTINEL")

    job = ImportJob(None, None, tmp_path, adapter_factory=factory)
    job.start()
    state = complete(job)
    assert state["status"] == "error" and "PRIVATE_SENTINEL" not in str(state)


def test_bridge_cancel_before_import(tmp_path):
    root, _ = export(tmp_path)
    calls = []
    result = import_into_ohmymeme(
        root, lambda *a, **k: calls.append(a), cancelled=lambda: True
    )
    assert result["cancelled"] and result["submitted"] == 0 and not calls


def test_bridge_cancel_during_import_skips_grouping(tmp_path):
    root, _ = export(tmp_path)
    groups = []

    def importer(paths, **kwargs):
        assert kwargs["progress_cb"](1, 1, "unused") is False
        return {"ids": [1], "rejected": 0, "skipped_dup": 0}

    result = import_into_ohmymeme(
        root,
        importer,
        progress_cb=lambda *_: False,
        ensure_collection=lambda *a: groups.append(a),
    )
    assert result["cancelled"] and result["ids"] == [1] and not groups
