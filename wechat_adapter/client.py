import hashlib
import json
import os
import platform
import plistlib
import shutil
import signal
import subprocess
import tarfile
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from feishu_adapter.adapter import AdapterError

BINARY_SHA256 = "fef101e48df805968715fdb7c9f954de9ac039b598bc0f5f6fd93f438b6b3996"
ARCHIVE_SHA256 = "d36cbcc546e88f3c112172688efb7845da13cc31761b5a33e2e877628f9cbb4b"


# Reuse the pinned helper's URL order and filename rule; never retain URLs.
def source_ordered_files(download, total):
    if not total:
        return []
    metadata = download / "导出信息" / "emoticon_urls.txt"
    if (
        metadata.is_symlink()
        or not metadata.is_file()
        or metadata.stat().st_size > 20 * 1024**2
    ):
        raise AdapterError("source_order_unavailable")
    urls = [line.strip() for line in metadata.read_text().splitlines() if line.strip()]
    if len(urls) != total:
        raise AdapterError("source_order_unavailable")
    files = {}
    for path in download.iterdir():
        if path.is_file():
            if path.is_symlink() or path.stem in files:
                raise AdapterError("source_order_unavailable")
            files[path.stem] = path.name
    ordered = []
    seen = set()
    for index, url in enumerate(urls):
        try:
            key = next(
                (
                    v
                    for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True)
                    if k == "m"
                ),
                "",
            )
        except ValueError:
            raise AdapterError("source_order_unavailable") from None
        key = key or f"{index + 1:06}"
        if key in files:
            if key in seen:
                raise AdapterError("source_order_unavailable")
            seen.add(key)
            ordered.append(files[key])
    if len(ordered) != len(files):
        raise AdapterError("source_order_unavailable")
    return ordered


# Translate only known codes; raw helper output can contain credentials.
def error_message(reason):
    return {
        "unsupported_platform": "微信 Mac 导入目前支持 Apple Silicon Mac",
        "unsupported_version": "此微信版本尚未适配；当前已验证 4.1.15（270102）",
        "account_count": "需要且只能有一个本地微信账号；未开始读取",
        "helper_invalid": "微信导出工具缺失或校验失败；请在源码仓库运行 scripts/fetch_wechat_helper.py 后重试",
        "original_signature": "微信安装包签名异常，请使用官方微信",
        "upstream_cache_exists": "原导出工具已有缓存或任务，请先处理后重试；未覆盖原文件",
        "source_order_unavailable": "未能确认微信收藏顺序，本次未导入",
        "wechat_quit_failed": "微信未能正常退出，请退出微信后重试",
        "key_timeout": "等待登录或表情库读取超时，请完成手机确认后重试",
        "upstream_failed": "原版微信工具导出失败，本轮密钥和日志已清理",
        "cleanup_failed": "清理未完成，已停止入库；请检查本机临时微信与缓存",
        "restore_failed": "已结束导出，但无法重新打开微信；请手动打开微信后重试",
        "invalid_export": "导出清单或图片数量异常，未入库",
        "total_byte_limit": "导出超过本次容量限制，未入库",
    }.get(reason, "微信导入失败，未将未核验图片加入图库")


