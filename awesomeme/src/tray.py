"""系统托盘 - 跨平台托盘图标"""

import logging
import threading
from pathlib import Path

from . import APP_ID, APP_PROFILE, __app_name__

try:
    from PIL import Image as PILImage
    from PIL import ImageDraw

    HAS_PIL = True
except ImportError:
    HAS_PIL = False

logger = logging.getLogger(__name__)

_pystray_available = None

# 托盘菜单标签：默认中文，后端不兼容（渲染/启动异常）时回退英文
_MENU_TEXT = {
    "zh": {"show": "显示/隐藏", "quit": "退出"},
    "en": {"show": "Show/Hide", "quit": "Quit"},
}


def _pystray_ok() -> bool:
    global _pystray_available
    if _pystray_available is None:
        try:
            import pystray  # noqa: F401

            _pystray_available = True
        except Exception:
            _pystray_available = False
    return _pystray_available


def _create_default_icon():
    """读取应用图标，资源不可用时生成中性的图库图标。"""
    if not HAS_PIL:
        return None
    size = 64
    try:
        with PILImage.open(
            Path(__file__).resolve().parent / "resources" / "icon.png"
        ) as icon:
            return icon.convert("RGBA").resize(
                (size, size), PILImage.Resampling.LANCZOS
            )
    except (OSError, ValueError):
        logger.warning("Cannot load application icon, using fallback")
    img = PILImage.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((10, 16, 46, 54), radius=6, fill=(126, 114, 164, 255))
    draw.rounded_rectangle((18, 8, 54, 46), radius=6, fill=(244, 238, 222, 255))
    return img


class TrayManager:
    """系统托盘管理器"""

    def __init__(self, on_show=None, on_quit=None, source_mode=False):
        self._icon = None
        self._thread = None
        self._on_show = on_show
        self._on_quit = on_quit
        self._running = False
        self._source_mode = source_mode
        self._lang = "zh"

    def _build_icon(self, icon_image, lang):
        """构造托盘图标与菜单，中文/英文标签；失败返回 False"""
        import pystray

        labels = _MENU_TEXT[lang]
        display_name = __app_name__ + (" Dev" if APP_PROFILE == "development" else "")
        try:
            menu_items = []
            if self._source_mode or APP_PROFILE == "development":
                menu_items.append(
                    pystray.MenuItem(display_name, lambda: None, enabled=False)
                )
            menu_items += [
                pystray.MenuItem(
                    labels["show"], self._on_show or (lambda: None), default=True
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(labels["quit"], self._on_quit or (lambda: None)),
            ]
            self._icon = pystray.Icon(
                APP_ID, icon_image, display_name, pystray.Menu(*menu_items)
            )
            self._lang = lang
            return True
        except Exception:
            logger.exception("tray menu build failed (%s)", lang)
            return False

    def _run_icon(self):
        """运行托盘主循环；中文标签在当前后端不兼容抛错时回退英文重建（仅一次）"""
        while True:
            try:
                if self._icon is None or not self._running:
                    return
                self._icon.run()
                return
            except Exception:
                if self._lang != "zh" or not self._running:
                    logger.exception("tray menu run failed (%s)", self._lang)
                    return
                logger.warning("tray menu Chinese labels failed, fallback to English")
                icon_image = _create_default_icon()
                if icon_image is None or not self._build_icon(icon_image, "en"):
                    return

    def start(self):
        """启动托盘"""
        if not _pystray_ok():
            logger.error("pystray not installed")
            return False

        icon_image = _create_default_icon()
        if icon_image is None:
            logger.error("Cannot create tray icon (PIL missing)")
            return False

        if not self._build_icon(icon_image, "zh"):
            logger.warning("tray menu Chinese init failed, fallback to English")
            if not self._build_icon(icon_image, "en"):
                return False

        self._running = True
        self._thread = threading.Thread(target=self._run_icon, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None

    def notify(self, title: str, message: str):
        if self._icon and _pystray_ok():
            try:
                self._icon.notify(message, title)
            except Exception:
                pass
