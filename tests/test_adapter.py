import copy
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from feishu_adapter import AdapterError, FeishuAdapter, import_into_ohmymeme
from feishu_adapter._http import ResourceHTTPError
from feishu_adapter.adapter import verify_media


def png():
    stream = io.BytesIO()
    Image.new("RGB", (8, 6), "red").save(stream, "PNG")
    return stream.getvalue()


def sticker(identifier="1", body=None):
    body = body or png()
    return {
        "stickerId": identifier,
        "image": {
            "origin": {
                "key": "test-" + identifier,
                "fsUnit": "test-cdn",
                "type": "NORMAL",
                "width": 8,
                "height": 6,
                "size": len(body),
            }
        },
    }


class Backend:
    def __init__(self, stickers=None, after=None):
        self.before = {
            "stickers": [sticker()] if stickers is None else stickers,
            "updateTime": 1,
        }
        self.after = self.before if after is None else after
        self.calls = 0
        self.closed = False

    def login(self, on_qr, cancelled):
        on_qr(b"<svg/>")

    def snapshot(self):
        self.calls += 1
        return copy.deepcopy(self.before if self.calls == 1 else self.after)

    def resource_config(self, snapshot):
        units = {
            s.get("image", {}).get("origin", {}).get("fsUnit")
            for s in snapshot["stickers"]
        }
        return {
            "resource_url_tpl": {
                "fs_unit_tpl": {
                    u: {
                        "hosts": ["https://test.feishucdn.com"],
                        "path_tpl": "/{{key}}",
                        "param": {},
                    }
                    for u in units
                    if u
                }
            }
        }

    def close(self):
        self.closed = True


def export(tmp_path, backend=None, downloader=None, **kwargs):
    backend = backend or Backend()
    adapter = FeishuAdapter(
        backend=backend, downloader=downloader or (lambda *_: png()), **kwargs
    )
    root = tmp_path / "export"
    report = adapter.export(root, on_qr=lambda svg: None)
    assert backend.closed
    assert report == json.loads((root / "manifest.json").read_text())
    return root, report


def test_export_and_bridge_deduplicate_content(tmp_path):
    root, result = export(tmp_path, Backend([sticker("1"), sticker("2")]))
    assert result["import_ready"] and result["unique_files"] == 1
    assert result["returned_count"] == 2
    assert result["collection_completeness"] == "server_limit_unverified"
    calls = []

    def importer(paths, **kwargs):
        calls.append((paths, kwargs))
        return {"ids": [9], "rejected": 0, "skipped_dup": 0}

    groups = []
    imported = import_into_ohmymeme(
        root, importer, ensure_collection=lambda *args: groups.append(args)
    )
    assert imported["submitted"] == 1 and imported["unaccounted"] == 0
    assert len(calls[0][0]) == 1 and groups == [([9], "飞书")]
    text = (root / "manifest.json").read_text()
    assert "test-1" not in text and "feishucdn.com" not in text


@pytest.mark.parametrize("mutation", ["add", "metadata", "update_time"])
def test_changed_snapshot_blocks_import(tmp_path, mutation):
    backend = Backend()
    backend.after = copy.deepcopy(backend.before)
    if mutation == "add":
        backend.after["stickers"].append(sticker("2"))
    elif mutation == "metadata":
        backend.after["stickers"][0]["image"]["origin"]["key"] = "changed"
    else:
        backend.after["updateTime"] = 2
    root, result = export(tmp_path, backend)
    assert result["status"] == "snapshot_changed" and not result["import_ready"]
    with pytest.raises(AdapterError, match="export_not_ready"):
        import_into_ohmymeme(root, lambda *a, **k: pytest.fail("must not import"))


@pytest.mark.parametrize("mode", ["encrypted", "missing", "corrupt", "metadata"])
def test_bad_resource_is_reported_not_imported(tmp_path, mode):
    item = sticker()
    body = png()
    if mode == "encrypted":
        item["encrypted"] = True
    elif mode == "missing":
        item["image"] = {}
    elif mode == "corrupt":
        body = body[:-8]
    else:
        item["image"]["origin"]["width"] = 20
    _, result = export(tmp_path, Backend([item]), lambda *_: body)
    assert result["status"] == "partial" and not result["import_ready"]


@pytest.mark.parametrize("status", [401, 403, 429])
def test_access_stop_does_not_retry_or_recheck(tmp_path, status):
    calls = []
    backend = Backend([sticker("1"), sticker("2")])

    def download(*args):
        calls.append(args)
        raise ResourceHTTPError("http_access_stop_" + str(status), stop=True)

    _, result = export(tmp_path, backend, download)
    assert result["status"] == "stopped" and len(calls) == 1 and backend.calls == 1
    assert result["charged_bytes"] == 50 * 1024 * 1024


