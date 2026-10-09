import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "fetch_helper",
    Path(__file__).resolve().parents[1] / "scripts/fetch_wechat_helper.py",
)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


@pytest.fixture
def source(tmp_path):
    folder = tmp_path / "wechat_adapter"
    folder.mkdir()
    body = b"synthetic helper archive"
    (folder / "UPSTREAM.json").write_text(
        json.dumps(
            {
                "download_url": "https://example.invalid/helper",
                "archive_sha256": hashlib.sha256(body).hexdigest(),
            }
        )
    )
    return tmp_path, body


def test_verified_download_is_reusable_without_network(source):
    root, body = source
    target = helper.fetch(root, lambda *a, **k: io.BytesIO(body))
    assert target.read_bytes() == body
    assert (
        helper.fetch(root, lambda *a, **k: pytest.fail("unexpected network")) == target
    )
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]


def test_corrupt_download_is_not_installed(source):
    root, _ = source
    with pytest.raises(RuntimeError, match="checksum"):
        helper.fetch(root, lambda *a, **k: io.BytesIO(b"wrong"))
    assert not list((root / "wechat_adapter/bin").iterdir())


def test_existing_unknown_file_is_preserved(source):
    root, _ = source
    target = root / "wechat_adapter/bin/wxemoticon.tar.gz"
    target.parent.mkdir()
    target.write_bytes(b"keep this file")
    with pytest.raises(RuntimeError, match="not overwritten"):
        helper.fetch(root, lambda *a, **k: pytest.fail("unexpected network"))
    assert target.read_bytes() == b"keep this file"


def test_symlink_is_rejected(source):
    root, _ = source
    target = root / "wechat_adapter/bin/wxemoticon.tar.gz"
    target.parent.mkdir()
    original = root / "original"
    original.write_bytes(b"preserved")
    target.symlink_to(original)
    with pytest.raises(RuntimeError, match="symlink"):
        helper.fetch(root, lambda *a, **k: pytest.fail("unexpected network"))
    assert original.read_bytes() == b"preserved"


def test_oversize_download_is_removed(source, monkeypatch):
    root, body = source
    monkeypatch.setattr(helper, "MAX_BYTES", 3)
    with pytest.raises(RuntimeError, match="size limit"):
        helper.fetch(root, lambda *a, **k: io.BytesIO(body))
    assert not list((root / "wechat_adapter/bin").iterdir())
