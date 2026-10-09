"""Export a stable server snapshot as verified, content-addressed originals."""

import hashlib
import io
import json
import os
import warnings
from pathlib import Path

from PIL import Image

from ._http import MAX_RESOURCE_BYTES, ResourceHTTPError, fetch_resource
from ._media import inspect_media
from ._resolver import ResourceResolutionError, resolve_origin


class AdapterError(RuntimeError):
    """Fixed error code; never include response bodies or credentials."""


EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def verify_media(
    body, origin=None, *, allow_gif_trailing=False, allow_image_trailing=False
):
    media = inspect_media(
        body,
        allow_gif_trailing=allow_gif_trailing,
        allow_image_trailing=allow_image_trailing,
    )
    if not media["complete"] or media["mime"] not in EXTENSIONS:
        raise AdapterError("invalid_media_container")
    if media["width"] * media["height"] > 40_000_000:
        raise AdapterError("pixel_limit")
    if not 1 <= media["frame_count"] <= 1000:
        raise AdapterError("frame_limit")
    if media["width"] * media["height"] * media["frame_count"] > 200_000_000:
        raise AdapterError("decoded_pixel_limit")
    origin = origin or {}
    for key in ("size", "width", "height"):
        if origin.get(key) not in (None, 0) and origin[key] != media[key]:
            raise AdapterError("origin_metadata_mismatch")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(body)) as picture:
                frames = getattr(picture, "n_frames", 1)
                default_image = bool(getattr(picture, "default_image", False))
                if frames != media["frame_count"] + int(default_image):
                    raise AdapterError("frame_count_mismatch")
                if media["width"] * media["height"] * frames > 200_000_000:
                    raise AdapterError("decoded_pixel_limit")
                for index in range(frames):
                    picture.seek(index)
                    picture.load()
                media.update(decoded_image_count=frames, default_image=default_image)
    except AdapterError:
        raise
    except Exception:
        raise AdapterError("pixel_decode_failed") from None
    media["pixel_validation"] = "all_frames_decoded"
    return media


def _write_new(path, body):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(body)


