"""Opt-in contract test against a supplied upstream checkout.

Executes the ACTUAL _do_import AST and MemeDB. No WebView/app initialization.
pHash calculation and manifest rebuild are stubbed; stego detection is actual
upstream code, exercised only with the recorded ordinary GIF (no STG3 payload).
This test is not a full GUI integration test.
"""

import ast
import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from test_adapter import Backend, export

from feishu_adapter import import_into_ohmymeme


def functions(path, names):
    tree = ast.parse(path.read_text())
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            selected.append(node)
        if isinstance(node, ast.ClassDef) and node.name == "WebUI":
            selected.extend(
                n
                for n in node.body
                if isinstance(n, ast.FunctionDef) and n.name in names
            )
    assert {node.name for node in selected} == set(names)
    return compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec")


def test_actual_upstream_import_and_repeat(tmp_path):
    upstream = os.environ.get("OHMYMEME_SOURCE")
    if not upstream:
        pytest.skip("Set OHMYMEME_SOURCE to a reviewed OhMyMeme checkout")
    source = Path(upstream) / "src"
    evidence = (
        Path(__file__).resolve().parents[1]
        / "investigation/feishu/live_probe/runs/20261005T165607Z"
    )
    if not evidence.exists():
        pytest.skip("Private local evidence not distributed")
    snapshot = json.loads((evidence / "list-3.json").read_text())
    assets = json.loads(
        (evidence / "resources/manifest-001-download.json").read_text()
    )["records"]
    by_key = {
        s["image"]["origin"]["key"]: (evidence / "resources" / r["file"]).read_bytes()
        for s, r in zip(snapshot["stickers"], assets)
    }
    backend = Backend(snapshot["stickers"])
    root, report = export(
        tmp_path, backend, lambda url, *_: by_key[url.rsplit("/", 1)[-1]]
    )
    assert report["import_ready"]

    tree = ast.parse((source / "database.py").read_text())
    tree.body = [
        n for n in tree.body if not isinstance(n, ast.ImportFrom) or not n.level
    ]
    db_globals = {"__name__": "isolated_upstream_database"}
    exec(compile(tree, str(source / "database.py"), "exec"), db_globals)
    db = db_globals["MemeDB"](tmp_path / "isolated.db")
    cache = tmp_path / "ohmymeme_cache"
    cache.mkdir()
    ext_globals = {}
    ext_tree = ast.parse((source / "adb_util.py").read_text())
    for node in ext_tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_QQ_FILE_TYPES" for t in node.targets
        ):
            exec(
                compile(
                    ast.Module(body=[node], type_ignores=[]),
                    "adb_util_constants",
                    "exec",
                ),
                ext_globals,
            )
    exec(functions(source / "adb_util.py", {"_detect_ext"}), ext_globals)
    context = {
        "os": os,
        "logger": logging.getLogger("contract"),
        "get_config": lambda: SimpleNamespace(cache_dir=cache),
        "get_db": lambda: db,
        "HAS_PIL": True,
        "PILImage": Image,
        "_IMPORT_LOCK": threading.Lock(),
        "_IMPORT_MAX_BYTES": 20 * 1024 * 1024,
        "_IMPORT_MAX_PX": 2560,
        "_perceptual_hash_path": lambda path: None,
        "build_manifest": lambda: None,
        "adb_util": SimpleNamespace(_detect_ext=ext_globals["_detect_ext"]),
    }
    exec(
        functions(
            source / "webui.py",
            {"_do_import", "_file_sha256", "_detect_image_ext", "_try_decode_stego"},
        ),
        context,
    )

    def importer(paths, **kwargs):
        return context["_do_import"](None, paths, **kwargs)

    first = import_into_ohmymeme(root, importer)
    second = import_into_ohmymeme(root, importer)
    assert len(first["ids"]) == 4 and first["unaccounted"] == 0
    assert (
        second["ids"] == []
        and second["skipped_dup"] == 4
        and second["unaccounted"] == 0
    )
    assert len(list(cache.iterdir())) == 4
    assert {hashlib.sha256(p.read_bytes()).hexdigest() for p in cache.iterdir()} == {
        record["media"]["sha256"] for record in assets
    }
    gif = next(cache.glob("*.gif"))
    with Image.open(gif) as picture:
        assert picture.n_frames == 10
    # Feed the same verified originals through the real background worker too.
    from feishu_adapter import FeishuAdapter
    from feishu_adapter.desktop import ImportJob

    job = ImportJob(
        importer,
        None,
        tmp_path / "job",
        adapter_factory=lambda: FeishuAdapter(
            backend=Backend(snapshot["stickers"]),
            downloader=lambda url, *_: by_key[url.rsplit("/", 1)[-1]],
        ),
    )
    assert job.start()["ok"]
    job._thread.join(timeout=5)
    assert not job._thread.is_alive()
    assert job.state()["status"] == "done"
    assert job.state()["result"]["skipped_dup"] == 4
    db.close()
