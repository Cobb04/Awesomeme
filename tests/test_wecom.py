import io
import json
import threading

import pytest
from PIL import Image

from feishu_adapter.adapter import AdapterError
from feishu_adapter.desktop import ImportJob
from feishu_adapter.ohmymeme import import_into_ohmymeme
from wecom_adapter import WeComAdapter
from wecom_adapter._http import validate_url
from wecom_adapter.adapter import validate_rows


def media(animated=False):
    buf = io.BytesIO()
    first = Image.new("RGB", (12, 10), "red")
    if animated:
        first.save(
            buf,
            format="GIF",
            save_all=True,
            append_images=[Image.new("RGB", (12, 10), "blue")],
            duration=[40, 80],
            loop=0,
        )
    else:
        first.save(buf, format="PNG")
    return buf.getvalue()


def row(i=1, **fields):
    return dict(
        collectionId=i,
        fileId="PRIVATE_FILE",
        emoUrl=f"https://wework.qpic.cn/PRIVATE/{i}",
        md5="0" * 32,
        size=0,
        width=900,
        height=800,
        type=99,
        **fields,
    )


class Backend:
    def __init__(self, rows, after=None):
        self.rows, self.after, self.calls = rows, after, 0

    def snapshot(self, cancelled):
        self.calls += 1
        return self.after if self.calls > 1 and self.after is not None else self.rows


def test_preserves_bytes_animation_and_accepts_metadata_mismatch(tmp_path):
    bodies = {1: media(), 2: media(True)}
    root = tmp_path / "export"
    report = WeComAdapter(
        backend=Backend([row(1), row(2)]),
        downloader=lambda url, *_: bodies[int(url[-1])],
    ).export(root)
    assert report["status"] == "ready" and report["warning_count"] == 2
    assert [r["media"]["frame_count"] for r in report["records"]] == [1, 2]
    for r, body in zip(report["records"], bodies.values()):
        assert (root / r["file"]).read_bytes() == body
    assert "PRIVATE" not in (root / "manifest.json").read_text()
    assert "emoUrl" not in json.dumps(report)


def test_complete_gif_with_client_suffix_is_preserved_and_importable(tmp_path):
    body = media(True) + b"client-metadata" + bytes(373)
    root = tmp_path / "export"
    report = WeComAdapter(backend=Backend([row()]), downloader=lambda *_: body).export(
        root
    )
    assert report["status"] == "ready"
    item = report["records"][0]
    assert item["media"]["trailing_bytes"] == 388
    assert item["media"]["frame_count"] == 2
    assert (root / item["file"]).read_bytes() == body
    result = import_into_ohmymeme(
        root, lambda *a, **k: dict(ids=[1], rejected=0, skipped_dup=0), source="wecom"
    )
    assert result["submitted"] == 1


@pytest.mark.parametrize(
    "body", [media(True)[:-1], media(True)[:-5] + b";", media(True) + bytes(4097)]
)
def test_gif_compatibility_still_rejects_truncation_and_excess_suffix(tmp_path, body):
    report = WeComAdapter(backend=Backend([row()]), downloader=lambda *_: body).export(
        tmp_path / "export"
    )
    assert not report["import_ready"] and report["failed_count"] == 1


def test_partial_download_imports_good_files_with_failure_count(tmp_path):
    def download(url, *_):
        if url.endswith("2"):
            raise AdapterError("http_status_404")
        return media()

    groups = []
    job = ImportJob(
        lambda paths, **kw: dict(ids=[1], rejected=0, skipped_dup=0),
        lambda ids, name: groups.append((ids, name)) or 2,
        tmp_path,
        source="wecom",
        adapter_factory=lambda: WeComAdapter(
            backend=Backend([row(1), row(2)]), downloader=download
        ),
    )
    job.start()
    job._thread.join(3)
    state = job.state()
    assert not state["busy"] and state["status"] == "partial"
    assert state["result"]["failed_downloads"] == 1
    assert groups == [([1], "企业微信")]


def test_changed_snapshot_never_imported(tmp_path):
    root = tmp_path / "export"
    report = WeComAdapter(
        backend=Backend([row()], after=[]), downloader=lambda *_: media()
    ).export(root)
    assert report["reason"] == "snapshot_changed" and not report["import_ready"]
    with pytest.raises(AdapterError, match="export_not_ready"):
        import_into_ohmymeme(
            root, lambda *a: pytest.fail("must not import"), source="wecom"
        )


