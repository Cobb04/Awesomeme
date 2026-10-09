# 开发 Awesomeme

本仓库包含 `awesomeme/` 桌面应用，以及根目录的三个来源 Adapter。当前第一期以 Apple Silicon macOS 为验证重点；功能和支持范围以 README 为准。

## 安装与运行

使用 Python 3.12、Node.js 22.12 或更新版本。macOS 原生组件需要 Swift 6.2 或更新版本及对应 SDK；可安装匹配的 Xcode 或 Command Line Tools。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
cd awesomeme
python -m pip install -r requirements.txt
npm ci
npm run build
python scripts/build_native.py
python -m src
```

Adapter 从仓库源码安装，不依赖本地预先构建的 wheel。修改 Vue 后重新执行 `npm run build`；设置页使用 `src/webui/settings.*`，不经过 Vite。

默认使用 development 身份。隔离运行可在启动前设置 `AWESOMEME_TEST_ROOT` 为一个新的绝对路径；生产身份不接受此选项。不要用真实图库测试删除、恢复或数据库迁移。

`build_native.py` 校验并解包固定 KeyboardShortcuts 源码，在构建副本中加入桥接和已记录的 SwiftUI 兼容调整。原始归档不改写。如果系统 SwiftPM 报 `BuildServerProtocol` 符号缺失，应换用与 SDK 匹配的完整工具链。若默认 Swift 工具链不可用，可设置 `AWESOMEME_SWIFT` 为兼容 Swift 的绝对路径，并按该工具链需要设置 `SDKROOT`。

## 可选微信导出工具

在仓库根目录执行 `python scripts/fetch_wechat_helper.py`，从 `wechat_adapter/UPSTREAM.json` 记录的上游版本下载。下载器只接受固定 SHA256，且拒绝覆盖内容不匹配的已有文件。它不执行下载的程序。

下载完成后，在根目录重新执行 `python -m pip install -e '.[test]'`。helper 保留在被忽略的 `wechat_adapter/bin/` 中；不要提交它，也不要将下载成功当作微信导入成功。

## 验证

根目录与应用测试分开运行，避免两个测试目录里的同名模块互相影响。普通测试使用临时目录、合成图片和接口替身，不要求登录聊天账号。

```bash
# 仓库根目录
python -m pytest tests/ -q
cd awesomeme
python -m pytest tests/ -q
npm run build
node tests/fixtures/startup_gallery_probe.cjs immediate
node tests/fixtures/startup_gallery_probe.cjs delayed
node tests/fixtures/picker_probe.cjs
node tests/fixtures/grid_slot_probe.cjs
node tests/fixtures/settings_hotkey_probe.cjs
```

上游补丁契约与私人样本回放在缺少显式输入时跳过；跳过项不算通过的真实集成测试。原生热键、权限、输入法、目标窗口和动图效果需要另行实机验证。

Python 代码使用 Black 和 Ruff。应用目录的规则保留在 `pyproject.toml` 中；改动后运行 `black --check src/` 和 `ruff check src/`。原有未改动代码的问题应单列，避免格式化整个上游代码树制造无关差异。

## 构建

在应用目录执行：

```bash
python scripts/build.py --macos --build-only
```

构建会重新编译前端，并生成本地 `dist/Awesomeme Dev.app`。默认开发身份与个人图库隔离。生成本地应用不等于签名、公证或公开安装包发行；本仓库当前不自动发布 Release。

## 提交改动

- 保持当前架构，按问题做小范围修改。
- 保留上游版权与许可证，不批量替换内部 `omm` 桥接名、旧备份前缀等兼容标识。
- 用户可见行为变化同步更新使用说明；依赖和构建变化同步更新本文件及来源记录。
- 不提交登录态、密钥、数据库、真实导出表情、调试日志、临时客户端或个人路径。
- 第二期的三端原生收藏互通与换号迁移仍处于规划阶段，参见路线图。
