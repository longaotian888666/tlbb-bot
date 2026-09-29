from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tlbb_backend import (  # noqa: E402
    TLBBBackendClient,
    TLBBBackendError,
    response_requires_relogin,
)


class FakeResponse:
    def __init__(self, data: dict) -> None:
        self.data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.data


class FakeSession:
    def __init__(
        self,
        *,
        server_page_limit: int | None = None,
        ignore_game_filter: bool = False,
    ) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []
        self.server_page_limit = server_page_limit
        self.ignore_game_filter = ignore_game_filter

    def post(self, url: str, json: dict, timeout: int) -> FakeResponse:
        self.calls.append((url, dict(json)))
        if url.endswith("/admin/agent/login"):
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "token": "test-token",
                        "userInfo": {
                            "agentId": 7050,
                            "username": "current-agent",
                            "nickname": "Current Agent",
                            "legacy": {"oldAgentId": 6050},
                        },
                    },
                }
            )
        if url.endswith("/admin/user/list"):
            account = str(json["search"]["account"]).casefold()
            game_id = json["search"]["gameId"]
            if account == "missing-player":
                rows: list[dict] = []
            elif account == "duplicate-player":
                rows = [
                    self._player("Duplicate-Player", 7103, 1),
                    self._player("duplicate-player", 7104, 2),
                ]
            elif account == "current-player":
                rows = [self._player("Current-Player", 7050, 1)]
            else:
                rows = [
                    self._player("target-player-extra", 7199, 1),
                    self._player("Target-Player", 7103, 2),
                ]
            if game_id != "" and not self.ignore_game_filter:
                rows = [row for row in rows if row["gid"] == game_id]
            page = int(json["currentPage"])
            requested_page_size = int(json["pageSize"])
            page_size = (
                min(requested_page_size, self.server_page_limit)
                if self.server_page_limit is not None
                else requested_page_size
            )
            start = (page - 1) * page_size
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "count": len(rows),
                        "list": rows[start : start + page_size],
                        "currentPage": page,
                    },
                }
            )
        if url.endswith("/admin/agent/getSubAgentList"):
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "count": 2,
                        "list": [
                            {
                                "agentId": 7103,
                                "username": "agent-7103",
                                "nickname": "Agent 7103",
                                "legacy": {"oldAgentId": 6103},
                            },
                            {
                                "agentId": 7104,
                                "username": "agent-7104",
                                "nickname": "Agent 7104",
                                "legacy": {"oldAgentId": 6104},
                            },
                        ],
                    },
                }
            )
        raise AssertionError(f"Unexpected URL: {url}")

    @staticmethod
    def _player(account: str, agent_id: int, game_id: int) -> dict:
        return {
            "_id": f"player-{account}",
            "account": account,
            "password": "must-not-leak",
            "agentId": agent_id,
            "agentParents": [7049, 7050],
            "gid": game_id,
            "gameName": "Test Game",
            "isStop": False,
            "createTime": "2026-07-27T00:00:00.000Z",
        }


class ScriptedAuthSession:
    def __init__(
        self,
        user_list_responses: list[dict],
        *,
        login_tokens: list[str] | None = None,
    ) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict, dict[str, str]]] = []
        self.user_list_responses = list(user_list_responses)
        self.login_tokens = login_tokens or ["token-1", "token-2"]
        self.login_count = 0

    def post(self, url: str, json: dict, timeout: int) -> FakeResponse:
        del timeout
        self.calls.append((url, dict(json), dict(self.headers)))
        if url.endswith("/admin/agent/login"):
            token_index = min(self.login_count, len(self.login_tokens) - 1)
            token = self.login_tokens[token_index]
            self.login_count += 1
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "token": token,
                        "userInfo": {
                            "agentId": 7050,
                            "username": "current-agent",
                            "nickname": "Current Agent",
                        },
                    },
                }
            )
        if url.endswith("/admin/user/list") and self.user_list_responses:
            return FakeResponse(self.user_list_responses.pop(0))
        raise AssertionError(f"Unexpected URL: {url}")


