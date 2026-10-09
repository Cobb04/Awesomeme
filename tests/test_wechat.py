import io
import json
import threading

import pytest
from PIL import Image
from test_wecom_integration import ui as _ui

from feishu_adapter.adapter import AdapterError, verify_media
from feishu_adapter.desktop import ImportJob
from feishu_adapter.ohmymeme import import_into_ohmymeme
from wechat_adapter import WeChatAdapter
from wechat_adapter.client import UpstreamClient

isolated_ui = _ui


def test_wechat_source_order_survives_repeat_and_cross_source_dedup(isolated_ui):
    host, db, cfg = isolated_ui

    class OrderedExport(ExportedFiles):
        def export_to(self, *args):
            counts = super().export_to(*args)
            counts["ordered_files"] = ["static.jpg", "animated.gif", "encrypted.jpg"]
            return counts

    # The oldest item already exists from another source with a smaller DB ID.
    earlier = cfg.data_dir / "earlier.png"
    earlier.write_bytes(picture())
    existing = host._do_import([str(earlier)])["ids"][0]
    other = host.ensure_import_collection([existing], "飞书")
    for name in ("first", "repeat"):
        export = cfg.data_dir / name
        WeChatAdapter(OrderedExport()).export(export)
        result = import_into_ohmymeme(
            export,
            host._do_import,
            ensure_collection=host.ensure_import_collection,
            source="wechat",
        )
        assert result["collection_status"] == "created_or_updated"
        cid = next(c[0] for c in db.get_collections() if c[1] == "微信")
        assert [m["mime_type"] for m in db.search(collection_id=cid)] == [
            "image/gif",
            "image/png",
        ]
        assert [m["mime_type"] for m in db.search()] == ["image/gif", "image/png"]
        assert [m["id"] for m in db.search(collection_id=other)] == [existing]
        assert db.count() == 2


def test_import_order_preserves_other_slots_metadata_and_rolls_back(isolated_ui):
    _, db, _ = isolated_ui
    ids = [db.add_meme(f"{i}.png", f"hash-{i}") for i in range(4)]
    cid = db.create_collection("微信")
    other = db.create_collection("其他")
    for mid in ids:
        db.add_to_collection(mid, other)
    for mid in (ids[0], ids[2]):
        db.add_to_collection(mid, cid)
    db.reorder_memes(ids)
    before = [{k: v for k, v in m.items() if k != "sort_order"} for m in db.get_all()]
    db.apply_import_order(cid, [ids[2], ids[0]])
    assert [m["id"] for m in db.search()] == [ids[2], ids[1], ids[0], ids[3]]
    assert [m["id"] for m in db.search(collection_id=other)] == ids
    after = [{k: v for k, v in m.items() if k != "sort_order"} for m in db.get_all()]
    assert sorted(before, key=lambda m: m["id"]) == sorted(after, key=lambda m: m["id"])
    with pytest.raises(ValueError):
        db.apply_import_order(cid, [ids[1]])
    assert [m["id"] for m in db.search()] == [ids[2], ids[1], ids[0], ids[3]]


def test_upstream_url_order_is_used_before_private_metadata_cleanup(tmp_path):
    download = tmp_path / "download"
    metadata = download / "导出信息"
    metadata.mkdir(parents=True)
    (metadata / "emoticon_urls.txt").write_text(
        "https://example.invalid/stodownload?m=zz-old\n"
        "https://example.invalid/stodownload?m=missing\n"
        "https://example.invalid/stodownload?m=aa-new\n"
    )
    (download / "zz-old.gif").write_bytes(picture(True))
    (download / "aa-new.png").write_bytes(picture())
    from wechat_adapter.client import source_ordered_files

    assert source_ordered_files(download, 3) == ["zz-old.gif", "aa-new.png"]
    (download / "unmapped.png").write_bytes(picture())
    with pytest.raises(AdapterError, match="source_order_unavailable"):
        source_ordered_files(download, 3)


def picture(animated=False):
    output = io.BytesIO()
    frames = [Image.new("RGB", (20, 20), color) for color in ("red", "blue")]
    if animated:
        frames[0].save(
            output, format="GIF", save_all=True, append_images=frames[1:], duration=80
        )
    else:
        frames[0].save(output, format="PNG")
    return output.getvalue()


