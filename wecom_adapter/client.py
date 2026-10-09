"""Version-gated access to the running client's favorites service."""

import hashlib
import json
import os
import platform
import plistlib
import selectors
import signal
import subprocess
import threading
import time
from pathlib import Path

from feishu_adapter.adapter import AdapterError

APP = Path("/Applications/企业微信.app")
EXE = APP / "Contents/MacOS/企业微信"
SHA256 = "6a8f22343c3b3098eed867b755ec5b523d3fda415ae9724e5000d0560847c7b8"
INDEX_NAME = "a52855cfa612a285ea965078dcac27ef.db"
_LOCK = threading.Lock()
_HELPER = None
ERRORS = {
    "unsupported_platform": "目前只支持 Apple Silicon Mac。",
    "client_missing": "请先安装并登录企业微信。",
    "unsupported_version": "这个企业微信版本尚未适配。当前支持 5.0.11（70742）。",
    "client_not_ready": "未找到已登录的企业微信主进程，请登录后重试。",
    "multiple_clients": "发现多个企业微信账号进程。请只保留要导入的账号，再重试。",
    "developer_tools_missing": "缺少 Apple 命令行工具。安装后重新打开 Awesomeme。",
    "attach_rejected": "系统未允许读取企业微信。若系统显示开发者工具授权，请完成授权后重试。",
    "collection_limit": "收藏列表超过本版读取限制（1000 条或 16 MiB），未导入。",
    "collection_unavailable": "企业微信收藏列表暂不可读，请确认已登录后重试。",
    "snapshot_changed": "导入期间收藏发生变化，本次未入库，请重试。",
    "client_changed": "企业微信进程或账号已变化，本次未入库，请重试。",
    "reader_busy": "上一次读取仍在退出，请稍后重试。",
    "detach_unconfirmed": "读取未正常结束，请先检查企业微信是否能正常操作。",
    "reader_timeout": "读取超时，本次未入库，请稍后重试。",
    "expression_timeout": "读取收藏超时，本次未入库，请稍后重试。",
}


def error_message(code):
    return ERRORS.get(code, "读取或下载未完成。请查看失败计数后重试。")


def preflight():
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise AdapterError("unsupported_platform")
    try:
        info = plistlib.loads((APP / "Contents/Info.plist").read_bytes())
        sha = hashlib.sha256()
        with EXE.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                sha.update(block)
        digest = sha.hexdigest()
    except OSError:
        raise AdapterError("client_missing") from None
    if (
        info.get("CFBundleShortVersionString"),
        info.get("CFBundleVersion"),
        digest,
    ) != ("5.0.11", "70742", SHA256):
        raise AdapterError("unsupported_version")
    try:
        found = subprocess.run(
            ["/usr/bin/xcrun", "--find", "lldb"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        lldb = found.stdout.strip()
        if not lldb or not Path(lldb).is_file():
            raise OSError()
    except (OSError, subprocess.SubprocessError):
        raise AdapterError("developer_tools_missing") from None
    processes = subprocess.run(
        ["/bin/ps", "-axo", "pid=,comm="],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    candidates = []
    for line in processes.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or parts[1] != str(EXE):
            continue
        files = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-a", "-p", parts[0], "-Fn"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        indices = {
            line[1:]
            for line in files.stdout.splitlines()
            if line.startswith("n") and line.endswith("/Emotion/" + INDEX_NAME)
        }
        if indices:
            if len(indices) != 1:
                raise AdapterError("multiple_clients")
            # Hash the account path; never persist the path itself.
            account = hashlib.sha256(next(iter(indices)).encode()).hexdigest()
            candidates.append((int(parts[0]), account))
    if len(candidates) != 1:
        raise AdapterError("multiple_clients" if candidates else "client_not_ready")
    return lldb, candidates[0]


def reader_command(lldb, pid, writer):
    script = Path(__file__).with_name("wecom_reader.py")
    quoted = str(script).replace("\\", "\\\\").replace('"', '\\"')
    return [
        lldb,
        "--no-lldbinit",
        "--batch",
        "-o",
        "settings set interpreter.prompt-on-quit false",
        "-o",
        "settings set target.detach-on-error true",
        "-o",
        f'command script import "{quoted}"',
        "-o",
        f"wecom-export {pid} {writer}",
    ]


def read_snapshot(lldb, pid, cancelled):
    global _HELPER
    if not _LOCK.acquire(blocking=False):
        raise AdapterError("reader_busy")
    reader = writer = None
    try:
        if _HELPER is not None and _HELPER.poll() is None:
            raise AdapterError("reader_busy")
        if cancelled():
            raise AdapterError("cancelled")
        reader, writer = os.pipe()
        _HELPER = subprocess.Popen(
            reader_command(lldb, pid, writer),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(writer,),
            start_new_session=True,
        )
        os.close(writer)
        writer = None
        body = bytearray()
        deadline = time.monotonic() + 45
        with selectors.DefaultSelector() as selector:
            selector.register(reader, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Interrupt the debugger so its finally block detaches. Never kill the client.
                    _HELPER.send_signal(signal.SIGINT)
                    try:
                        _HELPER.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        raise AdapterError("detach_unconfirmed") from None
                    raise AdapterError("reader_timeout")
                if not selector.select(min(remaining, 0.2)):
                    continue
                chunk = os.read(reader, 65536)
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > 17 * 1024 * 1024:
                    raise AdapterError("collection_limit")
        try:
            _HELPER.wait(timeout=10)
        except subprocess.TimeoutExpired:
            raise AdapterError("detach_unconfirmed") from None
        if cancelled():
            raise AdapterError("cancelled")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            raise AdapterError("reader_failed") from None
        if "error" in payload:
            code = payload["error"]
            raise AdapterError(code if code in ERRORS else "reader_failed")
        return payload["rows"]
    finally:
        for fd in (reader, writer):
            if fd is not None:
                os.close(fd)
        _LOCK.release()


class WeComClient:
    """Read only the approved favorites getter for the verified binary profile."""

    profile = "macos-arm64-5.0.11-70742"

    def __init__(self):
        self.identity = None

    def snapshot(self, cancelled):
        lldb, identity = preflight()
        if self.identity is not None and identity != self.identity:
            raise AdapterError("client_changed")
        self.identity = identity
        return read_snapshot(lldb, identity[0], cancelled)
