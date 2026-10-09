"""构建固定上游版本的同进程热键桥。"""

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# 原始归档保持不变；仅在忽略的 build 目录添加桥和动态库产品设置。
def build():
    native = ROOT / "native"
    metadata = json.loads((native / "UPSTREAM.json").read_text())
    archive = native / "KeyboardShortcuts.tar.gz"
    if hashlib.sha256(archive.read_bytes()).hexdigest() != metadata["sha256"]:
        raise RuntimeError("KeyboardShortcuts 归档校验失败")
    stage = ROOT / "build" / "native"
    stage.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        source.extractall(stage, filter="data")
    package = stage / ("KeyboardShortcuts-" + metadata["revision"])
    manifest = package / "Package.swift"
    text = manifest.read_text().replace(
        'name: "KeyboardShortcuts",\n\t\t\ttargets:',
        'name: "KeyboardShortcuts",\n\t\t\ttype: .dynamic,\n\t\t\ttargets:',
    )
    manifest.write_text(text.replace(".macOS(.v10_15)", ".macOS(.v13)"))
    sources = package / "Sources/KeyboardShortcuts"
    # CommandLineTools 不包含 SwiftUI 宏插件；展开等价 EnvironmentKey，排除预览。
    policy = sources / "ConflictPolicy.swift"
    old = "extension EnvironmentValues {\n\t@Entry\n\tvar keyboardShortcutsConflictPolicy = KeyboardShortcuts.ConflictPolicy.default\n}"
    replacement = """private struct AwesomemeConflictPolicyKey: EnvironmentKey {
    static let defaultValue = KeyboardShortcuts.ConflictPolicy.default
}
extension EnvironmentValues {
    var keyboardShortcutsConflictPolicy: KeyboardShortcuts.ConflictPolicy {
        get { self[AwesomemeConflictPolicyKey.self] }
        set { self[AwesomemeConflictPolicyKey.self] = newValue }
    }
}"""
    if old not in policy.read_text():
        raise RuntimeError("固定上游 ConflictPolicy 与构建补丁不匹配")
    policy.write_text(policy.read_text().replace(old, replacement))
    recorder = sources / "Recorder.swift"
    recorder.write_text(recorder.read_text().split("\n#Preview {", 1)[0] + "\n#endif\n")
    shutil.copy2(native / "AwesomemeHotkey.swift", sources)
    env = dict(os.environ, CLANG_MODULE_CACHE_PATH=str(stage / "clang-cache"))
    subprocess.run(
        [
            os.environ.get("AWESOMEME_SWIFT", "swift"),
            "build",
            "--package-path",
            str(package),
            "--cache-path",
            str(stage / "swift-cache"),
            "-c",
            "release",
            "--product",
            "KeyboardShortcuts",
        ],
        check=True,
        env=env,
    )
    library = package / ".build/release/libKeyboardShortcuts.dylib"
    destination = stage / "libAwesomemeHotkey.dylib"
    shutil.copy2(library, destination)
    return destination


if __name__ == "__main__":
    print(build())
