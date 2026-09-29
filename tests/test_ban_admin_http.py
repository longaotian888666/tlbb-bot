from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tlbb_bot.ban_admin_login import BanAdminHTTPError, UrllibHttpClient  # noqa: E402


class RecordingHTTPServer(ThreadingHTTPServer):
    records: list[dict[str, Any]]


class RecordingHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return None

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        parsed = urllib.parse.urlparse(self.path)
        server = cast(RecordingHTTPServer, self.server)
        server.records.append(
            {
                "method": self.command,
                "path": parsed.path,
                "query": urllib.parse.parse_qs(parsed.query),
                "headers": dict(self.headers),
                "body": body,
            }
        )

        if parsed.path == "/error-json":
            self._respond(
                403,
                {"code": 403, "msg": "登录失效", "token": "must-not-leak"},
            )
            return
        if parsed.path == "/error-text":
            payload = b"forbidden"
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        headers = {"Set-Cookie": "sid=cookie-123; Path=/"} if parsed.path == "/set-cookie" else {}
        self._respond(200, {"code": 0, "data": {"ok": True}}, headers)

    def _respond(
        self,
        status: int,
        payload: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


class UrllibHttpClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = RecordingHTTPServer(("127.0.0.1", 0), RecordingHandler)
        self.server.records = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = cast(tuple[str, int], self.server.server_address)
        self.base_url = f"http://{host}:{port}"
        self.client = UrllibHttpClient()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_get_encodes_query_and_sends_auth_header_without_body(self) -> None:
        response = self.client.request_json(
            f"{self.base_url}/echo?fixed=1",
            "get",
            {"page": 2, "account": "a b"},
            {"x-token": "token-value"},
            5,
        )

        self.assertEqual(response, {"code": 0, "data": {"ok": True}})
        record = self.server.records[-1]
        self.assertEqual(record["method"], "GET")
        self.assertEqual(record["query"], {"fixed": ["1"], "page": ["2"], "account": ["a b"]})
        self.assertEqual(record["body"], b"")
        self.assertEqual(record["headers"]["X-Token"], "token-value")

    def test_put_sends_utf8_json_body(self) -> None:
        payload = {"ID": 10001, "account": "测试账号", "passwd": "new-password"}

        self.client.request_json(
            f"{self.base_url}/game-user",
            "put",
            payload,
            {"x-token": "token-value"},
            5,
        )

        record = self.server.records[-1]
        self.assertEqual(record["method"], "PUT")
        self.assertEqual(json.loads(record["body"].decode("utf-8")), payload)
        self.assertEqual(record["headers"]["Content-Type"], "application/json;charset=UTF-8")

    def test_cookiejar_captures_and_reuses_cookie(self) -> None:
        self.client.post_json(f"{self.base_url}/set-cookie", {}, 5)
        self.client.request_json(f"{self.base_url}/echo", "GET", None, {}, 5)

        cookies = {cookie.name: cookie.value for cookie in self.client.cookie_jar}
        self.assertEqual(cookies, {"sid": "cookie-123"})
        self.assertEqual(self.server.records[-1]["headers"]["Cookie"], "sid=cookie-123")

    def test_http_error_is_wrapped_and_sensitive_fields_are_redacted(self) -> None:
        with self.assertRaises(BanAdminHTTPError) as caught:
            self.client.request_json(f"{self.base_url}/error-json", "GET", None, {}, 5)

        error = caught.exception
        self.assertEqual(error.status, 403)
        self.assertEqual(error.response["code"], 403)
        self.assertEqual(error.response["token"], "[REDACTED]")
        self.assertNotIn("must-not-leak", str(error))

    def test_non_json_http_error_has_empty_response(self) -> None:
        with self.assertRaises(BanAdminHTTPError) as caught:
            self.client.request_json(f"{self.base_url}/error-text", "GET", None, {}, 5)

        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(caught.exception.response, {})


if __name__ == "__main__":
    unittest.main()
