from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tlbb_bot.ban_admin_login import (  # noqa: E402
    BanAdminAPIError,
    BanAdminClient,
    BanAdminConfig,
    parse_fuzzy_role_matches,
)


class BanAdminDomainTests(unittest.TestCase):
    def make_client(self) -> BanAdminClient:
        return BanAdminClient(
            BanAdminConfig(
                login_url="https://admin.example.test/#/login",
                username="admin",
                password="pass",
                ocr_url="http://127.0.0.1:8000/ocr6",
            ),
            http_client=cast(Any, object()),
        )

    def test_role_action_methods_build_validated_payloads_without_write_retry(self) -> None:
        cases = [
            ("mute_role", "1"),
            ("unmute_role", "2"),
            ("ban_role", "3"),
            ("unban_role", "4"),
        ]
        for method_name, expected_channel in cases:
            with self.subTest(method=method_name):
                client = self.make_client()
                responses = [
                    {"code": 0, "data": {"userInfo": {"ID": 66, "userName": "admin"}}},
                    {
                        "code": 0,
                        "data": {"list": [{"ID": 28, "configName": "测试区服"}]},
                    },
                    {"code": 0, "msg": "ok"},
                ]
                with patch.object(client, "request_json", side_effect=responses) as request:
                    result = getattr(client, method_name)(
                        role_name="测试角色",
                        server_id=28,
                        expected_server_name="测试区服",
                        expected_agent_id=66,
                    )

                self.assertEqual(result["code"], 0)
                payload = request.call_args_list[-1].args[2]
                self.assertEqual(
                    payload,
                    {
                        "agentid": 66,
                        "agent": "admin",
                        "serverid": 28,
                        "servername": "测试区服",
                        "channel": expected_channel,
                        "name": "测试角色",
                        "noticeId": "",
                        "beginTime": "",
                        "endTime": "",
                        "rollingContent": "",
                        "frequency": "",
                    },
                )
                self.assertEqual(request.call_args_list[-1].kwargs, {"retry_on_auth_expired": False})

    def test_role_action_rejects_wrong_server_name_before_write(self) -> None:
        client = self.make_client()
        responses = [
            {"code": 0, "data": {"userInfo": {"ID": 66, "userName": "admin"}}},
            {"code": 0, "data": {"list": [{"ID": 28, "configName": "真实区服"}]}},
        ]
        with patch.object(client, "request_json", side_effect=responses) as request:
            with self.assertRaisesRegex(ValueError, "name mismatch"):
                client.ban_role(
                    role_name="测试角色",
                    server_id=28,
                    expected_server_name="错误区服",
                )

        self.assertEqual(request.call_count, 2)

    def test_find_similar_roles_builds_validated_payload_and_parses_matches(self) -> None:
        client = self.make_client()
        responses = [
            {
                "code": 0,
                "data": {"userInfo": {"ID": 66, "userName": "facai_wanmei"}},
            },
            {
                "code": 0,
                "data": {"list": [{"ID": 29, "configName": "二十九区"}]},
            },
            {
                "code": 0,
                "data": {},
                "msg": (
                    "如果过长,请复制到文本搜索查看:"
                    "角色名：念．γ, 账号:DD20260626; "
                    "角色名:念γ，账号：DD20260627；"
                ),
            },
        ]
        with patch.object(client, "request_json", side_effect=responses) as request:
            matches = client.find_similar_roles(
                role_name="念．γ",
                server_id=29,
                expected_server_name="二十九区",
                expected_agent_id=66,
            )

        self.assertEqual(
            matches,
            [
                {"roleName": "念．γ", "account": "DD20260626"},
                {"roleName": "念γ", "account": "DD20260627"},
            ],
        )
        self.assertEqual(
            request.call_args_list[-1],
            call(
                "POST",
                "/gameOrder/getFuzNames",
                {
                    "agentid": 66,
                    "agent": "facai_wanmei",
                    "serverid": 29,
                    "servername": "二十九区",
                    "channel": "1",
                    "name": "念．γ",
                    "noticeId": "",
                    "beginTime": "",
                    "endTime": "",
                    "rollingContent": "",
                    "frequency": "",
                },
                retry_on_auth_expired=True,
            ),
        )

    def test_parse_fuzzy_role_matches_returns_empty_and_deduplicates(self) -> None:
        self.assertEqual(parse_fuzzy_role_matches({"code": 0, "msg": "没有找到近似角色"}), [])
        self.assertEqual(
            parse_fuzzy_role_matches(
                {
                    "code": 0,
                    "msg": (
                        "角色名：同名角色, 账号:account-1; "
                        "角色名：同名角色, 账号:account-1;"
                    ),
                }
            ),
            [{"roleName": "同名角色", "account": "account-1"}],
        )

    def test_list_game_users_hides_password_by_default(self) -> None:
        client = self.make_client()
        response = {
            "code": 0,
            "data": {
                "list": [
                    {
                        "ID": 10001,
                        "account": "game-user",
                        "passwd": "secret-password",
                        "closed": 0,
                    }
                ]
            },
        }
        with patch.object(client, "request_json", return_value=response):
            safe_rows = client.list_game_users(account="game-user")

        self.assertNotIn("passwd", safe_rows[0])

    def test_find_game_users_by_account_paginates_filters_exactly_and_deduplicates(self) -> None:
        client = self.make_client()
        responses = [
            {
                "code": 0,
                "data": {
                    "count": 3,
                    "list": [
                        {"ID": 1, "account": "target-extra", "passwd": "hidden"},
                        {"ID": 2, "account": "Target", "passwd": "hidden"},
                    ],
                },
            },
            {
                "code": 0,
                "data": {
                    "count": 3,
                    "list": [
                        {"ID": 2, "account": "target", "passwd": "hidden"},
                    ],
                },
            },
        ]
        with patch.object(client, "request_json", side_effect=responses) as request:
            rows = client.find_game_users_by_account("target", page_size=2)

        self.assertEqual(rows, [{"ID": 2, "account": "Target"}])
        self.assertEqual(request.call_args_list[0].args[2]["page"], 1)
        self.assertEqual(request.call_args_list[1].args[2]["page"], 2)

    def test_find_game_user_by_account_rejects_ambiguous_exact_matches(self) -> None:
        client = self.make_client()
        response = {
            "code": 0,
            "data": {
                "count": 2,
                "list": [
                    {"ID": 10, "account": "same-account"},
                    {"ID": 11, "account": "SAME-ACCOUNT"},
                ],
            },
        }
        with patch.object(client, "request_json", return_value=response):
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                client.find_game_user_by_account("same-account")

    def test_get_game_user_password_by_account_returns_repr_safe_credential(self) -> None:
        client = self.make_client()
        responses = [
            {
                "code": 0,
                "data": {
                    "count": 1,
                    "list": [{"ID": 10001, "account": "game-user", "passwd": "list-secret"}],
                },
            },
            {
                "code": 0,
                "data": {
                    "regameUsers": {
                        "ID": 10001,
                        "account": "game-user",
                        "passwd": "detail-secret",
                    }
                },
            },
        ]
        with patch.object(client, "request_json", side_effect=responses):
            credential = client.get_game_user_password_by_account("game-user")

        self.assertEqual(credential.password, "detail-secret")
        self.assertNotIn("detail-secret", repr(credential))

    def test_get_game_user_password_by_account_rechecks_detail_account(self) -> None:
        client = self.make_client()
        responses = [
            {
                "code": 0,
                "data": {
                    "count": 1,
                    "list": [{"ID": 10001, "account": "game-user"}],
                },
            },
            {
                "code": 0,
                "data": {
                    "regameUsers": {
                        "ID": 10001,
                        "account": "different-user",
                        "passwd": "secret",
                    }
                },
            },
        ]
        with patch.object(client, "request_json", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                client.get_game_user_password_by_account("game-user")

    def test_find_game_user_and_explicit_password_accessor(self) -> None:
        client = self.make_client()
        response = {
            "code": 0,
            "data": {
                "regameUsers": {
                    "ID": 10001,
                    "account": "game-user",
                    "passwd": "secret-password",
                    "closed": 0,
                }
            },
        }
        with patch.object(client, "request_json", return_value=response):
            safe_row = client.find_game_user(10001)
            password = client.get_game_user_password(10001)

        self.assertNotIn("passwd", safe_row)
        self.assertEqual(password, "secret-password")

    def test_find_game_user_rejects_mismatched_response_id(self) -> None:
        client = self.make_client()
        response = {
            "code": 0,
            "data": {
                "regameUsers": {
                    "ID": 10002,
                    "account": "wrong-user",
                    "passwd": "secret-password",
                }
            },
        }
        with patch.object(client, "request_json", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "expected 10001"):
                client.find_game_user(10001)

    def test_update_game_user_password_preserves_source_and_disables_retry(self) -> None:
        client = self.make_client()
        source_row = {
            "ID": 10001,
            "account": "game-user",
            "passwd": "old-password",
            "closed": 0,
            "belong": "group-a",
            "agentId": 66,
            "CreatedAt": "must-not-be-sent",
        }
        responses = [
            {"code": 0, "data": {"regameUsers": dict(source_row)}},
            {"code": 0, "msg": "updated"},
        ]
        with patch.object(client, "request_json", side_effect=responses) as request:
            result = client.update_game_user_password(
                10001,
                "new-password",
                expected_account="game-user",
            )

        self.assertEqual(source_row["passwd"], "old-password")
        update_call = request.call_args_list[1]
        self.assertEqual(update_call.args[0:2], ("PUT", "/gameUsers/updateGameUsers"))
        self.assertEqual(update_call.args[2]["passwd"], "new-password")
        self.assertEqual(update_call.args[2]["closed"], 0)
        self.assertNotIn("CreatedAt", update_call.args[2])
        self.assertEqual(update_call.kwargs, {"retry_on_auth_expired": False})
        self.assertNotIn("password", result)
        self.assertNotIn("passwd", result)

    def test_update_game_user_password_requires_complete_safe_update_fields(self) -> None:
        client = self.make_client()
        response = {
            "code": 0,
            "data": {
                "regameUsers": {
                    "ID": 10001,
                    "account": "game-user",
                    "passwd": "old-password",
                }
            },
        }
        with patch.object(client, "request_json", return_value=response) as request:
            with self.assertRaisesRegex(RuntimeError, "missing update fields"):
                client.update_game_user_password(
                    10001,
                    "new-password",
                    expected_account="game-user",
                )

        request.assert_called_once()

    def test_reset_admin_password_requires_bound_confirmation_and_rejects_self(self) -> None:
        client = self.make_client()
        with patch.object(client, "request_json") as request:
            with self.assertRaisesRegex(ValueError, "confirmation"):
                client.reset_admin_password_to_default(77, confirmation="yes")
        request.assert_not_called()

        current_user_response = {
            "code": 0,
            "data": {"userInfo": {"ID": 66, "userName": "admin"}},
        }
        client = self.make_client()
        with patch.object(client, "request_json", return_value=current_user_response):
            with self.assertRaisesRegex(ValueError, "current backend user"):
                client.reset_admin_password_to_default(
                    66,
                    confirmation="RESET ADMIN 66 TO 123456",
                )

    def test_reset_admin_password_disables_retry_and_returns_no_default_password(self) -> None:
        client = self.make_client()
        responses = [
            {"code": 0, "data": {"userInfo": {"ID": 66, "userName": "admin"}}},
            {"code": 0, "msg": "reset"},
        ]
        with patch.object(client, "request_json", side_effect=responses) as request:
            result = client.reset_admin_password_to_default(
                77,
                confirmation="RESET ADMIN 77 TO 123456",
            )

        self.assertEqual(
            request.call_args_list[-1],
            call(
                "POST",
                "/user/resetPassword",
                {"ID": 77},
                retry_on_auth_expired=False,
            ),
        )
        self.assertNotIn("123456", repr(result))
        self.assertTrue(result["requiresImmediateRotation"])

    def test_domain_method_raises_on_nonzero_business_code(self) -> None:
        client = self.make_client()
        with patch.object(
            client,
            "request_json",
            return_value={"code": 500, "msg": "backend failed"},
        ):
            with self.assertRaises(BanAdminAPIError):
                client.get_servers()

    def test_boolean_ids_are_rejected(self) -> None:
        client = self.make_client()
        with self.assertRaisesRegex(ValueError, "positive integer"):
            client.find_game_user(True)


if __name__ == "__main__":
    unittest.main()
