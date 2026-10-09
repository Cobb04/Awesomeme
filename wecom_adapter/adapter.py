"""Export the current client's favorite media without re-encoding it."""

import hashlib
import json
import re
from pathlib import Path

from feishu_adapter.adapter import EXTENSIONS, AdapterError, _write_new, verify_media

from ._http import MAX_BYTES, fetch

FIELDS = {"collectionId", "fileId", "emoUrl", "md5", "size", "width", "height", "type"}
MAX_ITEMS = 1000


def validate_rows(rows):
    if not isinstance(rows, list) or len(rows) > MAX_ITEMS:
        raise AdapterError("collection_limit")
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != FIELDS:
            raise AdapterError("collection_schema")
        if any(
            type(row[k]) is not int
            for k in ("collectionId", "size", "width", "height", "type")
        ):
            raise AdapterError("collection_schema")
        if row["collectionId"] in ids:
            raise AdapterError("duplicate_source_id")
        ids.add(row["collectionId"])
        for key in ("fileId", "emoUrl", "md5"):
            if row[key] is not None and (
                not isinstance(row[key], str) or len(row[key]) > 8192
            ):
                raise AdapterError("collection_schema")


def fingerprint(rows):
    # Order changes alone do not change membership or media identity.
    return hashlib.sha256(
        json.dumps(
            sorted(rows, key=lambda r: r["collectionId"]), sort_keys=True
        ).encode()
    ).hexdigest()


def check_cancel(cancelled):
    if cancelled():
        raise AdapterError("cancelled")


class WeComAdapter:
    """Single-use export; injected backend/downloader permit account-free tests."""

    def __init__(self, *, backend=None, downloader=fetch, max_total_bytes=1024**3):
        if type(max_total_bytes) is not int or max_total_bytes < 1:
            raise ValueError("invalid_byte_limit")
        self.backend = backend
        self.downloader = downloader
        self.max_total_bytes = max_total_bytes
        self.used = False

    def export(self, output_dir, *, on_qr=None, progress=None, cancelled=None):
        """Write a redacted manifest and unchanged media to a new private directory."""
        if self.used:
            raise AdapterError("adapter_already_used")
        self.used = True
        progress = progress or (lambda event: None)
        cancelled = cancelled or (lambda: False)
        root = Path(output_dir).absolute()
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        (root / "assets").mkdir(mode=0o700)
        report = dict(
            schema_version=1,
            source="wecom_favorite_stickers",
            status="running",
            collection_completeness="current_client_snapshot",
            snapshot_consistent=False,
            all_returned_recovered=False,
            import_ready=False,
            returned_count=0,
            unique_files=0,
            failed_count=0,
            warning_count=0,
            charged_bytes=0,
            records=[],
        )
        try:
            check_cancel(cancelled)
            backend = self.backend
            if backend is None:
                from .client import WeComClient

                backend = WeComClient()
            progress({"event": "reading"})
            before = backend.snapshot(cancelled)
            validate_rows(before)
            check_cancel(cancelled)
            report["returned_count"] = len(before)
            report["snapshot_sha256"] = fingerprint(before)
            report["profile"] = getattr(backend, "profile", "injected_backend")
            hashes = set()
            for index, row in enumerate(before):
                check_cancel(cancelled)
                record = dict(
                    source_index=index,
                    source_id_sha256=hashlib.sha256(
                        str(row["collectionId"]).encode()
                    ).hexdigest(),
                    status="failed",
                )
                report["records"].append(record)
                try:
                    allowance = min(
                        MAX_BYTES, self.max_total_bytes - report["charged_bytes"]
                    )
                    if allowance <= 0:
                        raise AdapterError("total_byte_limit")
                    report["charged_bytes"] += allowance
                    body = self.downloader(row["emoUrl"], allowance, cancelled)
                    check_cancel(cancelled)
                    if len(body) > allowance:
                        raise AdapterError("resource_size_limit")
                    report["charged_bytes"] -= allowance - len(body)
                    media = verify_media(body, allow_gif_trailing=True)
                    checks = {
                        key: row[key] == media[key] if row[key] > 0 else None
                        for key in ("size", "width", "height")
                    }
                    md5 = row["md5"]
                    checks["md5"] = (
                        hashlib.md5(body).hexdigest() == md5.lower()
                        if isinstance(md5, str)
                        and re.fullmatch(r"[a-fA-F0-9]{32}", md5)
                        else None
                    )
                    mismatch = any(value is False for value in checks.values())
                    report["warning_count"] += int(mismatch)
                    name = media["sha256"] + EXTENSIONS[media["mime"]]
                    if media["sha256"] not in hashes:
                        _write_new(root / "assets" / name, body)
                        hashes.add(media["sha256"])
                    record.update(
                        status="recovered",
                        file="assets/" + name,
                        media=media,
                        metadata_checks=checks,
                        metadata_mismatch=mismatch,
                    )
                except AdapterError as exc:
                    if str(exc) == "cancelled":
                        raise
                    record["reason"] = str(exc)
                    report["failed_count"] += 1
                progress({"event": "resource", "done": index + 1, "total": len(before)})
            report["unique_files"] = len(hashes)
            check_cancel(cancelled)
            progress({"event": "checking_snapshot"})
            after = backend.snapshot(cancelled)
            validate_rows(after)
            check_cancel(cancelled)
            if fingerprint(after) != report["snapshot_sha256"]:
                raise AdapterError("snapshot_changed")
            report["snapshot_consistent"] = True
            report["all_returned_recovered"] = report["failed_count"] == 0
            report["status"] = "partial" if report["failed_count"] else "ready"
            report["import_ready"] = bool(hashes) or not before
        except AdapterError as exc:
            report.update(
                status="cancelled" if str(exc) == "cancelled" else "error",
                reason=str(exc),
                import_ready=False,
            )
        except Exception:
            report.update(
                status="error", reason="wecom_export_failed", import_ready=False
            )
        _write_new(
            root / "manifest.json",
            json.dumps(report, ensure_ascii=False, indent=2).encode(),
        )
        return report
