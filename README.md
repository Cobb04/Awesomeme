<p align="center"><img src="awesomeme/src/resources/icon.png" width="112" alt="Awesomeme"></p>

# Awesomeme

**聊天可以输，找图不能慢。**

你在微信里收藏了绝妙的表情，想在飞书里用。于是打开微信，翻找半天，再切回飞书——同事已经开始讨论下一个需求了。

Awesomeme 想替你省掉这段施法前摇：把散落在微信、企业微信和飞书里的表情导入本地图库，按 **⌘E** 找图、复制、粘贴。

一个面向 **macOS** 的本地表情管理工具，基于 [OhMyMeme](https://github.com/TNTXZ/OhMyMeme) 继续开发。支持静态图、动图、搜索、标签、分组和内容去重，有独立管理窗口，也有随叫随到的选图面板。

> 表情存在自己电脑里。至于该不该发给老板，还是你决定。

**当前为 0.1.0 源码预览版。** 首轮开发与验证集中在 Apple Silicon Mac；其他平台保留的上游功能尚未逐项验证。目前提供源码，尚无已签名、公证的安装包。适合愿意动手的朋友，暂时还没修炼到“下载、双击、开斗”的境界。

## 现在能做什么

- **给表情一个家**：导入本地文件，用分组、标签和收藏整理图库。让“那张图我明明存过”少发生几次。
- **同一只猫，不交三份房租**：跨来源按内容去重，同一图片只保存一份，同时保留其来源分组。
- **⌘E，召唤表情**：先点击聊天输入框，再按 ⌘E；搜索或用方向键选择，回车确认选图。
- **帮你粘贴，发送由你**：选图后复制，在目标检查通过时请求一次粘贴；失败时可以自行按 ⌘V。**不会自动发送消息。**
- **原图留底**：必要的发送尺寸调整另存副本，不覆盖图库原件。表情可以变小，底片不能丢。
- **白天黑夜都能用**：图库、选图面板和设置页使用 Awesomeme 标识，支持系统深浅色、键盘焦点与减少动画。

## 三个平台的导入范围

先把“能导入”和“随便互通”说清楚，避免 README 比程序先实现功能。

| 来源 | 当前方式 | 已知边界 |
| --- | --- | --- |
| 飞书 | 每次扫码授权，读取收藏并下载图片 | 不保存登录会话；大量收藏的返回上限尚未确认 |
| 企业微信 | 读取已登录的本地 Mac 客户端 | 实验支持 Apple Silicon、5.0.11（70742）；需要命令行工具和系统读取权限，读取时可能短暂停顿 |
| 微信 | 使用固定版本的上游导出工具 | 实验支持 Apple Silicon、4.1.15（270102）和一个本地账号；会退出微信、运行临时副本，并需要手机登录确认 |

微信导出可能包含历史收藏资源；目前不承诺读取到完整的“当前收藏全集”。取消收藏不会自动删除本地图片。客户端版本变化可能导致对应导入停止。

**当前没有把图片写入三平台原生收藏、旧账号迁移新账号或三端自动同步的功能。** 这些属于[第二期规划](docs/ROADMAP.md)，尚未实现。

现在的用法是：**导入 Awesomeme → 在本地找图 → 粘贴到聊天。** 表情先住进同一个图库，“一键搬家到新账号”还在施工图上。

## 从源码开始

想提前用上，需要先和终端交个朋友。

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

## 几个你可能想问的问题

**会不会替我把图发出去？**

不会。Awesomeme 负责选图、复制和请求粘贴；发送由你决定。斗图的最后一道防线，是你自己的回车键。

**换个账号，我的表情就能一键搬过去吗？**

目前不能。原生收藏写入和换号迁移仍是第二期规划，见[路线图](docs/ROADMAP.md)。

**为什么没有一键安装包？**

当前是源码预览版，签名、公证和正式分发还没完成。我们也想少敲几行命令。

## 参与开发

欢迎提交可复现的问题和小范围改进。“它坏了”是故事的开头，系统版本、客户端版本和复现步骤才是破案线索。

提交前请去除私人信息。不要上传个人表情库、二维码、登录信息或聊天内容——修 bug 不需要围观你的群聊。见[贡献指南](CONTRIBUTING.md)和[安全说明](SECURITY.md)。

## 来源与许可

Awesomeme 沿用 [GNU GPL v3](LICENSE)，保留上游版权和许可声明。感谢 OhMyMeme、KeyboardShortcuts 及各来源项目；详细版本、改动和第三方许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。Awesomeme 与微信、企业微信、飞书的提供方没有官方隶属关系。

表情可以没正形，开源致谢得认真。

**English:** Awesomeme is a local-first sticker library for macOS, with source-specific importers and a Command-E picker. It copies or requests a paste into your chat; it never sends automatically. This is an experimental source preview. Native sticker-library synchronization and account migration are planned, not implemented.
