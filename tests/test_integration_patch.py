import ast
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_adapter import Backend, png

import feishu_adapter.desktop as desktop
from feishu_adapter import FeishuAdapter


@pytest.fixture
def patched_checkout(tmp_path):
    upstream = os.environ.get("OHMYMEME_SOURCE")
    if not upstream:
        pytest.skip("Set OHMYMEME_SOURCE to test integration patch")
    integration = Path(__file__).resolve().parents[1] / "integration"
    compatibility = json.loads((integration / "compatibility.json").read_text())
    for relative, hashes in compatibility["files"].items():
        source = Path(upstream) / relative
        assert hashlib.sha256(source.read_bytes()).hexdigest() == hashes["before"]
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    patch = str(integration / "ohmymeme-feishu.patch")
    subprocess.run(["git", "apply", "--check", patch], cwd=tmp_path, check=True)
    subprocess.run(["git", "apply", patch], cwd=tmp_path, check=True)
    for relative, hashes in compatibility["files"].items():
        assert (
            hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest()
            == hashes["after"]
        )
    return tmp_path


def test_patch_and_settings_bridge_in_isolation(
    tmp_path, monkeypatch, patched_checkout
):
    tree = ast.parse((patched_checkout / "src/webui.py").read_text())
    api = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SettingsApi"
    )
    nodes = [
        node
        for node in api.body
        if isinstance(node, ast.FunctionDef) and "feishu_import" in node.name
    ]
    context = {}
    exec(
        compile(
            ast.Module(body=nodes, type_ignores=[]), "patched_settings_api", "exec"
        ),
        context,
    )
    imported = []

    def importer(paths, **kwargs):
        imported.extend(paths)
        return {"ids": [1], "rejected": 0, "skipped_dup": 0}

    original = desktop.ImportJob
    monkeypatch.setattr(
        desktop,
        "ImportJob",
        lambda *args: original(
            *args,
            adapter_factory=lambda: FeishuAdapter(
                backend=Backend(), downloader=lambda *_: png()
            ),
        ),
    )
    host = SimpleNamespace(
        _feishu_job=None,
        _feishu_job_lock=threading.Lock(),
        _feishu_next_generation=0,
        _feishu_run_generation=-1,
        _do_import=importer,
        ensure_import_collection=lambda *args: 1,
    )
    settings = SimpleNamespace(
        _webui=host, _cfg=SimpleNamespace(data_dir=tmp_path / "data")
    )
    assert context["get_feishu_import_progress"](settings)["status"] == "idle"
    assert context["start_feishu_import"](settings, 0)["ok"]
    host._feishu_job._thread.join(timeout=3)
    assert context["get_feishu_import_progress"](settings)["status"] == "done"
    assert len(imported) == 1
    assert context["cancel_feishu_import"](settings)["ok"]


@pytest.mark.parametrize("cancel_generation", [0, None])
def test_cancel_fences_start_that_has_not_arrived(
    monkeypatch, tmp_path, cancel_generation, patched_checkout
):
    source = patched_checkout / "src/webui.py"
    tree = ast.parse(source.read_text())
    api = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SettingsApi"
    )
    nodes = [
        n
        for n in api.body
        if isinstance(n, ast.FunctionDef) and "feishu_import" in n.name
    ]
    context = {}
    exec(
        compile(
            ast.Module(body=nodes, type_ignores=[]), "patched_settings_api", "exec"
        ),
        context,
    )
    host = SimpleNamespace(
        _feishu_job=None,
        _feishu_job_lock=threading.Lock(),
        _feishu_next_generation=0,
        _feishu_run_generation=-1,
        _do_import=lambda *_: None,
        ensure_import_collection=None,
    )
    settings = SimpleNamespace(_webui=host, _cfg=SimpleNamespace(data_dir=tmp_path))
    actions = []

    class Job:
        def __init__(self, *_):
            pass

        def start(self):
            actions.append("started")
            return {"ok": True}

        def cancel(self):
            actions.append("cancelled")
            return {"ok": True}

        def state(self):
            return {"status": "running", "busy": True}

    monkeypatch.setattr(desktop, "ImportJob", Job)
    generation = context["get_feishu_import_progress"](settings).get("generation", 0)
    cancel = context["cancel_feishu_import"]
    start = context["start_feishu_import"]
    assert cancel(
        *(
            [settings, cancel_generation]
            if len(inspect.signature(cancel).parameters) > 1
            else [settings]
        )
    )["ok"]
    result = start(
        *(
            [settings, generation]
            if len(inspect.signature(start).parameters) > 1
            else [settings]
        )
    )
    assert not result["ok"] and not actions
    current = context["get_feishu_import_progress"](settings)["generation"]
    assert start(settings, current)["ok"]
    cancel(settings, generation)
    assert actions == ["started"]
    cancel(settings, current)
    assert actions == ["started", "cancelled"]


def test_old_window_close_does_not_cancel_new_window(patched_checkout):
    tree = ast.parse((patched_checkout / "src/webui.py").read_text())
    host_class = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "WebUI"
    )
    node = next(
        n
        for n in host_class.body
        if isinstance(n, ast.FunctionDef) and n.name == "_on_feishu_settings_closed"
    )
    context = {}
    exec(
        compile(ast.Module(body=[node], type_ignores=[]), "window_close", "exec"),
        context,
    )
    current, old = object(), object()
    actions = []
    host = SimpleNamespace(
        _feishu_job_lock=threading.RLock(),
        _settings_window=current,
        _settings_api=SimpleNamespace(
            cancel_feishu_import=lambda: actions.append("cancelled")
        ),
    )
    context["_on_feishu_settings_closed"](host, old)
    assert actions == []
    context["_on_feishu_settings_closed"](host, current)
    assert actions == ["cancelled"]
