"""独立身份、持久存储和更新禁用的行为回归。"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from src import APP_ID, __app_name__, config, platform_util, updater


# 验证独立身份的对应行为。
def test_mac_persistent_originals_and_disposable_thumbnails(monkeypatch, tmp_path):
    monkeypatch.delenv("AWESOMEME_TEST_ROOT", raising=False)
    monkeypatch.setattr(config.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(config.platform, "system", lambda: "Darwin")
    cfg = config.Config()
    support = tmp_path / "Library" / "Application Support" / config.APP_NAME
    assert cfg.db_path == support / "memes.db"
    assert cfg.cache_dir == support / "cache"
    assert cfg.thumbnail_dir.is_relative_to(tmp_path / "Library" / "Caches")
    assert __app_name__ == "Awesomeme"
    assert not (tmp_path / "Library" / "Caches" / "OhMyMeme").exists()


# 验证独立身份的对应行为。
def test_two_profiles_do_not_share_paths_or_lock(tmp_path):
    program = """
import json, sys
from pathlib import Path
from src import APP_ID, config, platform_util
config.Path.home = lambda: Path(sys.argv[1])
print(json.dumps([APP_ID, str(config._get_data_dir()), platform_util._instance_id(), str(config.Config().sending_cache_dir)]))
"""
    records = []
    for profile in ("development", "production"):
        env = dict(os.environ, AWESOMEME_PROFILE=profile)
        env.pop("AWESOMEME_TEST_ROOT", None)
        records.append(
            json.loads(
                subprocess.check_output(
                    [sys.executable, "-c", program, str(tmp_path)],
                    env=env,
                    cwd=Path(__file__).resolve().parents[1],
                    text=True,
                )
            )
        )
    assert all(a != b for a, b in zip(*records))


# 验证独立身份的对应行为。
def test_test_root_isolates_config_data_and_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("AWESOMEME_TEST_ROOT", str(tmp_path))
    cfg = config.Config()
    assert cfg.config_dir == tmp_path / "config"
    assert cfg.db_path == tmp_path / "data" / "memes.db"
    first = platform_util._instance_id()
    first_cache = cfg.sending_cache_dir
    monkeypatch.setenv("AWESOMEME_TEST_ROOT", str(tmp_path / "another"))
    assert first != platform_util._instance_id()
    assert first_cache != cfg.sending_cache_dir
    assert first.startswith(APP_ID)


# 验证独立身份的对应行为。
def test_disabled_updater_never_fetches_downloads_or_installs(tmp_path):
    installer = tmp_path / "old.dmg"
    installer.write_bytes(b"old installer")
    with (
        mock.patch.object(
            updater.urllib.request, "urlopen", side_effect=AssertionError
        ),
        mock.patch.object(updater.subprocess, "run", side_effect=AssertionError),
        mock.patch.object(updater.subprocess, "Popen", side_effect=AssertionError),
    ):
        for force in (True, False):
            status = updater.check_latest_cached(force=force)
            assert status["disabled"] and not status["has_update"]
            assert not status.get("pending")
        assert updater.start_download("https://example.com/old.dmg") is False
        assert updater.download_release("https://example.com/old.dmg") is None
        assert updater.run_installer(str(installer)) is False
        assert updater._install_dmg_macos(str(installer)) is False
        assert updater.run_downloaded_installer() is False
