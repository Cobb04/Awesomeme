"""共享本地图库的快捷面板，不发送消息。"""

import hashlib
import logging
import secrets
import threading
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger(__name__)


# 只替换 picker 的工厂与 show；管理窗口继续使用原 NSWindow。
def install_panel_backend():
    from importlib.metadata import version

    import AppKit
    import objc
    from PyObjCTools import AppHelper
    from webview.platforms.cocoa import BrowserView

    if version("pywebview") != "6.2.1":
        raise RuntimeError("NSPanel 扩展仅验证 pywebview 6.2.1")
    if getattr(BrowserView, "_awesomeme_panel_installed", False):
        return

    class AwesomemePanel(AppKit.NSPanel):
        # nonactivating 必须在创建时设置，不能在普通 NSWindow 上事后追加。
        def initWithContentRect_styleMask_backing_defer_(
            self, rect, mask, backing, defer
        ):
            self = objc.super(
                AwesomemePanel, self
            ).initWithContentRect_styleMask_backing_defer_(
                rect, mask | AppKit.NSWindowStyleMaskNonactivatingPanel, backing, defer
            )
            if self:
                self.setFloatingPanel_(True)
                self.setHidesOnDeactivate_(False)
                self.setLevel_(AppKit.NSFloatingWindowLevel)
                self.setCollectionBehavior_(
                    AppKit.NSWindowCollectionBehaviorMoveToActiveSpace
                    | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                )
            return self

        # 搜索框可以接收输入，但不把整个应用提升为前台应用。
        def canBecomeKeyWindow(self):
            return True

        # 面板不成为管理窗口。
        def canBecomeMainWindow(self):
            return False

    original_init, original_show = BrowserView.__init__, BrowserView.show

    # pywebview 的 Cocoa 窗口构造均在主线程；临时工厂在构造结束立即恢复。
    def create(view, window):
        if not getattr(window, "_awesomeme_picker", False):
            return original_init(view, window)
        original_host = BrowserView.WindowHost
        BrowserView.WindowHost = AwesomemePanel
        try:
            original_init(view, window)
        finally:
            BrowserView.WindowHost = original_host

    # 不调用上游 activateIgnoringOtherApps，保留原应用为 active application。
    def show(view):
        if not getattr(view.pywebview_window, "_awesomeme_picker", False):
            return original_show(view)
        AppHelper.callAfter(view.window.makeKeyAndOrderFront_, None)

    BrowserView.__init__ = create
    BrowserView.show = show
    BrowserView._awesomeme_panel_installed = True


class PickerLibrary:
    # 复用管理窗口的数据库与文件定位，不接受前端传入路径。
    def __init__(self, host):
        self.host = host
        self.db = host._api._db

    # 每次打开重读分组；管理窗口的新增和修改会在下次查询生效。
    def groups(self):
        return [
            {"id": cid, "name": name} for cid, name, _, _ in self.db.get_collections()
        ]

    # 只查询本地图库，保留既有来源排序、名称和手动标签搜索。
    def page(self, keyword, group, offset):
        if not isinstance(keyword, str) or len(keyword) > 200:
            raise ValueError("invalid_query")
        if type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise ValueError("invalid_offset")
        if group is not None and (
            type(group) is not int or (group <= 0 and group not in (-3, -2))
        ):
            raise ValueError("invalid_group")
        if group == -3:
            if keyword:
                raise ValueError("search_recent_requires_all")
            rows = self.db.get_recent(48, offset)
            total = self.db.count_recent()
        else:
            ids = None
            if group is not None and group > 0:
                if group not in {g["id"] for g in self.groups()}:
                    return {"items": [], "total": 0}
                ids = self.host._api._get_collection_ids_recursive(group)
            args = dict(keyword=keyword, collection_id=ids, favorite_only=group == -2)
            rows = self.db.search(**args, offset=offset, limit=48)
            total = self.db.count(**args)
        items = []
        for row in rows:
            filename = row["filename"]
            animated = Path(filename).suffix.lower() in (".gif", ".webp", ".apng")
            preview = (
                f"/api/original/{row['id']}/{quote(filename, safe='')}"
                if animated
                else f"/api/thumb/{row['file_hash']}"
            )
            items.append(
                {
                    "id": row["id"],
                    "name": row["original_name"] or filename,
                    "preview": preview,
                    "hash": row["file_hash"],
                }
            )
        return {"items": items, "total": total}

    # 再次核对当前记录、文件范围与内容；已删除或替换的图片不能提交。
    def prepare(self, meme_id, expected_hash, target_bundle=None):
        from .clipboard_util import _prepare_feishu_image, _prepare_macos_image

        row = self.db.get_by_id(meme_id)
        if row is None or row["file_hash"] != expected_hash:
            raise ValueError("image_changed")
        found = self.host._api._find_meme_file(row["filename"])
        if not found:
            raise ValueError("image_missing")
        path = Path(found).resolve()
        if (
            not path.is_relative_to(self.host._cfg.cache_dir.resolve())
            or not path.is_file()
        ):
            raise ValueError("image_missing")
        prepared = _prepare_macos_image(str(path))
        if hashlib.sha256(prepared["payload"]).hexdigest() != expected_hash:
            raise ValueError("image_changed")
        if target_bundle == "com.electron.lark":
            prepared = _prepare_feishu_image(prepared)
        return prepared


