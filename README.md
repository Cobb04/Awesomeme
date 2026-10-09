<p align="center"><img src="awesomeme/src/resources/icon.png" width="112" alt="Awesomeme"></p>

# Awesomeme

把表情放进自己的本地图库，用 **⌘E** 随时找到、复制和粘贴。

Awesomeme 是面向 macOS 的本地表情管理工具，基于 [OhMyMeme](https://github.com/TNTXZ/OhMyMeme) 继续开发。第一期提供微信、企业微信、飞书收藏导入，独立管理窗口和快捷选图面板。图片保存在本机，支持静态图与动图、内容去重、搜索、标签和分组。

**当前为 0.1.0 源码预览版。** 首轮开发与验证集中在 Apple Silicon Mac；其他平台保留的上游功能尚未逐项验证。本次公开源码，不提供已签名、公证的安装包，也不将第二期功能计入现有能力。

## 现在能做什么

- **集中管理**：导入本地文件、整理分组和标签、收藏常用表情。
- **跨来源去重**：同一图片只保存一份，同时保留其来源分组。
- **快捷选图**：先点击聊天输入框，再按 ⌘E；搜索或用方向键选择，回车确认。
- **复制与粘贴**：选图后复制，在目标检查通过时请求一次粘贴；失败时可以自行按 ⌘V。**不会自动发送消息。**
- **保留原图**：图库保存原件；必要的发送尺寸调整另存副本，不覆盖图库文件。
- **独立外观**：图库、选图面板和设置页使用 Awesomeme 标识，支持系统深浅色、键盘焦点与减少动画。

## 三个平台的导入范围

| 来源 | 当前方式 | 已知边界 |
| --- | --- | --- |
| 飞书 | 每次扫码授权，读取收藏并下载图片 | 不保存登录会话；大量收藏的返回上限尚未确认 |
| 企业微信 | 读取已登录的本地 Mac 客户端 | 实验支持 Apple Silicon、5.0.11（70742）；需要命令行工具和系统读取权限，读取时可能短暂停顿 |
| 微信 | 使用固定版本的上游导出工具 | 实验支持 Apple Silicon、4.1.15（270102）和一个本地账号；会退出微信、运行临时副本，并需要手机登录确认 |

微信导出可能包含历史收藏资源；目前不承诺读取到完整的“当前收藏全集”。取消收藏不会自动删除本地图片。客户端版本变化可能导致对应导入停止。

**当前没有把图片写入三平台原生收藏、旧账号迁移新账号或三端自动同步的功能。** 这些属于[第二期规划](docs/ROADMAP.md)。

## 从源码开始

需要 Python 3.12、Node.js 22.12 或更新版本，以及构建原生热键组件所需的 Swift 6.2 或更新工具链。完整步骤和依赖说明见[开发指南](CONTRIBUTING.md)。

```bash
git clone https://github.com/Cobb04/Awesomeme.git
cd Awesomeme
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

微信 Mac 导出 helper 不随这个源码仓库分发。需要该功能时，在仓库根目录运行：

```bash
python scripts/fetch_wechat_helper.py
```

该命令从固定上游版本下载并校验文件，不启动微信、不执行导出。下载后重新安装根目录 Adapter 包。其他导入和图库功能不依赖这个可选 helper。

## 使用与数据

开发版显示为 **Awesomeme Dev**。默认图库和配置位于 `~/Library/Application Support/Awesomeme Development`，缩略图及发送副本位于 `~/Library/Caches/Awesomeme Development`。旧 OhMyMeme 数据不会自动迁移。

粘贴需要 macOS 辅助功能及事件投递权限。请先点击预期输入框并结束输入法选词；微信可能不公开完整的输入控件状态。飞书大图使用较小的发送副本，不能安全转换的动画保留原尺寸。更多细节见[使用说明](docs/USAGE.md)。

自动更新目前关闭。源码中的其他上游导入、云端同步和插件入口属于继承能力，不代表本项目已完成全部平台验收。

## 参与开发

欢迎提交可复现的问题和小范围改进。报告时附系统版本、客户端版本和去除私人信息的复现步骤；不要上传个人表情库、二维码、登录信息或聊天内容。见[贡献指南](CONTRIBUTING.md)和[安全说明](SECURITY.md)。

## 来源与许可

Awesomeme 沿用 [GNU GPL v3](LICENSE)，保留上游版权和许可声明。感谢 OhMyMeme、KeyboardShortcuts 及各来源项目；详细版本、改动和第三方许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。Awesomeme 与微信、企业微信、飞书的提供方没有官方隶属关系。

**English:** Awesomeme is a local-first sticker library for macOS, with source-specific importers and a Command-E picker. It copies or requests a paste into your chat; it never sends automatically. This is an experimental source preview. Native sticker-library synchronization and account migration are planned, not implemented.
