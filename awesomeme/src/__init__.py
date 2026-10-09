"""Awesomeme — 基于 OhMyMeme 的本地表情库。"""

import os
import plistlib
import sys
from pathlib import Path

__version__ = "0.1.0"
__app_name__ = "Awesomeme"
__upstream_version__ = "0.6.5"


# 打包身份来自包内元数据；源码默认使用开发身份。
def _profile():
    if getattr(sys, "frozen", False):
        info = Path(sys.executable).parent.parent / "Info.plist"
        with info.open("rb") as stream:
            value = plistlib.load(stream).get("AwesomemeProfile", "development")
    else:
        value = os.environ.get("AWESOMEME_PROFILE", "development")
    if value not in ("development", "production"):
        raise ValueError("AWESOMEME_PROFILE 必须是 development 或 production")
    return value


APP_PROFILE = _profile()
APP_ID = "local.awesomeme.desktop" + (".dev" if APP_PROFILE == "development" else "")
STORAGE_NAME = __app_name__ + (" Development" if APP_PROFILE == "development" else "")
UPDATE_ENABLED = False
UPDATE_DISABLED_REASON = (
    "Awesomeme 自动更新尚未接入；此版本不会自动检查、下载或安装更新。"
)


# 仅开发身份允许隔离测试根目录，不接受相对路径。
def test_root():
    value = os.environ.get("AWESOMEME_TEST_ROOT", "")
    if not value:
        return None
    if APP_PROFILE != "development" or not Path(value).is_absolute():
        raise ValueError("AWESOMEME_TEST_ROOT 仅供开发身份使用，且必须是绝对路径")
    return Path(value).resolve()
