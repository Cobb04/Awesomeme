# Third-party notices

Awesomeme is a modified distribution based on **OhMyMeme**, licensed under GNU GPL version 3. The full license is in [LICENSE](LICENSE), with the inherited copy retained in [awesomeme/LICENSE](awesomeme/LICENSE). Renaming the product and replacing its visual assets does not remove its source attribution.

## OhMyMeme

- Upstream: <https://github.com/OhMyMeme/OhMyMeme>
- Baseline recorded by this project: `3a3cad5c6a17544718ca5eb309a2ab77e7dadd3b` (0.6.5).
- License: GPL-3.0. Existing file-level author notices are retained.
- Awesomeme modifications through 2026-10-09: independent branding; macOS menu-bar and quick-picker workflow; image paste handling; refreshed Vue library and settings; WeChat, WeCom and Feishu source adapters; isolated validation tools and tests.
- The adapter source and new project code in this repository are distributed under GPL-3.0, except files explicitly carrying other notices below. The legacy `feishu-l1-adapter` package name and internal compatibility identifiers are retained where required.

## KeyboardShortcuts

- Author: Sindre Sorhus and contributors.
- Source: <https://github.com/sindresorhus/KeyboardShortcuts/tree/772133d9dbe800fdac0473226822994c5c162c58>.
- License: MIT; full notice: [KeyboardShortcuts-LICENSE](awesomeme/native/KeyboardShortcuts-LICENSE).
- The vendored source archive contains its own license. Pinned provenance and local build adaptations are recorded in [native/UPSTREAM.json](awesomeme/native/UPSTREAM.json).

## ABogus implementation

[abogus.py](awesomeme/src/abogus.py) retains its original GPLv3 notice and its adaptation chain: JoeanAmier/TikTokDownloader, Evil0ctal/Douyin_TikTok_Download_API and tool-douyin-emoji. The exact original import revision has not yet been established; the file-level notices remain authoritative for the copied material. Current licenses of intermediate projects do not replace these notices.

## Optional WeChat helper

The repository does **not** redistribute the `wxemoticon` executable, its archive, or the old adapter wheels that embedded it. The optional setup script downloads the fixed upstream v0.3.0 archive and verifies its SHA-256; it does not run the program.

- Upstream release: <https://github.com/liusheng22/export-wechat-emoji/releases/tag/v0.3.0>.
- Recorded source: `f1196314b66eddbe4da2a8b958c8082b5945aa1f`.
- Version, source and checksums: [wechat_adapter/UPSTREAM.json](wechat_adapter/UPSTREAM.json).
- The CLI declares MIT, but its macOS build embeds code adapted from Facebook fishhook (BSD-3-Clause) and other dependencies. This statement is not a complete license notice for that executable. Anyone redistributing it must independently verify and include the notices for the actual build.

## Installed dependencies and binary builds

Python and JavaScript dependencies are installed separately from their source registries; generated frontend bundles are excluded from this source repository. Their respective licenses continue to apply. The Swift source dependency above is included with its MIT notice.

This source preview does not include an official application binary. Before a binary release, the actual bundled dependency versions, complete license texts, notices and corresponding-source obligations must be checked. This includes TgCrypto (LGPLv3+), native code inside Python wheels, and any bundled helper. A source dependency declaration alone is not a complete notice for a compiled application.