class ExportedFiles:
    def export_to(self, directory, progress, cancelled):
        target = directory / "download"
        target.mkdir()
        for name, body in {
            "static.jpg": picture(),
            "animated.gif": picture(True),
            "encrypted.jpg": b"not an image",
        }.items():
            (target / name).write_bytes(body)
        return {"total": 3, "ok": 3, "failed": 0, "skipped": 0}


def test_partial_preserves_media_and_reports_unknown_completeness(tmp_path):
    report = WeChatAdapter(ExportedFiles()).export(tmp_path / "export")
    assert report["status"] == "partial"
    assert report["failed_count"] == 1
    assert report["unique_files"] == 2
    assert report["collection_completeness"] == "unverified"
    assert not report["snapshot_consistent"]
    assert report["export_completed"]
    recovered = [r for r in report["records"] if r["status"] == "recovered"]
    assert {r["media"]["frame_count"] for r in recovered} == {1, 2}
    assert {(tmp_path / "export" / r["file"]).read_bytes() for r in recovered} == {
        picture(),
        picture(True),
    }


def test_real_settings_repeat_and_cross_source(isolated_ui, monkeypatch):
    host, db, cfg = isolated_ui
    import wechat_adapter

    monkeypatch.setattr(
        wechat_adapter, "WeChatAdapter", lambda: WeChatAdapter(ExportedFiles())
    )
    api = host._settings_api
    generation = api.get_wechat_mac_import_progress()["generation"]
    assert not api.start_wechat_mac_import(generation)["ok"]
    assert api.start_wechat_mac_import(generation, True)["ok"]
    host._wechat_mac_job._thread.join(10)
    first = api.get_wechat_mac_import_progress()
    assert first["status"] == "partial"
    assert first["result"]["failed_downloads"] == 1
    assert len(first["result"]["ids"]) == 2
    before = {p.name: p.read_bytes() for p in cfg.cache_dir.iterdir() if p.is_file()}
    host.ensure_import_collection(first["result"]["ids"], "飞书")
    assert api.start_wechat_mac_import(first["generation"], True)["ok"]
    host._wechat_mac_job._thread.join(10)
    second = api.get_wechat_mac_import_progress()
    assert second["result"]["skipped_dup"] == 2
    assert second["result"]["ids"] == []
    assert len(db.get_all()) == 2
    assert before == {
        p.name: p.read_bytes() for p in cfg.cache_dir.iterdir() if p.is_file()
    }
    assert (
        db._get_conn().execute("SELECT COUNT(*) FROM meme_collections").fetchone()[0]
        == 4
    )


def test_cancel_invalidates_pending_start_and_window_close(isolated_ui, monkeypatch):
    host, _, _ = isolated_ui
    api = host._settings_api
    monkeypatch.setattr(ImportJob, "start", lambda _: pytest.fail("must not start"))
    old = api.get_wechat_mac_import_progress()["generation"]
    api.cancel_wechat_mac_import(old)
    assert not api.start_wechat_mac_import(old, True)["ok"]
    assert not api.start_wechat_mac_import(True, True)["ok"]
    host._settings_window = object()
    host._on_feishu_settings_closed(object())
    assert host._wechat_mac_next_generation == old + 1
    host._on_feishu_settings_closed(host._settings_window)
    assert host._wechat_mac_next_generation == old + 2


def test_cancel_before_validation_prevents_library_call(tmp_path):
    stopped = threading.Event()

    class CancelledExport(ExportedFiles):
        def export_to(self, *args):
            counts = super().export_to(*args)
            stopped.set()
            return counts

    report = WeChatAdapter(CancelledExport()).export(
        tmp_path / "export", cancelled=stopped.is_set
    )
    assert report["status"] == "cancelled" and not report["import_ready"]
    with pytest.raises(AdapterError, match="export_not_ready"):
        import_into_ohmymeme(
            tmp_path / "export", lambda _: pytest.fail("imported"), source="wechat"
        )


def test_modified_asset_refused_and_wrong_source_refused(tmp_path):
    target = tmp_path / "export"
    report = WeChatAdapter(ExportedFiles()).export(target)
    asset = next(r for r in report["records"] if r["status"] == "recovered")
    (target / asset["file"]).write_bytes(picture(not asset["media"]["animated"]))
    with pytest.raises(AdapterError, match="asset_hash_mismatch"):
        import_into_ohmymeme(target, lambda _: pytest.fail("imported"), source="wechat")
    with pytest.raises(AdapterError, match="export_not_ready"):
        import_into_ohmymeme(target, lambda _: pytest.fail("imported"), source="wecom")