class PickerApi:
    # 仅暴露搜索和选图；不向 JS 提供聊天对象、原图字节或本机路径。
    def __init__(self, controller):
        self._controller = controller

    # JS 取消在主线程处理。
    def cancel(self, session):
        from PyObjCTools import AppHelper

        AppHelper.callAfter(self._controller.cancel, session)

    def ready(self):
        self._controller.ready = True
        return {"page_size": 48}

    # 同一会话只接纳最新查询，旧查询不得扩大可选图片集合。
    def search(self, session, request, keyword="", group=None, offset=0):
        c = self._controller
        with c._query_lock:
            if (
                not session
                or session != c.session
                or c.committing
                or type(request) is not int
                or request <= c._request
            ):
                return {"status": "stale"}
            c._request = request
            c._choices = {}
        try:
            page = c.library.page(keyword, group, offset)
            groups = c.library.groups()
        except Exception:
            logger.warning("小面板图库查询失败")
            return {"status": "error"}
        with c._query_lock:
            if session != c.session or request != c._request or c.committing:
                return {"status": "stale"}
            c._choices = {item["id"]: item["hash"] for item in page["items"]}
        return {"status": "ok", **page, "groups": groups}

    # 提交只接受当前页的数据库 ID。
    def choose(self, session, request, meme_id):
        from PyObjCTools import AppHelper

        c = self._controller
        with c._query_lock:
            if (
                not session
                or session != c.session
                or c.committing
                or type(request) is not int
                or request != c._request
                or type(meme_id) is not int
                or meme_id not in c._choices
            ):
                return {"status": "invalid_selection"}
            c.committing = True
            expected_hash = c._choices[meme_id]
        AppHelper.callAfter(c.choose, session, meme_id, expected_hash)
        return {"status": "preparing"}

    # 管理操作在原管理窗口继续，离开面板即取消本轮插入。
    def manage(self, session):
        from PyObjCTools import AppHelper

        AppHelper.callAfter(self._controller.manage, session)


