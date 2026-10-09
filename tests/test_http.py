"""Synthetic mocks only: no network, existing credentials, or private files."""

import contextlib
import io
import os
import unittest
from unittest.mock import Mock, patch

import requests

from feishu_adapter._http import (
    MAX_RESOURCE_BYTES,
    TIMEOUT,
    ResourceHTTPError,
    fetch_resource,
)

URL = "https://cdn.feishucdn.com/resource/synthetic?signature=PRIVATE_SENTINEL"


def response(status=200, chunks=(b"synthetic",), headers=None):
    value = Mock()
    value.status_code = status
    value.headers = headers or {}
    value.__enter__ = Mock(return_value=value)
    value.__exit__ = Mock(return_value=False)
    value.iter_content = Mock(return_value=iter(chunks))
    return value


class ResourceHTTPTests(unittest.TestCase):
    def setUp(self):
        # A missing HTTP mock must fail rather than establish a real connection.
        guard = patch(
            "socket.create_connection", side_effect=AssertionError("network_forbidden")
        )
        guard.start()
        self.addCleanup(guard.stop)

    def test_prepared_request_carries_no_cookie_auth_or_environment_proxy(self):
        sent = []

        def send(session, prepared, **kwargs):
            sent.append(prepared)
            self.assertFalse(session.trust_env)
            self.assertIsNone(session.auth)
            for name in ("Cookie", "Authorization", "Proxy-Authorization", "Referer"):
                self.assertNotIn(name, prepared.headers)
            self.assertEqual(prepared.url, URL)
            self.assertEqual(kwargs["proxies"], {})
            self.assertFalse(kwargs["allow_redirects"])
            self.assertTrue(kwargs["stream"])
            self.assertEqual(kwargs["timeout"], TIMEOUT)
            self.assertEqual(len(session.cookies), 0)
            return response()

        with (
            patch.dict(
                os.environ,
                {
                    "NETRC": "/must/not/be/read",
                    "HTTPS_PROXY": "http://user:PRIVATE_SENTINEL@localhost:9999",
                },
            ),
            patch(
                "requests.sessions.get_netrc_auth",
                side_effect=AssertionError("netrc_forbidden"),
            ) as netrc,
            patch.object(requests.Session, "send", new=send),
        ):
            self.assertEqual(fetch_resource(URL, [URL]), b"synthetic")
            netrc.assert_not_called()
        self.assertEqual(len(sent), 1)

    def test_fresh_session_each_time_drops_even_polluted_factory_state(self):
        original_session = requests.Session
        sessions = []

        def factory():
            session = original_session()
            session.cookies.set(
                "session", "PRIVATE_SENTINEL", domain="cdn.feishucdn.com", path="/"
            )
            session.headers.update(
                {"Cookie": "PRIVATE_SENTINEL", "Authorization": "PRIVATE_SENTINEL"}
            )
            session.auth = ("synthetic", "PRIVATE_SENTINEL")
            sessions.append(session)
            return session

        def send(session, prepared, **kwargs):
            self.assertNotIn("Cookie", prepared.headers)
            self.assertNotIn("Authorization", prepared.headers)
            # A CDN cannot cause this session's cookie jar to retain a new cookie.
            self.assertFalse(session.cookies._policy.set_ok(None, None))
            self.assertFalse(session.cookies._policy.return_ok(None, None))
            return response()

        with (
            patch("feishu_adapter._http.requests.Session", side_effect=factory),
            patch.object(original_session, "send", new=send),
        ):
            fetch_resource(URL, {URL})
            fetch_resource(URL, {URL})
        self.assertEqual(len(sessions), 2)
        self.assertIsNot(sessions[0], sessions[1])
        self.assertTrue(all(len(session.cookies) == 0 for session in sessions))

    def test_exact_url_approval_is_required(self):
        with patch("feishu_adapter._http.requests.Session") as factory:
            for allowed in ([], [URL + "x"], URL, {URL: True}):
                with (
                    self.subTest(allowed_type=type(allowed).__name__),
                    self.assertRaises(ResourceHTTPError) as caught,
                ):
                    fetch_resource(URL, allowed)
                self.assertEqual(caught.exception.code, "resource_url_not_approved")
            factory.assert_not_called()

    def test_allowlist_cannot_override_host_or_transport_boundary(self):
        urls = [
            "http://cdn.feishucdn.com/r",
            "https://feishucdn.com.evil.example/r",
            "https://notfeishucdn.com/r",
            "https://cdn.feishucdn.com:444/r",
            "https://u:p@cdn.feishucdn.com/r",
            "https://@cdn.feishucdn.com/r",
            "https://127.0.0.1/r",
            "https://cdn.feishucdn.com./r",
            "https://cdn.feishucdn.com/r#",
            "https://cdn.feishucdn.com\\@evil.example/r",
            "https://cdn.feishucdn.com/r\n",
            "https://-bad.feishucdn.com/r",
        ]
        with patch("feishu_adapter._http.requests.Session") as factory:
            for url in urls:
                with (
                    self.subTest(url=url),
                    self.assertRaises(ResourceHTTPError) as caught,
                ):
                    fetch_resource(url, [url])
                self.assertTrue(caught.exception.stop)
            factory.assert_not_called()

    def test_access_denial_and_redirects_stop_without_reading_error_body(self):
        for status in (401, 403, 429, 301, 302, 307, 308):
            fake = response(
                status=status,
                chunks=(b"PRIVATE_SENTINEL",),
                headers={"Location": "https://evil.example/PRIVATE_SENTINEL"},
            )
            with (
                patch.object(requests.Session, "send", return_value=fake) as send,
                self.assertRaises(ResourceHTTPError) as caught,
            ):
                fetch_resource(URL, [URL])
            self.assertTrue(caught.exception.stop)
            self.assertEqual(caught.exception.http_status, status)
            self.assertNotIn("PRIVATE_SENTINEL", str(caught.exception))
            self.assertEqual(send.call_count, 1)
            fake.iter_content.assert_not_called()

    def test_non_200_including_partial_content_is_not_a_file(self):
        for status in (204, 206, 404, 500):
            fake = response(status=status)
            with (
                patch.object(requests.Session, "send", return_value=fake),
                self.assertRaises(ResourceHTTPError) as caught,
            ):
                fetch_resource(URL, [URL])
            self.assertEqual(caught.exception.code, "resource_http_status")
            fake.iter_content.assert_not_called()

    def test_declared_and_streamed_sizes_are_bounded(self):
        cases = [
            response(headers={"Content-Length": "11"}),
            response(chunks=(b"123456", b"78901")),
            response(chunks=(b"12345678901",), headers={"Content-Encoding": "gzip"}),
        ]
        for fake in cases:
            with (
                patch.object(requests.Session, "send", return_value=fake),
                self.assertRaises(ResourceHTTPError) as caught,
            ):
                fetch_resource(URL, [URL], max_bytes=10)
            self.assertEqual(caught.exception.code, "resource_size_limit")
            self.assertTrue(caught.exception.stop)
        cases[0].iter_content.assert_not_called()
        with patch.object(
            requests.Session, "send", return_value=response(chunks=(b"12345", b"67890"))
        ):
            self.assertEqual(fetch_resource(URL, [URL], max_bytes=10), b"1234567890")

    def test_invalid_limits_fail_before_network(self):
        with patch("feishu_adapter._http.requests.Session") as factory:
            for limit in (0, -1, True, "50", MAX_RESOURCE_BYTES + 1):
                with self.assertRaises(ResourceHTTPError):
                    fetch_resource(URL, [URL], max_bytes=limit)
            factory.assert_not_called()

    def test_network_exceptions_are_fixed_codes_with_no_output(self):
        for original, code in [
            (requests.Timeout("PRIVATE_SENTINEL"), "resource_timeout"),
            (requests.ConnectionError(URL), "resource_network_error"),
        ]:
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch.object(requests.Session, "send", side_effect=original),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
                self.assertRaises(ResourceHTTPError) as caught,
            ):
                fetch_resource(URL, [URL])
            self.assertEqual(str(caught.exception), code)
            self.assertTrue(caught.exception.__suppress_context__)
            self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