@pytest.mark.parametrize("failure", ["cancel", "exit", "malformed", "success"])
def test_runner_cleans_sensitive_files_on_every_exit(tmp_path, monkeypatch, failure):
    import wechat_adapter.client as module

    client = UpstreamClient()
    client.cache = tmp_path / "cache"
    client.copy = tmp_path / "WeChat.app"
    client.binary_bytes = b"fake"
    root = tmp_path / "run"
    root.mkdir()
    monkeypatch.setattr(client, "preflight", lambda: "test-account")
    commands = []

    class Result:
        returncode = 0

    def run(args, **kwargs):
        commands.append(args)
        return Result()

    class Process:
        returncode = 1 if failure == "exit" else 0
        pid = 123456

        def poll(self):
            return None if failure == "cancel" else self.returncode

    def spawn(args, **kwargs):
        client.copy.mkdir()
        (client.cache / "secret").write_text("PRIVATE_KEY")
        metadata = root / "download/导出信息"
        metadata.mkdir(parents=True)
        (metadata / "emoticon_urls.txt").write_text("PRIVATE_URL")
        kwargs["stdout"].write(
            b"invalid"
            if failure == "malformed"
            else json.dumps(
                {"total": 0, "ok": 0, "failed": 0, "skipped": 0, "wxid": "PRIVATE_ID"}
            ).encode()
        )
        kwargs["stdout"].flush()
        assert kwargs["start_new_session"]
        return Process()

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    stopped = []
    monkeypatch.setattr(client, "_stop", lambda p: stopped.append(p))
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    cancel_calls = 0

    def cancelled():
        nonlocal cancel_calls
        cancel_calls += 1
        return failure == "cancel" and cancel_calls >= 3

    if failure == "success":
        assert client.export_to(root, lambda _: None, cancelled) == {
            "total": 0,
            "ok": 0,
            "failed": 0,
            "skipped": 0,
        }
    else:
        with pytest.raises((AdapterError, ValueError)):
            client.export_to(root, lambda _: None, cancelled)
    assert stopped
    assert not client.cache.exists() and not client.copy.exists()
    assert not (root / "private").exists()
    assert not (root / "download/导出信息").exists()
    assert commands[-1][0] == "/usr/bin/open"


def test_existing_cache_never_deleted(tmp_path, monkeypatch):
    client = UpstreamClient()
    client.cache = tmp_path / "existing"
    client.cache.mkdir()
    (client.cache / "keep").write_text("prior user file")
    monkeypatch.setattr(client, "preflight", lambda: "one-account")
    with pytest.raises(AdapterError, match="upstream_cache_exists"):
        client.export_to(tmp_path, lambda _: None, lambda: False)
    assert (client.cache / "keep").read_text() == "prior user file"


def test_host_waits_for_cleanup_before_destroying_windows(isolated_ui):
    host, _, _ = isolated_ui
    events = []

    class Job:
        def cancel(self):
            events.append("cancel")

        def wait(self):
            events.append("cleanup_finished")

    class Window:
        def destroy(self):
            events.append("destroy")

    host._wechat_mac_job = Job()
    host._window = Window()
    host._save_window_position = lambda: None
    host.stop()
    assert events == ["cancel", "cleanup_finished", "destroy"]


@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
def test_bounded_trailing_metadata_preserves_hash_and_rejects_truncation(fmt):
    import hashlib

    out = io.BytesIO()
    Image.new("RGB", (12, 12), "green").save(out, format=fmt)
    raw = out.getvalue()
    body = raw + b"trailing-wechat-metadata"
    with pytest.raises(AdapterError):
        verify_media(body)
    media = verify_media(body, allow_image_trailing=True)
    assert media["sha256"] == hashlib.sha256(body).hexdigest()
    assert media["trailing_bytes"] == len(body) - len(raw)
    assert media["size"] == len(body)
    assert media["pixel_validation"] == "all_frames_decoded"
    for broken in [raw + b"x" * 4097, raw[:-2], raw[:30] + b"x" * 60]:
        with pytest.raises(AdapterError):
            verify_media(broken, allow_image_trailing=True)