class PickerController:
    # 原管理窗口与小面板共享数据库和本机缩略图服务。
    def __init__(self, host):
        import webview

        self.session = None
        self._generation = 0
        self._closing_for_commit = False
        self.ready = False
        self.committing = False
        self._observer = None
        self._target = None
        self._hud = None
        self.library = PickerLibrary(host)
        self._query_lock = threading.RLock()
        self._request = 0
        self._choices = {}
        self._selected_id = None
        self.window = webview.create_window(
            "Awesomeme 表情",
            f"http://127.0.0.1:{host._port}/picker/",
            js_api=PickerApi(self),
            width=400,
            height=460,
            min_size=(320, 340),
            hidden=True,
            frameless=True,
            easy_drag=False,
            resizable=False,
        )
        self.window._awesomeme_picker = True

    # 每次显示绑定新的会话编号，迟到的失焦通知不能取消新会话。
    def _attach(self, session):
        from Foundation import NSNotificationCenter, NSOperationQueue

        center = NSNotificationCenter.defaultCenter()
        if self._observer:
            center.removeObserver_(self._observer)
        self._observer = center.addObserverForName_object_queue_usingBlock_(
            "NSWindowDidResignKeyNotification",
            self.window.native,
            NSOperationQueue.mainQueue(),
            lambda _: self.cancel(session, reason="resign"),
        )

    # 热键在主线程调用，面板接收焦点之前捕获目标身份。
    def toggle(self):
        import AppKit

        if self.session:
            self.cancel(self.session)
            return
        if not self.ready:
            logger.warning("小面板尚未准备好")
            return
        self.session = secrets.token_urlsafe(24)
        self._generation += 1
        self.committing = False
        self._closing_for_commit = False
        self._request = 0
        self._choices = {}
        self._selected_id = None
        self._attach(self.session)
        from .macos_insertion import capture_target, input_anchor

        try:
            self._target = capture_target()
        except Exception as exc:
            logger.error("捕获粘贴目标失败：%s", type(exc).__name__)
            self._target = {"reason": "target_capture_error"}
        try:
            point = input_anchor(self._target) or AppKit.NSEvent.mouseLocation()
        except Exception as exc:
            logger.warning("定位小面板失败：%s", type(exc).__name__)
            point = AppKit.NSEvent.mouseLocation()
        screen = next(
            (
                s
                for s in AppKit.NSScreen.screens()
                if AppKit.NSPointInRect(point, s.frame())
            ),
            AppKit.NSScreen.mainScreen(),
        )
        bounds = screen.visibleFrame()
        frame = self.window.native.frame()
        x = max(
            bounds.origin.x,
            min(point.x, bounds.origin.x + bounds.size.width - frame.size.width),
        )
        y = max(
            bounds.origin.y,
            min(
                point.y + 8,
                bounds.origin.y + bounds.size.height - frame.size.height,
            ),
        )
        self.window.native.setFrameOrigin_(AppKit.NSMakePoint(x, y))
        # evaluate_js 会等待 WKWebView 回调，不能阻塞 AppKit 主线程。
        import json
        import threading

        threading.Thread(
            target=self.window.evaluate_js,
            args=(
                f"window.openSession({json.dumps(self.session)}, {self._generation})",
            ),
            daemon=True,
        ).start()
        self.window.show()

    # 过期关闭和提交引起的失焦均不能取消新一轮选择。
    def cancel(self, session, reason="user"):
        if session != self.session or (self._closing_for_commit and reason == "resign"):
            return
        self.session = None
        self.committing = False
        self._target = None
        self.window.hide()

    # 显示管理窗口时不把后续选择投回旧聊天。
    def manage(self, session):
        if session != self.session:
            return
        self.cancel(session)
        threading.Thread(target=self.library.host.show, daemon=True).start()

    # 锁定当前选择，在后台准备；此时 Esc 和外部切换仍可取消。
    def choose(self, session, meme_id, expected_hash):
        if session != self.session or not self.committing:
            return
        import threading

        import AppKit
        from PyObjCTools import AppHelper

        self._selected_id = meme_id
        generation = AppKit.NSPasteboard.generalPasteboard().changeCount()
        target_bundle = (self._target or {}).get("bundle_id")

        def prepare():
            try:
                prepared = self.library.prepare(meme_id, expected_hash, target_bundle)
            except Exception:
                prepared = None
            AppHelper.callAfter(self._commit, session, generation, prepared)

        threading.Thread(target=prepare, daemon=True).start()

    # 再核对会话与剪贴板代次，已取消的后台任务不能覆盖剪贴板。
    def _commit(self, session, generation, prepared):
        if session != self.session or not self.committing:
            return
        import AppKit
        from PyObjCTools import AppHelper

        from .clipboard_util import _commit_macos_image

        try:
            ok = prepared is not None and _commit_macos_image(prepared, generation)
        except Exception:
            ok = False
        if not ok:
            self.committing = False
            self.cancel(session)
            self._feedback({"status": "copy_failed", "reason": "write_failed"})
            return
        generation = AppKit.NSPasteboard.generalPasteboard().changeCount()
        try:
            if self.library.host._cfg.get("record_recent_use", True):
                self.library.db.record_use(self._selected_id)
        except Exception:
            logger.warning("最近使用记录未能保存")
        self._media_note = prepared.get("media_note")
        self._closing_for_commit = True
        self.window.native.orderOut_(None)
        AppHelper.callLater(0.12, self._finish, session, generation)

    # 只尝试一次；不因未知接收结果自动再次粘贴。
    def _finish(self, session, generation):
        if self.session != session or not self.committing:
            return
        from .macos_insertion import paste_once

        try:
            result = paste_once(self._target, generation)
        except Exception:
            logger.exception("粘贴请求失败；不自动重试")
            result = {"status": "copied_only", "reason": "paste_error"}
        self.session = None
        self.committing = False
        self._target = None
        if getattr(self, "_media_note", None):
            result["media_note"] = self._media_note
        self._feedback(result)

    # 非激活提示不会重新抢走输入焦点；状态中不保存目标窗口或聊天内容。
    def _feedback(self, result):
        import json

        import AppKit
        from PyObjCTools import AppHelper

        from .config import get_config

        messages = {
            "copied_only": "未能自动插入。已复制，请按 ⌘V",
            "paste_requested": "已请求插入；未出现时请按 ⌘V",
            "clipboard_changed": "剪贴板已变化，本次未插入",
            "copy_failed": "复制失败，请重试",
        }
        message = messages[result["status"]]
        if result.get("reason") == "accessibility_required":
            message = "已复制，请按 ⌘V · 自动插入需辅助功能权限"
        if (
            result.get("media_note") == "animation_original"
            and result.get("reason") != "accessibility_required"
        ):
            if result["status"] == "paste_requested":
                message = "动图保留原尺寸；已请求插入"
            elif result["status"] == "copied_only":
                message = "动图保留原尺寸；已复制，请按 ⌘V"
        if self._hud:
            self._hud.orderOut_(None)
        frame = self.window.native.frame()
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(frame.origin.x, frame.origin.y, 390, 42),
            AppKit.NSWindowStyleMaskBorderless
            | AppKit.NSWindowStyleMaskNonactivatingPanel,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        panel.setReleasedWhenClosed_(False)
        panel.setHidesOnDeactivate_(False)
        panel.setLevel_(AppKit.NSFloatingWindowLevel)
        label = AppKit.NSTextField.labelWithString_(message)
        label.setFrame_(AppKit.NSMakeRect(12, 10, 366, 24))
        panel.contentView().addSubview_(label)
        panel.orderFrontRegardless()
        self._hud = panel
        AppHelper.callLater(4, panel.orderOut_, None)
        (get_config().data_dir / "picker-probe-result.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("小面板插入结果：%s", result["status"])
