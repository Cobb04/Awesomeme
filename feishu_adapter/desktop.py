"""Background job for the existing OhMyMeme SettingsApi polling interface."""

import base64
import threading
import uuid
from copy import deepcopy
from pathlib import Path

from .adapter import FeishuAdapter
from .ohmymeme import import_into_ohmymeme


class ImportJob:
    """One worker at a time; keep its QR only in RAM until authentication or exit."""

    def __init__(
        self,
        import_files,
        ensure_collection,
        output_root,
        *,
        adapter_factory=FeishuAdapter,
        source="feishu",
    ):
        self._import_files = import_files
        self._ensure_collection = ensure_collection
        self._output_root = Path(output_root)
        self._adapter_factory = adapter_factory
        if source not in ("feishu", "wecom", "wechat"):
            raise ValueError("unsupported_import_source")
        self._source = source
        self._name = {"feishu": "飞书", "wecom": "企业微信", "wechat": "微信"}[source]
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread = None
        self._state = {
            "status": "idle",
            "busy": False,
            "qr_src": "",
            "message": "尚未开始",
        }

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {"ok": False, "error": f"已有{self._name}导入任务正在进行"}
            self._cancel = threading.Event()
            self._state = {
                "status": "running",
                "busy": True,
                "qr_src": "",
                "message": (
                    "正在生成二维码"
                    if self._source == "feishu"
                    else f"正在检查{self._name}"
                ),
                "done": 0,
                "total": 0,
            }
            self._thread = threading.Thread(
                target=self._run, daemon=self._source != "wechat"
            )
            self._thread.start()
        return {"ok": True}

    def state(self):
        with self._lock:
            return deepcopy(self._state)

    def wait(self):
        """Wait for resource cleanup before the host terminates its process."""
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def cancel(self):
        with self._lock:
            if self._state["busy"]:
                self._cancel.set()
                self._state.update(qr_src="", message="正在取消，请等待当前读取结束")
        return {"ok": True}

    def _set(self, **fields):
        with self._lock:
            self._state.update(fields)

    def _show_qr(self, svg):
        with self._lock:
            if not self._cancel.is_set():
                self._state.update(
                    status="waiting_scan",
                    message="请用手机飞书扫码并确认",
                    qr_src="data:image/svg+xml;base64,"
                    + base64.b64encode(svg).decode("ascii"),
                )

    def _progress(self, event):
        if event["event"] == "phase":
            self._set(status="exporting", message=event["message"])
        elif event["event"] in ("reading", "checking_snapshot"):
            self._set(
                status="reading",
                message=(
                    "正在读取企业微信收藏"
                    if event["event"] == "reading"
                    else "正在核对收藏是否发生变化"
                ),
            )
        elif event["event"] == "authenticated":
            self._set(status="downloading", qr_src="", message="授权成功，正在读取收藏")
        elif event["event"] == "resource":
            self._set(
                status="downloading",
                done=event["done"],
                total=event["total"],
                message=f"下载并核验 {event['done']}/{event['total']}",
            )

    def _import_progress(self, done, total, current):
        self._set(done=done, total=total, message=f"入库 {done}/{total}")
        return not self._cancel.is_set()

    def _run(self):
        try:
            directory = self._output_root / uuid.uuid4().hex
            report = self._adapter_factory().export(
                directory,
                on_qr=self._show_qr,
                progress=self._progress,
                cancelled=self._cancel.is_set,
            )
            self._set(
                qr_src="",
                export_dir=str(directory),
                collection_completeness=report["collection_completeness"],
            )
            if self._cancel.is_set() or report["status"] == "cancelled":
                self._set(status="cancelled", message="已取消")
                return
            if not report["import_ready"]:
                message = "导出未全部通过核验，未自动入库"
                if self._source == "wecom":
                    from wecom_adapter.client import error_message

                    message = error_message(report.get("reason"))
                elif self._source == "wechat":
                    from wechat_adapter.client import error_message

                    message = error_message(report.get("reason"))
                self._set(
                    status="error",
                    message=message,
                    reason=report.get("reason", report["status"]),
                    failed_count=report.get("failed_count", 0),
                )
                return
            self._set(
                status="importing",
                message="正在入库",
                done=0,
                total=report["unique_files"],
            )
            result = import_into_ohmymeme(
                directory,
                self._import_files,
                ensure_collection=self._ensure_collection,
                progress_cb=self._import_progress,
                cancelled=self._cancel.is_set,
                source=self._source,
            )
            result["failed_downloads"] = report.get("failed_count", 0)
            result["metadata_warnings"] = report.get("warning_count", 0)
            result["source_count"] = report["returned_count"]
            self._set(result=result)
            if self._cancel.is_set() or result.get("cancelled"):
                self._set(status="cancelled", message="已取消；已入库的图片保留")
            elif (
                result["unaccounted"]
                or result["rejected"]
                or result.get("collection_status") == "failed_after_import"
                or result["failed_downloads"]
            ):
                self._set(status="partial", message="部分完成，请查看结果计数")
            else:
                self._set(
                    status="done",
                    message=(
                        (
                            "未导出表情"
                            if self._source == "wechat"
                            else "当前没有收藏表情"
                        )
                        if not report["returned_count"]
                        else f"完成：新增 {len(result['ids'])}，重复 {result['skipped_dup']}"
                    ),
                )
        except Exception:
            self._set(status="error", message="导入失败", reason="desktop_job_failed")
        finally:
            self._set(qr_src="", busy=False)
