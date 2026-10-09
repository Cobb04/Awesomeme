"""Bounded, credential-free download of exact locators from the favorites service."""

import http.client
import ipaddress
import ssl
import time
from urllib.parse import urljoin, urlsplit

from feishu_adapter.adapter import AdapterError

HOST = "wework.qpic.cn"
MAX_BYTES = 20 * 1024 * 1024


def validate_url(url):
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise AdapterError("invalid_locator")
    if any(ord(c) < 33 or ord(c) > 126 for c in url):
        raise AdapterError("non_ascii_or_control_in_url")
    try:
        p = urlsplit(url)
        if (
            p.scheme != "https"
            or p.hostname != HOST
            or p.username is not None
            or p.password is not None
            or p.port not in (None, 443)
            or p.fragment
        ):
            raise AdapterError("url_outside_allowlist")
    except ValueError:
        raise AdapterError("malformed_url") from None
    return p


def fetch(url, max_bytes=MAX_BYTES, cancelled=lambda: False):
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES:
        raise AdapterError("resource_size_limit")
    deadline = time.monotonic() + 22
    redirects = 0
    while True:
        if cancelled():
            raise AdapterError("cancelled")
        p = validate_url(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdapterError("download_deadline")
        conn = http.client.HTTPSConnection(
            HOST, 443, timeout=min(5, remaining), context=ssl.create_default_context()
        )
        try:
            conn.connect()
            if not ipaddress.ip_address(conn.sock.getpeername()[0]).is_global:
                raise AdapterError("non_public_peer")
            target = p.path or "/"
            if p.query:
                target += "?" + p.query
            conn.request(
                "GET",
                target,
                headers={
                    "User-Agent": "Awesomeme-WeCom/1",
                    "Accept": "image/*",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location or redirects >= 2:
                    raise AdapterError("redirect_limit")
                url = urljoin(url, location)
                validate_url(url)
                redirects += 1
                continue
            if response.status != 200:
                raise AdapterError("http_status_%d" % response.status)
            encoding = response.getheader("Content-Encoding")
            if encoding and encoding.lower() != "identity":
                raise AdapterError("unexpected_encoding")
            length = response.getheader("Content-Length")
            if length and (not length.isdecimal() or int(length) > max_bytes):
                raise AdapterError("content_length_limit")
            content = bytearray()
            while True:
                if cancelled():
                    raise AdapterError("cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdapterError("download_deadline")
                # HTTPResponse owns its socket after Connection: close; its original
                # 5-second read timeout remains active even if conn.sock is now None.
                if conn.sock:
                    conn.sock.settimeout(min(5, remaining))
                chunk = response.read(min(65536, max_bytes + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > max_bytes:
                    raise AdapterError("body_limit")
            if length and len(content) != int(length):
                raise AdapterError("truncated_body")
            if not content:
                raise AdapterError("empty_body")
            return bytes(content)
        except AdapterError:
            raise
        except (OSError, http.client.HTTPException):
            raise AdapterError("resource_network_error") from None
        finally:
            conn.close()
