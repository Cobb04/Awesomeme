"""Bounded, credential-free downloads for already approved Feishu resource URLs.

Each call creates and closes its own requests.Session. This module never takes
an authenticated session, loads credentials, follows redirects, logs, or writes
files. A returned body is not proof of its media type or collection completeness.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import urlsplit

import requests

MAX_RESOURCE_BYTES = 50 * 1024 * 1024
TIMEOUT = (10, 20)  # connect timeout, per-read timeout; seconds
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_URL_CHARS = 16384


class ResourceHTTPError(RuntimeError):
    """Fixed code only. ``stop`` means the caller must stop the resource route.

    Never include the URL, response body, request headers, or original exception
    in a report. http_status is an optional integer suitable for a safe report.
    """

    def __init__(
        self, code: str, *, stop: bool = False, http_status: int | None = None
    ):
        self.code = code
        self.stop = stop
        self.http_status = http_status
        super().__init__(code)


class _RejectCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


def _validate_url(url: str, allowed_urls: Collection[str]) -> None:
    # The exact allowlist comes from the approved resolver, not a hostname-only
    # permission. A string is not accepted as a collection of approved URLs.
    if (
        not isinstance(url, str)
        or not url
        or len(url) > _MAX_URL_CHARS
        or not isinstance(allowed_urls, Collection)
        or isinstance(allowed_urls, (str, bytes, bytearray, Mapping))
        or url not in allowed_urls
    ):
        raise ResourceHTTPError("resource_url_not_approved", stop=True)
    if any(ord(c) < 33 or ord(c) == 127 for c in url) or "\\" in url or "#" in url:
        raise ResourceHTTPError("resource_url_rejected", stop=True)
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise ResourceHTTPError("resource_url_rejected", stop=True) from None
    if (
        parsed.scheme != "https"
        or not host
        or not host.isascii()
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
        or host.endswith(".")
        or len(host) > 253
        or not all(_HOST_LABEL.fullmatch(p) for p in host.split("."))
        or not (host == "feishucdn.com" or host.endswith(".feishucdn.com"))
    ):
        raise ResourceHTTPError("resource_url_rejected", stop=True)


def fetch_resource(
    url: str, allowed_urls: Collection[str], max_bytes: int = MAX_RESOURCE_BYTES
) -> bytes:
    """Fetch one exact approved CDN URL without login cookies or authentication.

    The caller enforces the experiment's cumulative byte budget and interprets
    media bytes. 401/403/429 and redirects are stop conditions, not reasons to
    retry with account credentials. Timeout is per connection/read, not a total
    wall-clock deadline. The decoded body is also bounded if a server compresses
    a response despite Accept-Encoding: identity.
    """
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_RESOURCE_BYTES:
        raise ResourceHTTPError("resource_invalid_size_limit", stop=True)
    _validate_url(url, allowed_urls)
    try:
        with requests.Session() as session:
            session.trust_env = False  # no netrc, environment proxy, or CA override
            session.auth = None
            session.cookies = CookieJar(policy=_RejectCookies())
            session.proxies.clear()
            session.headers.clear()
            session.headers.update(
                {
                    "User-Agent": "FeishuStickerReadProbe/1.0",
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                }
            )
            try:
                with session.get(
                    url, timeout=TIMEOUT, allow_redirects=False, stream=True
                ) as response:
                    status = response.status_code
                    if status in (401, 403, 429):
                        raise ResourceHTTPError(
                            "http_access_stop_" + str(status),
                            stop=True,
                            http_status=status,
                        )
                    if 300 <= status < 400:
                        raise ResourceHTTPError(
                            "resource_redirect_requires_review",
                            stop=True,
                            http_status=status,
                        )
                    if status != 200:
                        # In particular, never accept a partial 206 body as a file.
                        raise ResourceHTTPError(
                            "resource_http_status", http_status=status
                        )
                    length = response.headers.get("Content-Length")
                    if length is not None:
                        if (
                            not isinstance(length, str)
                            or not length.isascii()
                            or not length.isdecimal()
                        ):
                            raise ResourceHTTPError("resource_invalid_content_length")
                        if len(length) > 20 or int(length) > max_bytes:
                            raise ResourceHTTPError("resource_size_limit", stop=True)
                    body = bytearray()
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        if len(chunk) > max_bytes - len(body):
                            raise ResourceHTTPError("resource_size_limit", stop=True)
                        body.extend(chunk)
                    return bytes(body)
            finally:
                session.cookies.clear()
    except requests.Timeout:
        raise ResourceHTTPError("resource_timeout") from None
    except requests.RequestException:
        raise ResourceHTTPError("resource_network_error") from None


__all__ = ["fetch_resource", "ResourceHTTPError", "MAX_RESOURCE_BYTES"]
