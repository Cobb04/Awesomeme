"""KeyboardShortcuts 同进程桥，注册失败不回退到全局键盘监听。"""

import ctypes
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


class MacHotkey:
    # 持有原生桥与通知订阅，避免回调对象被回收。
    def __init__(self):
        self._bridge = None
        self._observer = None
        self._failure_observer = None
        self._library = None
        self._center = None

    # 当前首版固定 ⌘E；不把未支持的设置误报为成功。
    def register(self, hotkey, callback):
        import objc
        from Foundation import NSNotificationCenter, NSOperationQueue, NSThread

        if not NSThread.isMainThread():
            raise RuntimeError("热键注册必须在主线程")
        self.unregister()
        if hotkey.lower().replace(" ", "") not in ("cmd+e", "command+e", "super+e"):
            return False
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
        path = base / "native/libAwesomemeHotkey.dylib"
        if not getattr(sys, "frozen", False):
            path = base / "build/native/libAwesomemeHotkey.dylib"
        self._library = ctypes.CDLL(str(path))
        self._bridge = objc.lookUpClass("AwesomemeHotkey").alloc().init()
        self._center = NSNotificationCenter.defaultCenter()

        # 上游在按键释放后回调，避免修饰键仍按住时进入粘贴链路。
        def pressed(_):
            try:
                callback()
            except Exception:
                logger.exception("Awesomeme 热键回调失败")

        self._observer = self._center.addObserverForName_object_queue_usingBlock_(
            "AwesomemeHotkeyPressed",
            self._bridge,
            NSOperationQueue.mainQueue(),
            pressed,
        )
        self._failure_observer = (
            self._center.addObserverForName_object_queue_usingBlock_(
                "AwesomemeHotkeyFailed",
                self._bridge,
                NSOperationQueue.mainQueue(),
                lambda _: logger.error("⌘E 注册已失效，请从应用窗口继续操作"),
            )
        )
        # Carbon kVK_ANSI_E=14，cmdKey=256；不监听其他按键。
        return bool(self._bridge.registerKeyCode_modifiers_(14, 256))

    # 注销原生热键及通知；必须与注册一样在主线程执行。
    def unregister(self):
        if self._bridge:
            self._bridge.unregister()
            self._bridge = None
        if self._center:
            for observer in (self._observer, self._failure_observer):
                if observer:
                    self._center.removeObserver_(observer)
        self._observer = self._failure_observer = None
