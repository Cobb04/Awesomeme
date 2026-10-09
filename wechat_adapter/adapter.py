import hashlib
import json
from pathlib import Path

from feishu_adapter.adapter import EXTENSIONS, AdapterError, verify_media

from .client import UpstreamClient


# Adapt the upstream export to the existing shared import manifest.
class WeChatAdapter:
    def __init__(self, client=None):
        self.client = client or UpstreamClient()

    # Validate original bytes before passing any file to the gallery bridge.
    def export(self, directory, *, on_qr=None, progress=None, cancelled=None):
        root = Path(directory)
        root.mkdir(parents=True, mode=0o700, exist_ok=False)
        (root / "assets").mkdir(mode=0o700)
        progress = progress or (lambda _: None)
        cancelled = cancelled or (lambda: False)
        report = dict(
            schema_version=1,
            source="wechat_exported_stickers",
            status="error",
            import_ready=False,
            export_completed=False,
            snapshot_consistent=False,
            collection_completeness="unverified",
            returned_count=0,
            unique_files=0,
            failed_count=0,
            records=[],
        )
        try:
            counts = self.client.export_to(root, progress, cancelled)
            report["returned_count"] = counts["total"]
            files = sorted(p for p in (root / "download").iterdir() if p.is_file())
            if len(files) != counts["ok"]:
                raise AdapterError("invalid_export")
            if "ordered_files" in counts:
                names = counts["ordered_files"]
                if len(names) != len(files) or set(names) != {p.name for p in files}:
                    raise AdapterError("source_order_unavailable")
                files = [root / "download" / name for name in reversed(names)]
                report["ordering"] = "upstream_export_desc"
            if sum(p.stat().st_size for p in files) > 2 * 1024**3:
                raise AdapterError("total_byte_limit")
            hashes = set()
            for index, path in enumerate(files):
                if cancelled():
                    raise AdapterError("cancelled")
                record = dict(
                    source_index=index,
                    source_id_sha256=hashlib.sha256(path.name.encode()).hexdigest(),
                    status="failed",
                )
                try:
                    if path.is_symlink() or path.stat().st_size > 50 * 1024**2:
                        raise AdapterError("resource_size_limit")
                    body = path.read_bytes()
                    media = verify_media(
                        body, allow_gif_trailing=True, allow_image_trailing=True
                    )
                    name = media["sha256"] + EXTENSIONS[media["mime"]]
                    if media["sha256"] not in hashes:
                        path.replace(root / "assets" / name)
                        hashes.add(media["sha256"])
                    record.update(
                        status="recovered", file="assets/" + name, media=media
                    )
                except AdapterError as exc:
                    record["reason"] = str(exc)
                    report["failed_count"] += 1
                report["records"].append(record)
                progress(
                    {"event": "resource", "done": index + 1, "total": counts["total"]}
                )
            for index in range(counts["failed"]):
                report["records"].append(
                    dict(
                        status="failed",
                        source_index=len(files) + index,
                        reason="download_failed",
                    )
                )
                report["failed_count"] += 1
            if cancelled():
                raise AdapterError("cancelled")
            report.update(
                unique_files=len(hashes),
                export_completed=True,
                import_ready=bool(hashes) or not counts["total"],
                status="partial" if report["failed_count"] else "ready",
            )
        except AdapterError as exc:
            report.update(
                status="cancelled" if str(exc) == "cancelled" else "error",
                reason=str(exc),
                import_ready=False,
            )
        except Exception:
            report.update(status="error", reason="upstream_failed", import_ready=False)
        (root / "manifest.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        return report
