import json
from types import SimpleNamespace

import pytest

from feishu_adapter.adapter import AdapterError
from wecom_adapter import _http as http
from wecom_adapter import client


def test_unsupported_platform_does_not_inspect_processes(monkeypatch):
    monkeypatch.setattr(client.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        client.subprocess, "run", lambda *a, **k: pytest.fail("process access")
    )
    with pytest.raises(AdapterError, match="unsupported_platform"):
        client.preflight()


def test_real_lldb_reader_initialization_without_attaching():
    import platform
    import shutil
    import subprocess

    if platform.system() != "Darwin" or not shutil.which("lldb"):
        pytest.skip("Apple LLDB unavailable")
    # Run the production initialization, omit only the final command that attaches.
    command = client.reader_command(shutil.which("lldb"), 0, 0)
    result = subprocess.run(command[:-2], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_account_change_stops_before_second_attach(monkeypatch):
    identities = iter([("lldb", (1, "a")), ("lldb", (2, "b"))])
    monkeypatch.setattr(client, "preflight", lambda: next(identities))
    calls = []
    monkeypatch.setattr(client, "read_snapshot", lambda *a: calls.append(a) or [])
    reader = client.WeComClient()
    assert reader.snapshot(lambda: False) == []
    with pytest.raises(AdapterError, match="client_changed"):
        reader.snapshot(lambda: False)
    assert len(calls) == 1


def test_running_reader_and_cancel_do_not_start_another_debugger(monkeypatch):
    monkeypatch.setattr(client, "_HELPER", SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(
        client.subprocess, "Popen", lambda *a, **k: pytest.fail("overlapping attach")
    )
    with pytest.raises(AdapterError, match="reader_busy"):
        client.read_snapshot("lldb", 1, lambda: False)
    monkeypatch.setattr(client, "_HELPER", None)
    with pytest.raises(AdapterError, match="cancelled"):
        client.read_snapshot("lldb", 1, lambda: True)


def test_pipe_transfer_is_private_and_reports_fixed_errors(monkeypatch):
    import os

    def popen(args, **kw):
        assert kw["stdout"] == kw["stderr"] == client.subprocess.DEVNULL
        assert kw["stdin"] == client.subprocess.DEVNULL
        assert "--no-lldbinit" in args
        writer = kw["pass_fds"][0]
        os.write(writer, json.dumps({"error": "attach_rejected"}).encode())
        return SimpleNamespace(wait=lambda **kw: 0, poll=lambda: 0)

    monkeypatch.setattr(client, "_HELPER", None)
    monkeypatch.setattr(client.subprocess, "Popen", popen)
    with pytest.raises(AdapterError, match="attach_rejected"):
        client.read_snapshot("lldb", 1, lambda: False)


@pytest.mark.parametrize(
    "status,headers,body,error",
    [
        (
            302,
            {"Location": "https://evil.invalid/private"},
            b"",
            "url_outside_allowlist",
        ),
        (200, {"Content-Length": "100"}, b"tiny", "content_length_limit"),
        (200, {"Content-Length": "10"}, b"tiny", "truncated_body"),
        (200, {"Content-Encoding": "gzip"}, b"tiny", "unexpected_encoding"),
        (403, {}, b"", "http_status_403"),
    ],
)
def test_http_boundaries(monkeypatch, status, headers, body, error):
    import io

    stream = io.BytesIO(body)
    response = SimpleNamespace(status=status, getheader=headers.get, read=stream.read)
    calls = []
    conn = SimpleNamespace(
        connect=lambda: None,
        sock=SimpleNamespace(
            getpeername=lambda: ("1.1.1.1", 443), settimeout=lambda v: None
        ),
        request=lambda *a, **k: calls.append((a, k)),
        getresponse=lambda: response,
        close=lambda: calls.append("closed"),
    )
    monkeypatch.setattr(http.http.client, "HTTPSConnection", lambda *a, **k: conn)
    with pytest.raises(AdapterError, match=error):
        http.fetch("https://wework.qpic.cn/only-approved", 20, lambda: False)
    assert calls[-1] == "closed"
    assert set(calls[0][1]["headers"]) == {
        "User-Agent",
        "Accept",
        "Accept-Encoding",
        "Connection",
    }
