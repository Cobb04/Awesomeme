from unittest.mock import Mock, patch

import pytest

from feishu_adapter import AdapterError
from feishu_adapter.client import API, FeishuClient


def test_session_never_loads_existing_credentials():
    client = FeishuClient()
    assert client.session.trust_env is False
    assert len(client.session.cookies) == 0
    client.session.cookies.set(
        "session", "test", domain="internal-api-lark-api.feishu.cn", path="/"
    )
    client.close()
    assert len(client.session.cookies) == 0 and not client.authenticated


@pytest.mark.parametrize("code", [302, 401, 403, 429, 500])
def test_access_error_never_follows_or_retries(code):
    client = FeishuClient()
    response = Mock(status_code=code)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    with patch.object(client.session, "request", return_value=response) as request:
        with pytest.raises(AdapterError, match="http_status_" + str(code)):
            client._request("GET", API + "/settings/v3/")
        assert request.call_count == 1
        assert request.call_args.kwargs["allow_redirects"] is False
    client.close()


def test_gateway_other_commands_forbidden():
    client = FeishuClient()
    with pytest.raises(AdapterError, match="gateway_command_rejected"):
        client._request("POST", API + "/im/gateway/", headers={"x-command": "1"})
    client.close()


def test_filter_settings_before_strict_template_validation():
    client = FeishuClient()
    client.authenticated = True
    settings = {
        "meta": {"complete": True},
        "data": {
            "resource_url_tpl": {
                "fs_unit_tpl": {
                    "chosen-cdn": {
                        "hosts": ["https://a.feishucdn.com"],
                        "path_tpl": "/{{key}}",
                    },
                    "unrelated-unit": {"hosts": [], "path_tpl": ""},
                }
            }
        },
    }
    with patch.object(client, "_json", return_value=(settings, "")):
        config = client.resource_config(
            {"stickers": [{"image": {"origin": {"fsUnit": "chosen-cdn"}}}]}
        )
        assert list(config["resource_url_tpl"]["fs_unit_tpl"]) == ["chosen-cdn"]
    client.close()


def test_login_from_fresh_qr_then_authenticate():
    client = FeishuClient()
    images = []

    def json_request(method, url, **kwargs):
        if url.endswith("/init"):
            return {
                "code": 0,
                "data": {"step_info": {"token": "SYNTHETIC_QR"}},
            }, "SYNTHETIC_FLOW"
        if url.endswith("/polling"):
            client.session.cookies.set(
                "session",
                "SYNTHETIC_SESSION",
                domain="internal-api-lark-api.feishu.cn",
                path="/",
            )
            return {"code": 0, "data": {"step_info": {"status": 2}}}, ""
        return {"code": 0, "data": {"user": {"id": "1234"}}}, ""

    with (
        patch.object(client, "_json", side_effect=json_request) as request,
        patch("feishu_adapter.client.time.sleep"),
    ):
        client.login(images.append, lambda: False)
    assert client.authenticated and len(images) == 1 and b"<svg" in images[0]
    assert request.call_count == 3
    client.close()


@pytest.mark.parametrize(
    "status,reason",
    [
        (3, "qr_cancelled"),
        (4, "qr_error"),
        (5, "qr_expired"),
        (0, "login_extra_step_required"),
    ],
)
def test_qr_stop_requires_new_explicit_run(status, reason):
    client = FeishuClient()
    replies = [
        (
            {"code": 0, "data": {"step_info": {"token": "SYNTHETIC_QR"}}},
            "SYNTHETIC_FLOW",
        ),
        ({"code": 0, "data": {"step_info": {"status": status}}}, ""),
    ]
    with (
        patch.object(client, "_json", side_effect=replies) as request,
        patch("feishu_adapter.client.time.sleep"),
    ):
        with pytest.raises(AdapterError, match=reason):
            client.login(lambda svg: None, lambda: False)
        assert request.call_count == 2
    client.close()
