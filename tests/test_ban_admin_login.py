from __future__ import annotations

import http.cookiejar
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tlbb_bot.ban_admin_login import (  # noqa: E402
    BanAdminAPIError,
    BanAdminHTTPError,
    BanAdminClient,
    BanAdminConfig,
    api_code,
    build_api_url,
    build_login_payload,
    captcha_image_for_ocr,
    load_config,
    parse_ocr_text,
    response_requires_relogin,
)


class FakeHttpClient:
    def __init__(
        self,
        *,
        login_response: dict[str, object] | None = None,
        login_tokens: list[str] | None = None,
        request_responses: list[dict[str, object] | Exception] | None = None,
    ) -> None:
        self.cookie_jar = http.cookiejar.CookieJar()
        self.calls: list[tuple[object, ...]] = []
        self.login_response = login_response
        self.login_tokens = login_tokens or ["token-value"]
        self.login_count = 0
        self.request_responses = list(request_responses or [])

    def post_json(self, url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
        self.calls.append(("json", url, dict(payload)))
        if url == "https://admin.example.test/api/base/captcha":
            return {
                "code": 0,
                "data": {
                    "captchaId": "captcha-id-123",
                    "picPath": "data:image/png;base64,QUJDRA==",
                },
            }
        if url == "https://admin.example.test/api/base/login":
            if self.login_response is not None:
                return self.login_response
            token_index = min(self.login_count, len(self.login_tokens) - 1)
            token = self.login_tokens[token_index]
            self.login_count += 1
            return {
                "code": 0,
                "data": {
                    "token": token,
                    "user": {
                        "ID": 66,
                        "userName": "admin",
                        "nickName": "ops",
                    },
                },
            }
        raise AssertionError(f"unexpected json url: {url}")

    def post_form(self, url: str, payload: dict[str, str], timeout: float) -> dict[str, object]:
        self.calls.append(("form", url, dict(payload)))
        self.assert_ocr_payload(payload)
        return {"code": 200, "data": ["2468"]}

    def assert_ocr_payload(self, payload: dict[str, str]) -> None:
        assert payload == {
            "image": "QUJDRA==",
            "probability": "false",
            "png_fix": "false",
            "charsets": "0123456789",
        }

    def request_json(
        self,
        url: str,
        method: str,
        payload: dict[str, object] | None,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, object]:
        self.calls.append(("request", method.upper(), url, dict(payload or {}), dict(headers)))
        if self.request_responses:
            response = self.request_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return {"code": 0, "data": {"ok": True}}


class BanAdminLoginTests(unittest.TestCase):
    def test_load_config_accepts_ascii_and_chinese_env_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "封禁账号后台登陆地址：https://admin.example.test/login",
                        "账号 = admin_user",
                        "密码=secret-value",
                        "BAN_ADMIN_OCR_URL=http://127.0.0.1:8000/ocr6",
                    ]
                ),
                encoding="utf-8-sig",
            )

            config = load_config(env_path)

        self.assertEqual(config.login_url, "https://admin.example.test/login")
        self.assertEqual(config.username, "admin_user")
        self.assertEqual(config.password, "secret-value")
        self.assertEqual(config.ocr_url, "http://127.0.0.1:8000/ocr6")

    def test_build_api_url_uses_login_origin(self) -> None:
        self.assertEqual(
            build_api_url("https://admin.example.test/#/login", "/api/base/login"),
            "https://admin.example.test/api/base/login",
        )

    def test_captcha_image_for_ocr_strips_data_url_prefix(self) -> None:
        self.assertEqual(captcha_image_for_ocr("data:image/png;base64,QUJDRA=="), "QUJDRA==")

    def test_parse_ocr_text_accepts_ocr6_list_or_string_response(self) -> None:
        self.assertEqual(parse_ocr_text({"code": 200, "data": ["1234"]}), "1234")
        self.assertEqual(parse_ocr_text({"code": 200, "data": "5678"}), "5678")

    def test_parse_ocr_text_rejects_failed_or_empty_response(self) -> None:
        with self.assertRaisesRegex(ValueError, "OCR"):
            parse_ocr_text({"code": 500, "msg": "model missing"})
        with self.assertRaisesRegex(ValueError, "空验证码"):
            parse_ocr_text({"code": 200, "data": [""]})

    def test_build_login_payload_uses_backend_field_names(self) -> None:
        self.assertEqual(
            build_login_payload("admin", "pass", "captcha-id", "2468"),
            {
                "username": "admin",
                "password": "pass",
                "captcha": "2468",
                "captchaId": "captcha-id",
            },
        )

    def test_api_code_preserves_zero_success_code(self) -> None:
        self.assertEqual(api_code({"code": 0}, default=-1), 0)

    def test_package_import_does_not_eagerly_import_cli_module(self) -> None:
        sys.modules.pop("tlbb_bot", None)
        sys.modules.pop("tlbb_bot.ban_admin_login", None)

        package = importlib.import_module("tlbb_bot")

        self.assertNotIn("tlbb_bot.ban_admin_login", sys.modules)
        self.assertEqual(package.BanAdminClient.__name__, "BanAdminClient")
        self.assertIn("tlbb_bot.ban_admin_login", sys.modules)

    def test_client_login_fetches_captcha_uses_ocr_submits_login_and_writes_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "ban_admin_session.json"
            fake_http = FakeHttpClient()
            client = BanAdminClient(
                BanAdminConfig(
                    login_url="https://admin.example.test/#/login",
                    username="admin",
                    password="pass",
                    ocr_url="http://127.0.0.1:8000/ocr6",
                ),
                http_client=fake_http,
                session_path=session_path,
            )

            session = client.login()

            self.assertEqual(session.token, "token-value")
            self.assertEqual(session.auth_headers(), {"x-token": "token-value"})
            self.assertEqual(
                fake_http.calls,
                [
                    ("json", "https://admin.example.test/api/base/captcha", {}),
                    (
                        "form",
                        "http://127.0.0.1:8000/ocr6",
                        {
                            "image": "QUJDRA==",
                            "probability": "false",
                            "png_fix": "false",
                            "charsets": "0123456789",
                        },
                    ),
                    (
                        "json",
                        "https://admin.example.test/api/base/login",
                        {
                            "username": "admin",
                            "password": "pass",
                            "captcha": "2468",
                            "captchaId": "captcha-id-123",
                        },
                    ),
                ],
            )
            saved = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["api_base"], "https://admin.example.test/api")
            self.assertEqual(saved["token"], "token-value")
            self.assertEqual(saved["token_header"], "x-token")
            self.assertEqual(saved["user"]["id"], 66)
            self.assertEqual(saved["user"]["username"], "admin")

    def test_client_login_rejects_success_response_without_token(self) -> None:
        fake_http = FakeHttpClient(login_response={"code": 0, "data": {"user": {"ID": 66}}})
        client = BanAdminClient(
            BanAdminConfig(
                login_url="https://admin.example.test/#/login",
                username="admin",
                password="pass",
                ocr_url="http://127.0.0.1:8000/ocr6",
            ),
            http_client=fake_http,
        )

        with self.assertRaisesRegex(ValueError, "data.token"):
            client.login()

    def test_client_request_json_reuses_login_http_client_and_auth_headers(self) -> None:
        fake_http = FakeHttpClient(request_responses=[{"code": 0, "data": {"list": []}}])
        client = BanAdminClient(
            BanAdminConfig(
                login_url="https://admin.example.test/#/login",
                username="admin",
                password="pass",
                ocr_url="http://127.0.0.1:8000/ocr6",
            ),
            http_client=fake_http,
        )

        response = client.request_json("GET", "/gameUsers/getGameUsersList", {"page": 1})

        self.assertEqual(response, {"code": 0, "data": {"list": []}})
        self.assertEqual(
            fake_http.calls[-1],
            (
                "request",
                "GET",
                "https://admin.example.test/api/gameUsers/getGameUsersList",
                {"page": 1},
                {"x-token": "token-value"},
            ),
        )

    def test_client_get_relogs_once_on_auth_expired_response(self) -> None:
        fake_http = FakeHttpClient(
            login_tokens=["token-1", "token-2"],
            request_responses=[
                {"code": 401, "msg": "token 已过期"},
                {"code": 0, "data": {"ok": True}},
            ],
        )
        client = BanAdminClient(
            BanAdminConfig(
                login_url="https://admin.example.test/#/login",
                username="admin",
                password="pass",
                ocr_url="http://127.0.0.1:8000/ocr6",
            ),
            http_client=fake_http,
        )

        response = client.request_json("GET", "/gameUsers/getGameUsersList", {"page": 1})

        self.assertEqual(response, {"code": 0, "data": {"ok": True}})
        login_calls = [
            call for call in fake_http.calls if call[0] == "json" and call[1] == "https://admin.example.test/api/base/login"
        ]
        request_calls = [call for call in fake_http.calls if call[0] == "request"]
        self.assertEqual(len(login_calls), 2)
        self.assertEqual(request_calls[0][4], {"x-token": "token-1"})
        self.assertEqual(request_calls[1][4], {"x-token": "token-2"})

    def test_get_servers_relogs_once_on_code_7_authorization_expired(self) -> None:
        fake_http = FakeHttpClient(
            login_tokens=["token-1", "token-2"],
            request_responses=[
                {"code": 7, "msg": "授权已过期"},
                {
                    "code": 0,
                    "data": {
                        "list": [{"ID": 29, "configName": "二十九区"}],
                    },
                },
            ],
        )
        client = BanAdminClient(
            BanAdminConfig(
                login_url="https://admin.example.test/#/login",
                username="admin",
                password="pass",
                ocr_url="http://127.0.0.1:8000/ocr6",
            ),
            http_client=fake_http,
        )

        servers = client.get_servers()

        self.assertEqual(servers, [{"ID": 29, "configName": "二十九区"}])
        login_calls = [
            call
            for call in fake_http.calls
            if call[0] == "json"
            and call[1] == "https://admin.example.test/api/base/login"
        ]
        request_calls = [call for call in fake_http.calls if call[0] == "request"]
        self.assertEqual(len(login_calls), 2)
        self.assertEqual(len(request_calls), 2)
        self.assertEqual(request_calls[0][4], {"x-token": "token-1"})
        self.assertEqual(request_calls[1][4], {"x-token": "token-2"})

    def test_get_servers_stops_after_one_authorization_expired_retry(self) -> None:
        fake_http = FakeHttpClient(
            login_tokens=["token-1", "token-2"],
            request_responses=[
                {"code": 7, "msg": "授权已过期"},
                {"code": 7, "msg": "授权已过期"},
            ],
        )
        client = BanAdminClient(
            BanAdminConfig(
                login_url="https://admin.example.test/#/login",
                username="admin",
                password="pass",
                ocr_url="http://127.0.0.1:8000/ocr6",
            ),
            http_client=fake_http,
        )

        with self.assertRaises(BanAdminAPIError) as caught:
            client.get_servers()

        login_calls = [
            call
            for call in fake_http.calls
            if call[0] == "json"
            and call[1] == "https://admin.example.test/api/base/login"
        ]
        request_calls = [call for call in fake_http.calls if call[0] == "request"]
        self.assertEqual(caught.exception.api_path, "/gameOrder/getServers")
        self.assertEqual(caught.exception.code, 7)
        self.assertEqual(len(login_calls), 2)
        self.assertEqual(len(request_calls), 2)

    def test_success_and_unrelated_expiry_messages_do_not_require_relogin(self) -> None:
        self.assertFalse(response_requires_relogin({"code": 0, "msg": "token updated successfully"}))
        self.assertFalse(response_requires_relogin({"code": 7, "msg": "活动已过期"}))
        self.assertFalse(response_requires_relogin({"code": 403, "msg": "无权限"}))
        self.assertTrue(response_requires_relogin({"code": 401, "msg": "未登录"}))
        self.assertTrue(response_requires_relogin({"code": 7, "msg": "token 已过期"}))
        self.assertTrue(response_requires_relogin({"code": 7, "msg": "授权已过期"}))

    def test_write_does_not_retry_code_7_authorization_expired_by_default(self) -> None:
        fake_http = FakeHttpClient(
            login_tokens=["token-1", "token-2"],
            request_responses=[{"code": 7, "msg": "授权已过期"}],
        )
        client = BanAdminClient(
            BanAdminConfig(
                login_url="https://admin.example.test/#/login",
                username="admin",
                password="pass",
                ocr_url="http://127.0.0.1:8000/ocr6",
            ),
            http_client=fake_http,
        )

        response = client.request_json(
            "POST",
            "/gameConfig/banUser",
            {"name": "role"},
        )

        self.assertEqual(response["code"], 7)
        request_calls = [call for call in fake_http.calls if call[0] == "request"]
        login_calls = [
            call
            for call in fake_http.calls
            if call[0] == "json"
            and call[1] == "https://admin.example.test/api/base/login"
        ]
        self.assertEqual(len(request_calls), 1)
        self.assertEqual(len(login_calls), 1)

    def test_client_write_methods_do_not_retry_auth_expired_by_default(self) -> None:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                fake_http = FakeHttpClient(
                    login_tokens=["token-1", "token-2"],
                    request_responses=[{"code": 401, "msg": "token 已过期"}],
                )
                client = BanAdminClient(
                    BanAdminConfig(
                        login_url="https://admin.example.test/#/login",
                        username="admin",
                        password="pass",
                        ocr_url="http://127.0.0.1:8000/ocr6",
                    ),
                    http_client=fake_http,
                )

                response = client.request_json(method, "/gameConfig/banUser", {"name": "role"})

                self.assertEqual(response["code"], 401)
                request_calls = [call for call in fake_http.calls if call[0] == "request"]
                login_calls = [
                    call
                    for call in fake_http.calls
                    if call[0] == "json" and call[1] == "https://admin.example.test/api/base/login"
                ]
                self.assertEqual(len(request_calls), 1)
                self.assertEqual(len(login_calls), 1)

    def test_client_write_can_retry_when_explicitly_enabled(self) -> None:
        fake_http = FakeHttpClient(
            login_tokens=["token-1", "token-2"],
            request_responses=[
                {"code": 401, "msg": "token 已过期"},
                {"code": 0, "data": {"ok": True}},
            ],
        )
        client = BanAdminClient(
            BanAdminConfig(
                login_url="https://admin.example.test/#/login",
                username="admin",
                password="pass",
                ocr_url="http://127.0.0.1:8000/ocr6",
            ),
            http_client=fake_http,
        )

        response = client.request_json(
            "POST",
            "/gameConfig/idempotent-write",
            {"value": 1},
            retry_on_auth_expired=True,
        )

        self.assertEqual(response, {"code": 0, "data": {"ok": True}})
        request_calls = [call for call in fake_http.calls if call[0] == "request"]
        self.assertEqual(len(request_calls), 2)

    def test_client_write_does_not_retry_http_401_by_default(self) -> None:
        fake_http = FakeHttpClient(
            login_tokens=["token-1", "token-2"],
            request_responses=[
                BanAdminHTTPError(
                    "https://admin.example.test/api/gameConfig/banUser",
                    401,
                    {"code": 401, "msg": "未登录"},
                )
            ],
        )
        client = BanAdminClient(
            BanAdminConfig(
                login_url="https://admin.example.test/#/login",
                username="admin",
                password="pass",
                ocr_url="http://127.0.0.1:8000/ocr6",
            ),
            http_client=fake_http,
        )

        with self.assertRaises(BanAdminHTTPError):
            client.request_json("POST", "/gameConfig/banUser", {"name": "role"})

        request_calls = [call for call in fake_http.calls if call[0] == "request"]
        login_calls = [
            call
            for call in fake_http.calls
            if call[0] == "json" and call[1] == "https://admin.example.test/api/base/login"
        ]
        self.assertEqual(len(request_calls), 1)
        self.assertEqual(len(login_calls), 1)

    def test_client_get_relogs_once_on_http_401(self) -> None:
        fake_http = FakeHttpClient(
            login_tokens=["token-1", "token-2"],
            request_responses=[
                BanAdminHTTPError(
                    "https://admin.example.test/api/user/getUserInfo",
                    401,
                    {"code": 401, "msg": "未登录"},
                ),
                {"code": 0, "data": {"ok": True}},
            ],
        )
        client = BanAdminClient(
            BanAdminConfig(
                login_url="https://admin.example.test/#/login",
                username="admin",
                password="pass",
                ocr_url="http://127.0.0.1:8000/ocr6",
            ),
            http_client=fake_http,
        )

        response = client.request_json("GET", "/user/getUserInfo")

        self.assertEqual(response, {"code": 0, "data": {"ok": True}})
        request_calls = [call for call in fake_http.calls if call[0] == "request"]
        self.assertEqual(len(request_calls), 2)

    def test_client_get_does_not_retry_permission_denied_http_403(self) -> None:
        fake_http = FakeHttpClient(
            request_responses=[
                BanAdminHTTPError(
                    "https://admin.example.test/api/user/getUserInfo",
                    403,
                    {"code": 403, "msg": "无权限"},
                )
            ]
        )
        client = BanAdminClient(
            BanAdminConfig(
                login_url="https://admin.example.test/#/login",
                username="admin",
                password="pass",
                ocr_url="http://127.0.0.1:8000/ocr6",
            ),
            http_client=fake_http,
        )

        with self.assertRaises(BanAdminHTTPError):
            client.request_json("GET", "/user/getUserInfo")

        request_calls = [call for call in fake_http.calls if call[0] == "request"]
        self.assertEqual(len(request_calls), 1)


if __name__ == "__main__":
    unittest.main()