def test_empty_snapshot_is_not_completeness_proof(tmp_path):
    root, result = export(tmp_path, Backend([]))
    assert result["returned_count"] == 0 and result["import_ready"]
    assert result["collection_completeness"] == "server_limit_unverified"
    assert (
        import_into_ohmymeme(root, lambda *a: pytest.fail("empty import"))["submitted"]
        == 0
    )


def test_total_budget_stops(tmp_path):
    _, result = export(
        tmp_path, Backend([sticker("1"), sticker("2")]), max_total_bytes=len(png())
    )
    assert result["reason"] == "total_byte_limit"
    assert not result["import_ready"]


def test_cancel_and_single_use(tmp_path):
    backend = Backend()
    adapter = FeishuAdapter(backend=backend)
    result = adapter.export(
        tmp_path / "cancelled", on_qr=lambda _: None, cancelled=lambda: True
    )
    assert result["status"] == "cancelled" and backend.closed and backend.calls == 0
    with pytest.raises(AdapterError, match="adapter_already_used"):
        adapter.export(tmp_path / "again", on_qr=lambda _: None)


@pytest.mark.parametrize("items", [[sticker("1"), sticker("1")], [sticker("")]])
def test_invalid_ids_stop(tmp_path, items):
    _, result = export(tmp_path, Backend(items))
    assert result["status"] == "stopped" and result["unique_files"] == 0


def test_tampered_asset_rejected(tmp_path):
    root, result = export(tmp_path)
    target = root / result["records"][0]["file"]
    stream = io.BytesIO()
    Image.new("RGB", (8, 6), "blue").save(stream, "PNG")
    target.write_bytes(stream.getvalue())
    with pytest.raises(AdapterError, match="asset_hash_mismatch"):
        import_into_ohmymeme(root, lambda *a: pytest.fail("tampered"))


def test_upstream_unaccounted_and_group_failure(tmp_path):
    root, _ = export(tmp_path)
    result = import_into_ohmymeme(
        root, lambda *a, **k: {"ids": [], "rejected": 0, "skipped_dup": 0}
    )
    assert result["unaccounted"] == 1
    result = import_into_ohmymeme(
        root,
        lambda *a, **k: {"ids": [1], "rejected": 0, "skipped_dup": 0},
        ensure_collection=lambda *args: -1,
    )
    assert result["ids"] == [1] and result["collection_status"] == "failed_after_import"


def test_replay_four_real_downloads(tmp_path):
    run = (
        Path(__file__).resolve().parents[1]
        / "investigation/feishu/live_probe/runs/20261005T165607Z"
    )
    if not run.exists():
        pytest.skip("Private local evidence is not distributed with the package")
    before = json.loads((run / "list-3.json").read_text())
    manifest = json.loads((run / "resources/manifest-001-download.json").read_text())
    bodies = [
        (run / "resources" / record["file"]).read_bytes()
        for record in manifest["records"]
    ]
    by_key = {
        item["image"]["origin"]["key"]: body
        for item, body in zip(before["stickers"], bodies)
    }
    backend = Backend(before["stickers"], json.loads((run / "list-4.json").read_text()))
    backend.before = before
    root, result = export(
        tmp_path, backend, lambda url, *_: by_key[url.rsplit("/", 1)[-1]]
    )
    assert result["import_ready"] and result["unique_files"] == 4
    assert result["charged_bytes"] == 4236418
    assert [r["media"]["sha256"] for r in result["records"]] == [
        r["media"]["sha256"] for r in manifest["records"]
    ]
    assert result["records"][2]["media"]["frame_count"] == 10


def test_apng_default_image_decodes_separately():
    frames = [Image.new("RGB", (8, 6), color) for color in ("red", "blue", "green")]
    stream = io.BytesIO()
    frames[0].save(
        stream,
        "PNG",
        save_all=True,
        append_images=frames[1:],
        default_image=True,
        duration=100,
    )
    result = verify_media(stream.getvalue())
    assert result["frame_count"] == 2
    assert result["decoded_image_count"] == 3
    assert result["default_image"] is True


def test_cancellation_during_final_snapshot_is_honoured(tmp_path):
    cancelled = False

    class CancellingBackend(Backend):
        def snapshot(self):
            nonlocal cancelled
            result = super().snapshot()
            if self.calls == 2:
                cancelled = True
            return result

    backend = CancellingBackend()
    result = FeishuAdapter(backend=backend, downloader=lambda *_: png()).export(
        tmp_path / "cancelled-final", on_qr=lambda _: None, cancelled=lambda: cancelled
    )
    assert result["status"] == "cancelled" and not result["import_ready"]
    assert backend.closed