def _snapshot_key(snapshot):
    return json.dumps(
        {"stickers": snapshot["stickers"], "updateTime": snapshot.get("updateTime")},
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_snapshot(snapshot):
    stickers = snapshot.get("stickers")
    if not isinstance(stickers, list) or len(stickers) > 1000:
        raise AdapterError("invalid_snapshot")
    ids = [item.get("stickerId") for item in stickers]
    if any(not isinstance(value, str) or not value for value in ids):
        raise AdapterError("missing_sticker_id")
    if len(set(ids)) != len(ids):
        raise AdapterError("duplicate_sticker_id")


class FeishuAdapter:
    """Each instance is single-use. backend and downloader are private test seams."""

    def __init__(
        self,
        *,
        backend=None,
        downloader=fetch_resource,
        max_file_bytes=MAX_RESOURCE_BYTES,
        max_total_bytes=1024**3,
    ):
        if not 1 <= max_file_bytes <= MAX_RESOURCE_BYTES or max_total_bytes < 1:
            raise ValueError("invalid_byte_limits")
        self.backend = backend
        self.downloader = downloader
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.used = False

    def export(self, output_dir, *, on_qr, progress=None, cancelled=None):
        """Create a NEW directory. Return a manifest dict, also saved as manifest.json.

        on_qr receives SVG bytes in RAM; it must not persist them. progress receives
        fixed event names/counts only. cancelled is a callable returning a bool.
        Session closes on every exit. Import eligibility covers returned records;
        the server's collection limit remains unverified, including empty lists.
        """
        if self.used:
            raise AdapterError("adapter_already_used")
        self.used = True
        cancelled = cancelled or (lambda: False)
        progress = progress or (lambda event: None)
        directory = Path(output_dir).expanduser().absolute()
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        (directory / "assets").mkdir(mode=0o700)
        report = {
            "schema_version": 1,
            "source": "feishu_favorite_stickers",
            "status": "running",
            "snapshot_consistent": False,
            "collection_completeness": "server_limit_unverified",
            "all_returned_recovered": False,
            "import_ready": False,
            "returned_count": 0,
            "charged_bytes": 0,
            "records": [],
        }
        backend = self.backend
        try:
            if backend is None:
                from .client import FeishuClient

                backend = FeishuClient()
            self._check_cancel(cancelled)
            backend.login(on_qr, cancelled)
            self._check_cancel(cancelled)
            progress({"event": "authenticated"})
            before = backend.snapshot()
            self._check_cancel(cancelled)
            _validate_snapshot(before)
            report["returned_count"] = len(before["stickers"])
            report["at_client_limit"] = len(before["stickers"]) >= 1000
            report["snapshot_sha256"] = hashlib.sha256(
                _snapshot_key(before).encode()
            ).hexdigest()
            config = backend.resource_config(before) if before["stickers"] else {}
            report["profile"] = getattr(backend, "profile_name", "injected_backend")
            for index, sticker in enumerate(before["stickers"]):
                self._check_cancel(cancelled)
                record = {
                    "source_index": index,
                    "source_id_sha256": hashlib.sha256(
                        sticker["stickerId"].encode()
                    ).hexdigest(),
                    "status": "pending",
                }
                report["records"].append(record)
                try:
                    urls = resolve_origin(sticker, config)
                    allowance = min(
                        self.max_file_bytes,
                        self.max_total_bytes - report["charged_bytes"],
                    )
                    if allowance <= 0:
                        raise AdapterError("total_byte_limit")
                    # Reserve the complete allowance if a network read fails part-way.
                    report["charged_bytes"] += allowance
                    body = self.downloader(urls[0], urls, allowance)
                    if len(body) > allowance:
                        raise AdapterError("resource_size_limit")
                    report["charged_bytes"] -= allowance - len(body)
                    self._check_cancel(cancelled)
                    media = verify_media(body, sticker["image"]["origin"])
                    filename = media["sha256"] + EXTENSIONS[media["mime"]]
                    target = directory / "assets" / filename
                    if target.exists():
                        if target.is_symlink() or target.read_bytes() != body:
                            raise AdapterError("asset_collision")
                    else:
                        _write_new(target, body)
                    record.update(
                        status="recovered", file="assets/" + filename, media=media
                    )
                except ResourceResolutionError as error:
                    record.update(
                        status="blocked" if error.blocked else "missing",
                        reason=error.code,
                    )
                except ResourceHTTPError as error:
                    record.update(status="failed", reason=error.code)
                    if error.stop:
                        raise AdapterError(error.code) from None
                except AdapterError as error:
                    record.update(status="failed", reason=str(error))
                    if str(error) in (
                        "cancelled",
                        "total_byte_limit",
                        "resource_size_limit",
                        "asset_collision",
                    ):
                        raise
                progress(
                    {
                        "event": "resource",
                        "done": index + 1,
                        "total": len(before["stickers"]),
                        "status": record["status"],
                    }
                )
            self._check_cancel(cancelled)
            after = backend.snapshot()
            self._check_cancel(cancelled)
            _validate_snapshot(after)
            report["snapshot_consistent"] = _snapshot_key(before) == _snapshot_key(
                after
            )
            report["all_returned_recovered"] = all(
                r["status"] == "recovered" for r in report["records"]
            )
            report["import_ready"] = (
                report["snapshot_consistent"] and report["all_returned_recovered"]
            )
            report["status"] = (
                "ready"
                if report["import_ready"]
                else (
                    "snapshot_changed"
                    if not report["snapshot_consistent"]
                    else "partial"
                )
            )
        except AdapterError as error:
            report.update(
                status="cancelled" if str(error) == "cancelled" else "stopped",
                reason=str(error),
            )
        except ResourceResolutionError as error:
            report.update(status="stopped", reason=error.code)
        except KeyboardInterrupt:
            report.update(status="cancelled", reason="cancelled")
        except Exception:
            report.update(status="stopped", reason="unexpected_error")
        finally:
            if backend is not None:
                backend.close()
            report["unique_files"] = len(
                {r["file"] for r in report["records"] if r["status"] == "recovered"}
            )
            temporary = directory / ".manifest.tmp"
            _write_new(
                temporary, json.dumps(report, ensure_ascii=False, indent=2).encode()
            )
            os.replace(temporary, directory / "manifest.json")
        return report

    @staticmethod
    def _check_cancel(cancelled):
        if cancelled():
            raise AdapterError("cancelled")
