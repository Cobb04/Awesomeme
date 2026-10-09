"""python -m feishu_adapter export --output <new-directory>"""

import argparse
import json
import signal
import time
from datetime import datetime
from pathlib import Path

from .adapter import FeishuAdapter
from .qr import QRViewer


def main():
    parser = argparse.ArgumentParser(description="扫码后自动导出飞书收藏表情原图。")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="独立扫码登录并导出")
    export.add_argument(
        "--output",
        type=Path,
        default=Path("exports") / datetime.now().strftime("%Y%m%d-%H%M%S"),
    )
    args = parser.parse_args()
    if args.output.exists():
        parser.error("输出目录已存在。请指定新目录，以免覆盖文件。")
    interrupted = False

    def stop(*_args):
        nonlocal interrupted
        interrupted = True

    old_sigint = signal.signal(signal.SIGINT, stop)
    viewer = None
    try:
        viewer = QRViewer()

        def show(svg):
            viewer.show(svg)
            print("请打开扫码页面：" + viewer.url, flush=True)

        def progress(event):
            if event["event"] == "authenticated":
                viewer.status("授权成功，正在自动下载收藏表情。")
            print(json.dumps(event, ensure_ascii=False), flush=True)

        report = FeishuAdapter().export(
            args.output, on_qr=show, progress=progress, cancelled=lambda: interrupted
        )
        viewer.status(
            (
                "导出完成。结果见终端。"
                if report["import_ready"]
                else "导出未全部完成。原因见终端；二维码已清除。"
            ),
            done=True,
        )
        print(
            json.dumps(
                {key: value for key, value in report.items() if key != "records"},
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        print("结果目录：" + str(args.output.absolute()), flush=True)
        # Let the visible page receive the terminal status before the server exits.
        if not interrupted:
            time.sleep(3)
        return 0 if report["import_ready"] else 2
    finally:
        if viewer is not None:
            viewer.close()
        signal.signal(signal.SIGINT, old_sigint)


if __name__ == "__main__":
    raise SystemExit(main())