class AgentBackendReauthenticationTests(unittest.TestCase):
    @staticmethod
    def success_response() -> dict:
        return {
            "code": 0,
            "data": {
                "count": 1,
                "list": [FakeSession._player("Target-Player", 7103, 2)],
            },
        }

    @staticmethod
    def make_client(
        session: ScriptedAuthSession,
        *,
        max_retries: int = 3,
    ) -> TLBBBackendClient:
        return TLBBBackendClient(
            "https://backend.example.test/prod-api",
            "api-user",
            "api-password",
            session=cast(Any, session),
            max_retries=max_retries,
        )

    def test_expiry_classifier_is_specific_to_authentication(self) -> None:
        for response in (
            {"code": 401, "msg": "unauthorized"},
            {"code": 500, "msg": "登录失效!"},
            {"code": 7, "msg": "未登录"},
            {"code": 7, "msg": "token 已过期"},
        ):
            with self.subTest(response=response):
                self.assertTrue(response_requires_relogin(response))

        for response in (
            {"code": 0, "msg": "登录失效"},
            {"code": 7, "msg": "活动已过期"},
            {"code": 403, "msg": "无权限"},
            {"code": 500, "msg": "数据库异常"},
        ):
            with self.subTest(response=response):
                self.assertFalse(response_requires_relogin(response))

    def test_player_list_relogs_once_and_retries_with_new_token(self) -> None:
        session = ScriptedAuthSession(
            [
                {"code": 500, "msg": "登录失效!"},
                self.success_response(),
            ]
        )
        client = self.make_client(session)

        rows = client.get_player_agents(
            "target-player",
            include_agent_metadata=False,
        )

        login_calls = [call for call in session.calls if call[0].endswith("/admin/agent/login")]
        list_calls = [call for call in session.calls if call[0].endswith("/admin/user/list")]
        self.assertEqual([row["account"] for row in rows], ["Target-Player"])
        self.assertEqual(len(login_calls), 2)
        self.assertEqual(len(list_calls), 2)
        self.assertEqual(list_calls[0][2].get("token"), "token-1")
        self.assertEqual(list_calls[1][2].get("token"), "token-2")
        self.assertEqual(client.token, "token-2")
        self.assertEqual(session.headers.get("token"), "token-2")

    def test_player_list_stops_after_one_failed_relogin(self) -> None:
        session = ScriptedAuthSession(
            [
                {"code": 500, "msg": "登录失效!"},
                {"code": 500, "msg": "登录失效!"},
            ]
        )
        client = self.make_client(session, max_retries=5)

        with self.assertRaisesRegex(
            TLBBBackendError,
            "POST /admin/user/list failed: 登录失效!",
        ):
            client.get_player_agents(
                "target-player",
                include_agent_metadata=False,
            )

        login_calls = [call for call in session.calls if call[0].endswith("/admin/agent/login")]
        list_calls = [call for call in session.calls if call[0].endswith("/admin/user/list")]
        self.assertEqual(len(login_calls), 2)
        self.assertEqual(len(list_calls), 2)

    def test_unrelated_error_and_default_post_do_not_relogin(self) -> None:
        unrelated_session = ScriptedAuthSession(
            [{"code": 500, "msg": "数据库异常"}]
        )
        unrelated_client = self.make_client(unrelated_session)
        with self.assertRaisesRegex(TLBBBackendError, "数据库异常"):
            unrelated_client.get_player_agents(
                "target-player",
                include_agent_metadata=False,
            )

        raw_session = ScriptedAuthSession(
            [{"code": 500, "msg": "登录失效!"}]
        )
        raw_client = self.make_client(raw_session)
        raw_client.login()
        with self.assertRaisesRegex(TLBBBackendError, "登录失效!"):
            raw_client.request_json(
                "POST",
                "/admin/user/list",
                {"search": {"account": "target-player"}},
            )

        unrelated_logins = [
            call for call in unrelated_session.calls if call[0].endswith("/admin/agent/login")
        ]
        raw_logins = [call for call in raw_session.calls if call[0].endswith("/admin/agent/login")]
        self.assertEqual(len(unrelated_logins), 1)
        self.assertEqual(len(raw_logins), 1)