def test_cancel_during_snapshot_waits_then_does_not_download_or_import(tmp_path):
    ready, release = threading.Event(), threading.Event()

    class Wait:
        def snapshot(self, cancelled):
            ready.set()
            assert release.wait(3)
            return [row()]

    job = ImportJob(
        lambda *a: pytest.fail("import after cancel"),
        None,
        tmp_path,
        source="wecom",
        adapter_factory=lambda: WeComAdapter(
            backend=Wait(), downloader=lambda *a: pytest.fail("download after cancel")
        ),
    )
    job.start()
    assert ready.wait(3)
    job.cancel()
    assert not job.start()["ok"]
    release.set()
    job._thread.join(3)
    assert job.state()["status"] == "cancelled" and not job.state()["busy"]


def test_empty_is_success_without_empty_group(tmp_path):
    job = ImportJob(
        lambda *a: pytest.fail("empty import"),
        lambda *a: pytest.fail("empty group"),
        tmp_path,
        source="wecom",
        adapter_factory=lambda: WeComAdapter(backend=Backend([])),
    )
    job.start()
    job._thread.join(3)
    assert job.state()["status"] == "done"
    assert job.state()["message"] == "当前没有收藏表情"


def test_existing_image_is_added_to_source_group(tmp_path):
    root = tmp_path / "export"
    WeComAdapter(backend=Backend([row()]), downloader=lambda *_: media()).export(root)
    groups = []
    result = import_into_ohmymeme(
        root,
        lambda *a, **k: dict(ids=[], existing_ids=[42], rejected=0, skipped_dup=1),
        ensure_collection=lambda ids, name: groups.append((ids, name)) or 1,
        source="wecom",
    )
    assert result["ids"] == [] and result["unaccounted"] == 0
    assert groups == [([42], "企业微信")]


def test_duplicate_files_in_source_only_imported_once(tmp_path):
    root = tmp_path / "export"
    report = WeComAdapter(
        backend=Backend([row(1), row(2)]), downloader=lambda *_: media()
    ).export(root)
    assert report["unique_files"] == 1 and report["returned_count"] == 2

    def importer(paths, **kw):
        assert len(paths) == 1
        return dict(ids=[1], rejected=0, skipped_dup=0)

    assert import_into_ohmymeme(root, importer, source="wecom")["submitted"] == 1


def test_tampered_asset_rejected(tmp_path):
    root = tmp_path / "export"
    report = WeComAdapter(
        backend=Backend([row()]), downloader=lambda *_: media()
    ).export(root)
    (root / report["records"][0]["file"]).write_bytes(media(True))
    with pytest.raises(AdapterError, match="asset_hash_mismatch"):
        import_into_ohmymeme(root, None, source="wecom")


@pytest.mark.parametrize(
    "code",
    ["unsupported_version", "attach_rejected", "client_not_ready", "collection_limit"],
)
def test_actionable_preflight_errors(tmp_path, code):
    class Broken:
        def snapshot(self, cancelled):
            raise AdapterError(code)

    job = ImportJob(
        None,
        None,
        tmp_path,
        source="wecom",
        adapter_factory=lambda: WeComAdapter(backend=Broken()),
    )
    job.start()
    job._thread.join(3)
    assert job.state()["status"] == "error" and job.state()["reason"] == code
    assert job.state()["message"] not in ("导入失败", code)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "http://wework.qpic.cn/a",
        "https://wework.qpic.cn.evil/a",
        "https://evil/a",
        "https://user@wework.qpic.cn/a",
        "https://wework.qpic.cn:444/a",
        "https://wework.qpic.cn/a#x",
        "https://wework.qpic.cn/a\n",
    ],
)
def test_url_allowlist(url):
    with pytest.raises(AdapterError):
        validate_url(url)


def test_snapshot_does_not_silently_truncate_or_accept_duplicate_ids():
    with pytest.raises(AdapterError, match="collection_limit"):
        validate_rows([row(i) for i in range(1001)])
    with pytest.raises(AdapterError, match="duplicate_source_id"):
        validate_rows([row(), row()])


def test_total_budget_counts_failed_attempts(tmp_path):
    calls = []

    def download(*args):
        calls.append(args)
        raise AdapterError("resource_network_error")

    r = WeComAdapter(
        backend=Backend([row(1), row(2)]), downloader=download, max_total_bytes=100
    ).export(tmp_path / "x")
    assert len(calls) == 1 and r["failed_count"] == 2 and not r["import_ready"]
