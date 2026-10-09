"""Download the optional pinned helper without running it."""

import hashlib
import json
import os
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 32 * 1024 * 1024


# Preserve existing files and publish only bytes matching the pinned release.
def fetch(root=ROOT, opener=urllib.request.urlopen):
    metadata = json.loads((root / "wechat_adapter/UPSTREAM.json").read_text())
    target = root / "wechat_adapter/bin/wxemoticon.tar.gz"
    expected = metadata["archive_sha256"]
    if target.is_symlink():
        raise RuntimeError("Refusing a symlink at the helper destination")
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise RuntimeError("Existing helper differs; it was not overwritten")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        metadata["download_url"], headers={"User-Agent": "Awesomeme-source-setup"}
    )
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            digest = hashlib.sha256()
            total = 0
            with opener(request, timeout=30) as response:
                while chunk := response.read(128 * 1024):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise RuntimeError("Download exceeds the size limit")
                    digest.update(chunk)
                    output.write(chunk)
        if digest.hexdigest() != expected:
            raise RuntimeError("Download checksum mismatch; helper was not installed")
        os.link(temporary, target)
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    print(fetch())
    print("Checksum verified. No helper was executed and no account was accessed.")
