"""Check a pinned OhMyMeme checkout; apply only with an explicit --apply."""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    checkout = args.checkout.resolve()
    enclosing = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=checkout,
        capture_output=True,
        text=True,
    )
    if (
        enclosing.returncode == 0
        and Path(enclosing.stdout.strip()).resolve() != checkout
    ):
        parser.error("请选择独立 OhMyMeme 根目录，不能是其他仓库中的子目录")
    here = Path(__file__).resolve().parent
    manifest = json.loads((here / "compatibility.json").read_text())
    for relative, hashes in manifest["files"].items():
        path = checkout / relative
        if (
            not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != hashes["before"]
        ):
            parser.error("源文件与审阅版本不同，停止：" + relative)
    patch = here / "ohmymeme-feishu.patch"
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=checkout, check=True)
    if not args.apply:
        print("兼容性检查通过。使用 --apply 才会应用补丁和复制 wheel。")
        return
    wheel = here / "vendor/feishu_l1_adapter-0.2.0-py3-none-any.whl"
    if not wheel.is_file():
        parser.error("缺少随包提供的 wheel")
    vendor = checkout / "vendor"
    destination = vendor / wheel.name
    if vendor.is_symlink() or destination.is_symlink():
        parser.error("vendor 路径是符号链接，停止")
    if destination.exists():
        parser.error("wheel 已存在，停止以免覆盖")
    vendor.mkdir(exist_ok=True)
    with destination.open("xb") as output, wheel.open("rb") as source:
        shutil.copyfileobj(source, output)
    try:
        subprocess.run(["git", "apply", str(patch)], cwd=checkout, check=True)
    except BaseException:
        destination.unlink()
        raise
    for relative, hashes in manifest["files"].items():
        if (
            hashlib.sha256((checkout / relative).read_bytes()).hexdigest()
            != hashes["after"]
        ):
            raise RuntimeError("补丁结果核验失败，请检查文件：" + relative)
    print(
        "补丁已应用。请在此 OhMyMeme 的 Python 环境运行 pip install -r requirements.txt。"
    )


if __name__ == "__main__":
    main()
