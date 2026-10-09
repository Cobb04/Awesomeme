"""Small, fixed read-only protocol surface. Never persist the Session."""

import io
import json
import time
import uuid
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

import qrcode
import requests
from qrcode.image.svg import SvgPathImage

from ._resolver import extract_resource_config
from ._wire import StickerWireError, build_pull, decode_pull
from .adapter import AdapterError

ACCOUNTS = "https://accounts.feishu.cn"
API = "https://internal-api-lark-api.feishu.cn"
WEB = "https://open-dev.feishu.cn"
ROUTES = {
    ("POST", ACCOUNTS + "/accounts/qrlogin/init"),
    ("POST", ACCOUNTS + "/accounts/qrlogin/polling"),
    ("GET", API + "/accounts/web/user"),
    ("GET", API + "/settings/v3/"),
    ("POST", API + "/im/gateway/"),
}


class FeishuClient:
    profile_name = "tested_compatibility_profile_3.9.32_app161471"

    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            }
        )
        self.authenticated = False
        self.pulls = 0

    def _request(self, method, url, *, headers=None, **kwargs):
        if (method, url) not in ROUTES:
            raise AdapterError("route_rejected")
        if (
            urlsplit(url).path == "/im/gateway/"
            and (headers or {}).get("x-command") != "103"
        ):
            raise AdapterError("gateway_command_rejected")
        try:
            with self.session.request(
                method,
                url,
                headers=headers,
                timeout=(10, 20),
                allow_redirects=False,
                stream=True,
                **kwargs,
            ) as response:
                if response.status_code != 200:
                    raise AdapterError("http_status_" + str(response.status_code))
                body = bytearray()
                for chunk in response.iter_content(65536):
                    if len(body) + len(chunk) > 16 * 1024 * 1024:
                        raise AdapterError("response_size_limit")
                    body.extend(chunk)
                return bytes(body), response.headers.get("x-flow-key", "")
        except requests.RequestException:
            raise AdapterError("network_error") from None

    def _json(self, method, url, **kwargs):
        body, flow = self._request(method, url, **kwargs)
        try:
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError()
            return value, flow
        except (ValueError, UnicodeError):
            raise AdapterError("invalid_json_response") from None

    def _cookies_for(self, url):
        prepared = self.session.prepare_request(requests.Request("GET", url))
        cookies = SimpleCookie()
        cookies.load(prepared.headers.get("Cookie", ""))
        return cookies

    def login(self, on_qr, cancelled):
        headers = {
            "Content-Type": "application/json",
            "X-App-Id": "1",
            "X-Api-Version": "1.0.8",
            "X-Device-Info": "platform=websdk",
            "X-Terminal-Type": "2",
            "Origin": ACCOUNTS,
            "Referer": ACCOUNTS + "/accounts/page/login?app_id=1",
        }
        result, flow = self._json(
            "POST",
            ACCOUNTS + "/accounts/qrlogin/init",
            headers=headers,
            json={"redirect_uri": WEB + "/next/messenger"},
        )
        if result.get("code") != 0:
            raise AdapterError("qr_init_failed")
        token = (result.get("data") or {}).get("step_info", {}).get("token")
        if not isinstance(token, str) or not token or not flow:
            raise AdapterError("qr_init_missing_fields")
        buffer = io.BytesIO()
        qrcode.make(
            json.dumps({"qrlogin": {"token": token}}, separators=(",", ":")),
            image_factory=SvgPathImage,
        ).save(buffer)
        on_qr(buffer.getvalue())
        token = result = buffer = None
        headers["X-Flow-Key"] = flow
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if cancelled():
                raise AdapterError("cancelled")
            time.sleep(2)
            result, _ = self._json(
                "POST", ACCOUNTS + "/accounts/qrlogin/polling", headers=headers, json={}
            )
            if result.get("code") != 0:
                raise AdapterError("qr_poll_failed")
            data = result.get("data") or {}
            status = (data.get("step_info") or {}).get("status")
            if status in (3, 4, 5):
                raise AdapterError(
                    {3: "qr_cancelled", 4: "qr_error", 5: "qr_expired"}[status]
                )
            if "session" in self._cookies_for(API + "/im/gateway/"):
                self._authenticate()
                return
            if data.get("next_step") != "qr_login_polling" or status == 0:
                raise AdapterError("login_extra_step_required")
        raise AdapterError("qr_timeout")

    def _authenticate(self):
        headers = {
            "x-app-id": "12",
            "x-api-version": "1.0.8",
            "x-device-info": "platform=websdk",
            "x-lgw-os-type": "1",
            "x-lgw-terminal-type": "2",
            "origin": WEB,
            "referer": WEB + "/",
        }
        cookies = self._cookies_for(API + "/accounts/web/user")
        if "swp_csrf_token" in cookies:
            headers["x-csrf-token"] = cookies["swp_csrf_token"].value
        profile, _ = self._json(
            "GET",
            API + "/accounts/web/user",
            headers=headers,
            params={"app_id": "12", "_t": int(time.time() * 1000)},
        )
        user_id = ((profile.get("data") or {}).get("user") or {}).get("id")
        if (
            profile.get("code") not in (None, 0)
            or not str(user_id).isascii()
            or not str(user_id).isdecimal()
            or int(user_id) <= 0
        ):
            raise AdapterError("authentication_unconfirmed")
        self.authenticated = True

    def snapshot(self):
        if not self.authenticated:
            raise AdapterError("authentication_required")
        if self.pulls >= 6:
            raise AdapterError("list_request_limit")
        self.pulls += 1
        cid = uuid.uuid4().hex
        headers = {
            "content-type": "application/x-protobuf",
            "origin": WEB,
            "referer": WEB + "/",
            "x-appid": "161471",
            "x-command": "103",
            "x-command-version": "5.7.0",
            "x-lgw-os-type": "1",
            "x-lgw-terminal-type": "2",
            "x-source": "web",
            "x-web-version": "3.9.32",
            "x-request-id": cid,
        }
        body, _ = self._request(
            "POST", API + "/im/gateway/", headers=headers, data=build_pull(1000, cid)
        )
        try:
            return decode_pull(body)
        except StickerWireError as error:
            raise AdapterError(error.code) from None

    def resource_config(self, snapshot):
        if not self.authenticated:
            raise AdapterError("authentication_required")
        settings, _ = self._json(
            "GET",
            API + "/settings/v3/",
            params={
                "app_id": "161471",
                "version": "3.9.32",
                "app": "lark",
                "platform": "web",
                "action": "startup",
                "app_channel": "",
                "tags": "web_messenger",
            },
        )
        if settings.get("meta", {}).get("complete") is not True:
            raise AdapterError("settings_not_complete")
        needed = {
            s.get("image", {}).get("origin", {}).get("fsUnit")
            for s in snapshot["stickers"]
        }
        units = (
            settings.get("data", {}).get("resource_url_tpl", {}).get("fs_unit_tpl", {})
        )
        selected = {unit: units[unit] for unit in needed if unit in units}
        if not selected:
            return {"resource_url_tpl": {"fs_unit_tpl": {}}}
        return extract_resource_config(
            {
                "meta": settings.get("meta"),
                "data": {"resource_url_tpl": {"fs_unit_tpl": selected}},
            }
        )

    def close(self):
        self.authenticated = False
        self.session.cookies.clear()
        self.session.close()