class PlayerAgentTests(unittest.TestCase):
    def make_client(self) -> tuple[TLBBBackendClient, FakeSession]:
        session = FakeSession()
        client = TLBBBackendClient(
            "https://backend.example.test/prod-api",
            "api-user",
            "api-password",
            session=cast(Any, session),
        )
        return client, session

    def test_get_player_agents_paginates_and_keeps_only_exact_account(self) -> None:
        client, session = self.make_client()

        rows = client.get_player_agents("  target-player  ", page_size=1)

        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0],
            {
                "account": "Target-Player",
                "agentId": 7103,
                "agentParents": [7049, 7050],
                "gameId": 2,
                "gameName": "Test Game",
                "isStop": False,
                "createTime": "2026-07-27T00:00:00.000Z",
                "currentAgentId": 7103,
                "legacyAgentId": 6103,
                "nickname": "Agent 7103",
                "username": "agent-7103",
            },
        )
        self.assertNotIn("password", rows[0])
        player_calls = [call for call in session.calls if call[0].endswith("/admin/user/list")]
        self.assertEqual(len(player_calls), 2)
        self.assertEqual(player_calls[0][1]["search"], {"account": "target-player", "gameId": ""})
        self.assertEqual(session.headers["token"], "test-token")

    def test_get_player_agents_honors_count_when_server_caps_page_size(self) -> None:
        session = FakeSession(server_page_limit=1)
        client = TLBBBackendClient(
            "https://backend.example.test/prod-api",
            "api-user",
            "api-password",
            session=cast(Any, session),
        )

        rows = client.get_player_agents("target-player", include_agent_metadata=False)

        self.assertEqual([row["account"] for row in rows], ["Target-Player"])
        player_calls = [call for call in session.calls if call[0].endswith("/admin/user/list")]
        self.assertEqual(len(player_calls), 2)

    def test_get_player_agent_returns_none_when_account_does_not_exist(self) -> None:
        client, _ = self.make_client()

        result = client.get_player_agent("missing-player")

        self.assertIsNone(result)

    def test_get_player_agent_requires_game_id_for_duplicate_accounts(self) -> None:
        session = FakeSession(ignore_game_filter=True)
        client = TLBBBackendClient(
            "https://backend.example.test/prod-api",
            "api-user",
            "api-password",
            session=cast(Any, session),
        )

        with self.assertRaisesRegex(TLBBBackendError, "Multiple player records"):
            client.get_player_agent("duplicate-player", include_agent_metadata=False)

        result = client.get_player_agent(
            "duplicate-player",
            game_id=2,
            include_agent_metadata=False,
        )
        assert result is not None
        self.assertEqual(result["agentId"], 7104)
        self.assertEqual(result["gameId"], 2)

    def test_get_player_agent_uses_current_login_metadata(self) -> None:
        client, _ = self.make_client()

        result = client.get_player_agent("current-player")

        assert result is not None
        self.assertEqual(result["agentId"], 7050)
        self.assertEqual(result["username"], "current-agent")
        self.assertEqual(result["legacyAgentId"], 6050)

    def test_get_player_agent_rejects_empty_account(self) -> None:
        client, session = self.make_client()

        with self.assertRaisesRegex(TLBBBackendError, "must not be empty"):
            client.get_player_agent("   ")

        self.assertEqual(session.calls, [])

    def test_get_player_agents_rejects_non_positive_page_size(self) -> None:
        client, session = self.make_client()

        with self.assertRaisesRegex(TLBBBackendError, "positive integer"):
            client.get_player_agents("target-player", page_size=0)

        self.assertEqual(session.calls, [])

    def test_from_env_file_reads_api_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "API_BASE_URL=https://backend.example.test/prod-api",
                        "API_USERNAME='api-user'",
                        'API_PASSWORD="api-password"',
                    ]
                ),
                encoding="utf-8-sig",
            )

            client = TLBBBackendClient.from_env_file(env_path, session=cast(Any, FakeSession()))

        self.assertEqual(client.base_url, "https://backend.example.test/prod-api")
        self.assertEqual(client.username, "api-user")
        self.assertEqual(client.password, "api-password")


if __name__ == "__main__":
    unittest.main()
