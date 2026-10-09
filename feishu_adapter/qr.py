"""RAM-only QR viewer. No refresh or other mutation HTTP endpoint."""

import html
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class QRViewer:
    def __init__(self):
        self._lock = threading.Lock()
        self._svg = b""
        self._message = "正在生成二维码…"
        self._active = True
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if (
                    self.headers.get("Host") != owner.host
                    or self.path != "/"
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"
                    or self.headers.get("Origin") not in (None, owner.url.rstrip("/"))
                ):
                    self.send_error(403)
                    return
                with owner._lock:
                    svg, message, active = owner._svg, owner._message, owner._active
                # Only our qrcode encoder produces SVG; server strings never enter HTML.
                refresh = '<meta http-equiv="refresh" content="2">' if active else ""
                body = (
                    '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    + refresh
                    + "<title>飞书表情导入</title>"
                    "<style>body{font:18px system-ui;text-align:center;margin:48px auto;"
                    "max-width:560px;padding:20px;background:#fff;color:#222}"
                    "svg{width:320px;height:320px;max-width:90vw}p{line-height:1.6}</style>"
                    "<h1>飞书表情导入</h1><p>"
                    + html.escape(message)
                    + "</p>"
                    + svg.decode()
                    + "</html>"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
                )
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.host = "127.0.0.1:" + str(self.server.server_port)
        self.url = "http://" + self.host + "/"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def show(self, svg):
        with self._lock:
            self._svg = svg
            self._message = "请用手机飞书扫码并确认。二维码有效期最多 3 分钟。"

    def status(self, message, *, done=False):
        with self._lock:
            self._svg = b""
            self._message = message
            self._active = not done

    def close(self):
        self.status("扫码页面已关闭。", done=True)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