# Use the unmodified, pinned upstream executable shipped with the adapter.
class UpstreamClient:
    def __init__(self):
        self.archive = Path(__file__).with_name("bin") / "wxemoticon.tar.gz"
        self.binary_bytes = None
        self.user_dir = Path.home()
        self.app = Path("/Applications/WeChat.app")
        self.cache = (
            self.user_dir
            / "Library/Containers/com.tencent.xinWeChat/Data/Documents/export-wechat-emoji"
        )
        self.copy = self.user_dir / "Library/Caches/export-wechat-emoji/WeChat.app"
        self.data = (
            self.user_dir
            / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
        )

    # Preflight performs no credential read and never overwrites prior caches.
    def preflight(self):
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise AdapterError("unsupported_platform")
        self.binary_bytes = self._load_binary()
        try:
            info = plistlib.loads((self.app / "Contents/Info.plist").read_bytes())
        except (OSError, ValueError):
            raise AdapterError("unsupported_version") from None
        if (
            info.get("CFBundleShortVersionString") != "4.1.15"
            or info.get("CFBundleVersion") != "270102"
        ):
            raise AdapterError("unsupported_version")
        signed = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(self.app)],
            capture_output=True,
            timeout=30,
        )
        details = subprocess.run(
            ["/usr/bin/codesign", "-dv", str(self.app)], capture_output=True, timeout=10
        )
        if (
            signed.returncode
            or details.returncode
            or b"Signature=adhoc" in details.stderr
        ):
            raise AdapterError("original_signature")
        accounts = (
            [
                p
                for p in self.data.iterdir()
                if not p.is_symlink()
                and (p / "db_storage/emoticon/emoticon.db").is_file()
            ]
            if self.data.is_dir()
            else []
        )
        if len(accounts) != 1:
            raise AdapterError("account_count")
        if (
            self.cache.exists()
            or self.cache.is_symlink()
            or self.copy.exists()
            or self.copy.is_symlink()
        ):
            raise AdapterError("upstream_cache_exists")
        return accounts[0].name

    # Keep the release archive intact so app signing cannot modify the helper.
    def _load_binary(self):
        if (
            not self.archive.is_file()
            or hashlib.sha256(self.archive.read_bytes()).hexdigest() != ARCHIVE_SHA256
        ):
            raise AdapterError("helper_invalid")
        try:
            with tarfile.open(self.archive, "r:gz") as archive:
                member = archive.getmember("wxemoticon")
                if not member.isfile() or member.size > 12 * 1024**2:
                    raise AdapterError("helper_invalid")
                with archive.extractfile(member) as stream:
                    body = stream.read()
            if hashlib.sha256(body).hexdigest() != BINARY_SHA256:
                raise AdapterError("helper_invalid")
            return body
        except (OSError, KeyError, tarfile.TarError):
            raise AdapterError("helper_invalid") from None

    # A separate process group contains the CLI, helper and temporary WeChat.
    def _stop(self, process):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            if sig == signal.SIGTERM:
                time.sleep(1)
        process.wait(timeout=10)

    # Capture only an explicit numeric result; stderr stays in a private directory.
    def export_to(self, directory, progress, cancelled):
        account = self.preflight()
        if cancelled():
            raise AdapterError("cancelled")
        try:
            self.cache.mkdir(mode=0o700)
        except FileExistsError:
            raise AdapterError("upstream_cache_exists") from None
        private = directory / "private"
        temporary = private / "tmp"
        process = None
        quit_attempted = False
        try:
            private.mkdir(mode=0o700)
            temporary.mkdir(mode=0o700)
            binary = private / "wxemoticon"
            binary.write_bytes(self.binary_bytes)
            binary.chmod(0o700)
            progress({"event": "phase", "message": "正在正常退出微信，准备临时副本"})
            quit_attempted = True
            quit_result = subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-e",
                    'tell application "/Applications/WeChat.app" to quit',
                ],
                capture_output=True,
                timeout=30,
            )
            if quit_result.returncode:
                raise AdapterError("wechat_quit_failed")
            if cancelled():
                raise AdapterError("cancelled")
            env = dict(os.environ, TMPDIR=str(temporary))
            with (
                (private / "stdout").open("wb") as out,
                (private / "stderr").open("wb") as err,
            ):
                process = subprocess.Popen(
                    [
                        str(binary),
                        "--no-interactive",
                        "export",
                        "--wxid",
                        account,
                        "--out-dir",
                        str(directory / "download"),
                        "--flat",
                        "--timeout",
                        "600",
                        "--json",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    env=env,
                    start_new_session=True,
                )
                started = time.monotonic()
                previous = None
                while process.poll() is None:
                    if cancelled():
                        raise AdapterError("cancelled")
                    if time.monotonic() - started > 1500:
                        raise AdapterError("key_timeout")
                    downloads = directory / "download"
                    if downloads.is_dir():
                        files = [p for p in downloads.iterdir() if p.is_file()]
                        if (
                            len(files) > 10000
                            or sum(p.stat().st_size for p in files) > 2 * 1024**3
                        ):
                            raise AdapterError("total_byte_limit")
                    if (private / "stderr").stat().st_size > 10 * 1024**2:
                        raise AdapterError("upstream_failed")
                    log = (private / "stderr").read_text(errors="replace")
                    phase = (
                        "正在下载微信表情"
                        if "已生成 URL 列表" in log
                        else (
                            "请在微信窗口进入微信，并在手机确认；若一直等待，可打开一次表情面板"
                            if "即将启动微信副本" in log
                            else "正在准备临时微信，请稍候"
                        )
                    )
                    if phase != previous:
                        progress({"event": "phase", "message": phase})
                        previous = phase
                    time.sleep(0.3)
            if process.returncode:
                log = (private / "stderr").read_text(errors="replace")
                raise AdapterError(
                    "key_timeout" if "等待 key 超时" in log else "upstream_failed"
                )
            raw = json.loads((private / "stdout").read_text())
            counts = {k: raw.get(k) for k in ("total", "ok", "failed", "skipped")}
            if (
                any(type(v) is not int or v < 0 for v in counts.values())
                or counts["total"]
                != counts["ok"] + counts["failed"] + counts["skipped"]
                or counts["skipped"]
                or counts["total"] > 10000
            ):
                raise AdapterError("invalid_export")
            if counts["total"]:
                counts["ordered_files"] = source_ordered_files(
                    directory / "download", counts["total"]
                )
            return counts
        finally:
            self.binary_bytes = None
            cleanup_ok = True
            try:
                if process is not None:
                    self._stop(process)
            except Exception:
                cleanup_ok = False
            if cleanup_ok:
                for path in (
                    self.cache,
                    self.copy,
                    private,
                    directory / "download/导出信息",
                ):
                    try:
                        if path.exists():
                            shutil.rmtree(path)
                    except OSError:
                        cleanup_ok = False
            if quit_attempted and cleanup_ok:
                try:
                    restored = subprocess.run(
                        ["/usr/bin/open", str(self.app)],
                        capture_output=True,
                        timeout=15,
                    )
                    restore_ok = restored.returncode == 0
                except Exception:
                    restore_ok = False
            else:
                restore_ok = True
            if not cleanup_ok:
                raise AdapterError("cleanup_failed")
            if not restore_ok:
                raise AdapterError("restore_failed")
