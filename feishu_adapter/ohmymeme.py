"""Bridge to WebUI._do_import in OhMyMeme dev 3a3cad5c6a17544718ca5eb309a2ab77e7dadd3b."""

import json
from pathlib import Path

from .adapter import AdapterError, verify_media


def import_into_ohmymeme(
    export_dir,
    import_files,
    *,
    ensure_collection=None,
    progress_cb=None,
    cancelled=None,
    source="feishu",
):
    """Pass ui._do_import and optionally ui.ensure_import_collection.

    Revalidates files before import. Refuses partial/changed snapshots. The
    caller owns scheduling; invoke from its import worker, not the UI thread.
    Returns explicit unaccounted count because upstream can swallow exceptions.
    """
    cancelled = cancelled or (lambda: False)
    if source not in ("feishu", "wecom", "wechat"):
        raise AdapterError("unsupported_import_source")
    group_name = {"feishu": "飞书", "wecom": "企业微信", "wechat": "微信"}[source]
    allow_partial = source in ("wecom", "wechat")
    root = Path(export_dir).resolve()
    report = json.loads((root / "manifest.json").read_text())
    expected_source = (
        "wechat_exported_stickers"
        if source == "wechat"
        else source + "_favorite_stickers"
    )
    source_verified = (
        report.get("export_completed")
        if source == "wechat"
        else report.get("snapshot_consistent")
    )
    if (
        report.get("schema_version") != 1
        or report.get("source") != expected_source
        or report.get("status")
        not in (("ready", "partial") if allow_partial else ("ready",))
        or not report.get("import_ready")
        or not source_verified
        or (not allow_partial and not report.get("all_returned_recovered"))
    ):
        raise AdapterError("export_not_ready")
    if len(report["records"]) != report["returned_count"]:
        raise AdapterError("manifest_count_mismatch")
    paths, names, seen = [], [], set()
    for record in report["records"]:
        if cancelled():
            return {
                "ids": [],
                "rejected": 0,
                "skipped_dup": 0,
                "unaccounted": 0,
                "submitted": 0,
                "cancelled": True,
            }
        if record["status"] != "recovered":
            if allow_partial and record["status"] == "failed":
                continue
            raise AdapterError("export_not_ready")
        path = (root / record["file"]).resolve()
        if path.parent != root / "assets" or not path.is_file():
            raise AdapterError("asset_path_rejected")
        if path.stat().st_size > 50 * 1024 * 1024:
            raise AdapterError("resource_size_limit")
        media = verify_media(
            path.read_bytes(),
            allow_gif_trailing=source in ("wecom", "wechat"),
            allow_image_trailing=source == "wechat",
        )
        if media["sha256"] != record["media"]["sha256"]:
            raise AdapterError("asset_hash_mismatch")
        if media["sha256"] in seen:
            continue
        seen.add(media["sha256"])
        paths.append(str(path))
        names.append(group_name + "表情_" + record["source_id_sha256"][:12])
    if not paths:
        return {
            "ids": [],
            "rejected": 0,
            "skipped_dup": 0,
            "unaccounted": 0,
            "submitted": 0,
        }
    stopped = False

    def progress(done, total, current):
        nonlocal stopped
        if cancelled():
            stopped = True
            return False
        if progress_cb is not None and progress_cb(done, total, current) is False:
            stopped = True
            return False
        return True

    if cancelled():
        return {
            "ids": [],
            "rejected": 0,
            "skipped_dup": 0,
            "unaccounted": 0,
            "submitted": 0,
            "cancelled": True,
        }
    result = import_files(paths, names=names, progress_cb=progress)
    ids = result["ids"]
    unaccounted = len(paths) - len(ids) - result["rejected"] - result["skipped_dup"]
    if unaccounted < 0:
        raise AdapterError("unexpected_import_result")
    result = dict(result, submitted=len(paths), unaccounted=unaccounted)
    collection_ids = list(dict.fromkeys(ids + result.get("existing_ids", [])))
    preserve_order = (
        source == "wechat" and report.get("ordering") == "upstream_export_desc"
    )
    if preserve_order:
        ordered_ids = result.get("ordered_ids")
        if not isinstance(ordered_ids, list) or set(ordered_ids) != set(collection_ids):
            result["collection_status"] = "failed_after_import"
            return result
        collection_ids = list(dict.fromkeys(ordered_ids))
    if stopped or cancelled():
        result["cancelled"] = True
    if collection_ids and ensure_collection is not None and not result.get("cancelled"):
        try:
            collection_id = (
                ensure_collection(collection_ids, group_name, preserve_order=True)
                if preserve_order
                else ensure_collection(collection_ids, group_name)
            )
            result["collection_status"] = (
                "created_or_updated"
                if type(collection_id) is int and collection_id > 0
                else "failed_after_import"
            )
        except Exception:
            result["collection_status"] = "failed_after_import"
    return result
