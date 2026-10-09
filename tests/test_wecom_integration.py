"""Exercise the actual desktop modules with an isolated database and configuration."""

import sys
from pathlib import Path

import pytest
from test_wecom import Backend, media, row

from feishu_adapter.desktop import ImportJob
from wecom_adapter import WeComAdapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "awesomeme"))


@pytest.fixture
def ui(tmp_path, monkeypatch):
    from src import config, database, manifest, webui

    monkeypatch.setattr(config, "_get_config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(config, "_get_data_dir", lambda: tmp_path / "data")
    cfg = config.Config()
    db = database.MemeDB(tmp_path / "isolated.db")
    for module in (webui, manifest):
        monkeypatch.setattr(module, "get_config", lambda: cfg)
        monkeypatch.setattr(module, "get_db", lambda: db)
    monkeypatch.setattr(webui.WebUI, "_find_free_port", lambda self: 12345)
    host = webui.WebUI()
    yield host, db, cfg
    db.close()


def test_settings_real_import_repeat_and_cross_source_groups(ui, monkeypatch):
    host, db, cfg = ui
    import wecom_adapter

    def factory():
        return WeComAdapter(
            backend=Backend([row(1), row(2)]),
            downloader=lambda url, *_: media(url.endswith("2")),
        )

    monkeypatch.setattr(wecom_adapter, "WeComAdapter", factory)
    api = host._settings_api
    assert api.start_wecom_import(api.get_wecom_import_progress()["generation"])["ok"]
    host._wecom_job._thread.join(5)
    first = api.get_wecom_import_progress()
    assert first["status"] == "done" and len(first["result"]["ids"]) == 2
    assert len(db.get_all()) == 2
    cache = {p.name: p.read_bytes() for p in cfg.cache_dir.iterdir() if p.is_file()}
    assert set(cache.values()) == {media(), media(True)}
    # Existing image has a second source; repeating must backfill its WeCom group.
    mid = first["result"]["ids"][0]
    other = host.ensure_import_collection([mid], "飞书")
    db._get_conn().execute(
        "DELETE FROM meme_collections WHERE meme_id=? AND collection_id!=?",
        (mid, other),
    )
    db._get_conn().commit()
    assert api.start_wecom_import(first["generation"])["ok"]
    host._wecom_job._thread.join(5)
    second = api.get_wecom_import_progress()
    assert second["status"] == "done" and second["result"]["skipped_dup"] == 2
    assert second["result"]["ids"] == []
    assert len(db.get_all()) == 2
    assert (
        db._get_conn()
        .execute("SELECT COUNT(*) FROM meme_collections WHERE meme_id=?", (mid,))
        .fetchone()[0]
        == 2
    )


def test_stale_settings_start_cannot_attach(ui, monkeypatch):
    host, _, _ = ui
    monkeypatch.setattr(ImportJob, "start", lambda self: pytest.fail("stale start"))
    api = host._settings_api
    old = api.get_wecom_import_progress()["generation"]
    api.cancel_wecom_import(old)
    assert not api.start_wecom_import(old)["ok"]
    assert not api.start_wecom_import(True)["ok"]


def test_old_window_close_does_not_cancel_new_job(ui):
    host, _, _ = ui
    host._settings_window = object()
    generation = host._wecom_next_generation
    host._on_feishu_settings_closed(object())
    assert host._wecom_next_generation == generation
    host._on_feishu_settings_closed(host._settings_window)
    assert host._wecom_next_generation == generation + 1
