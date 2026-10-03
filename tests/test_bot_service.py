from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Callable, cast


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tlbb_bot.ban_admin_login import GameUserPassword  # noqa: E402
from tlbb_bot.bot_service import (  # noqa: E402
    TEAM_369_AGENT_IDS,
    BotCommandProcessor,
    TLBBBotService,
    is_369_team_agent,
)


class FakeAgentClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | str | None]] = []
        self.agent_id = 7103
        self.agent_parents = [7049, 7050]

    def get_player_agents(
        self,
        account: str,
        *,
        game_id: int | str | None = None,
    ) -> list[dict]:
        self.calls.append((account, game_id))
        if account == "missing":
            return []
        return [
            {
                "account": account,
                "agentId": self.agent_id,
                "agentParents": list(self.agent_parents),
                "gameId": game_id or 1,
                "gameName": "测试游戏",
                "username": "agent-user",
                "nickname": "代理昵称",
            }
        ]


class FakeAdminClient:
    def __init__(self) -> None:
        self.role_matches: list[dict[str, str]] = [
            {"roleName": "完整昵称", "account": "game-account"}
        ]
        self.action_calls: list[tuple[str, dict[str, object]]] = []
        self.password_updates: list[tuple[int, str, str, bool]] = []

    def get_servers(self) -> list[dict[str, object]]:
        return [
            {"ID": 29, "configName": "二十九区"},
            {"ID": 30, "configName": "三十区"},
        ]

    def find_similar_roles(self, **kwargs: object) -> list[dict[str, str]]:
        return [dict(row) for row in self.role_matches]

    def _action(self, name: str, kwargs: dict[str, object]) -> dict[str, object]:
        self.action_calls.append((name, dict(kwargs)))
        return {"code": 0, "msg": "ok"}

    def ban_role(self, **kwargs: object) -> dict[str, object]:
        return self._action("ban", kwargs)

    def unban_role(self, **kwargs: object) -> dict[str, object]:
        return self._action("unban", kwargs)

    def mute_role(self, **kwargs: object) -> dict[str, object]:
        return self._action("mute", kwargs)

    def unmute_role(self, **kwargs: object) -> dict[str, object]:
        return self._action("unmute", kwargs)

    def find_game_user_by_account(self, account: str) -> dict[str, object]:
        if account == "missing":
            raise ValueError("not found")
        return {"ID": 10001, "account": account}

    def get_game_user_password_by_account(self, account: str) -> GameUserPassword:
        return GameUserPassword(10001, account, "plain-secret")

    def update_game_user_password(
        self,
        user_id: int,
        new_password: str,
        *,
        expected_account: str,
        verify: bool,
    ) -> dict[str, object]:
        self.password_updates.append((user_id, new_password, expected_account, verify))
        return {
            "userId": user_id,
            "account": expected_account,
            "updated": True,
            "verified": verify,
        }


class MutableClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class BotCommandProcessorTests(unittest.TestCase):
    def make_processor(
        self,
        *,
        allowed_actor_ids: set[str] | None = None,
        authorization_checker: Callable[[str], bool] | None = None,
        clock: MutableClock | None = None,
    ) -> tuple[BotCommandProcessor, FakeAgentClient, FakeAdminClient]:
        agent = FakeAgentClient()
        admin = FakeAdminClient()
        service = TLBBBotService(cast(Any, agent), cast(Any, admin))
        processor = BotCommandProcessor(
            service,
            allowed_actor_ids=allowed_actor_ids,
            authorization_checker=authorization_checker,
            confirmation_ttl_seconds=60,
            clock=clock or MutableClock(),
            token_factory=lambda: "ABC123",
        )
        return processor, agent, admin

    def test_dynamic_authorizer_and_pending_discard_take_effect_immediately(self) -> None:
        authorized = {"root"}
        processor, _, admin = self.make_processor(
            authorization_checker=lambda actor_id: actor_id in authorized,
        )

        denied = processor.handle("operator", "/agent account")
        authorized.add("operator")
        allowed = processor.handle("operator", "/agent account")
        staged = processor.handle("operator", "/role ban 29 完整昵称")
        authorized.remove("operator")
        removed_confirm = processor.handle("operator", "/confirm ABC123")
        discarded = processor.discard_pending("operator")
        authorized.add("operator")
        after_readd = processor.handle("operator", "/confirm ABC123")

        self.assertIn("无权限", denied)
        self.assertIn("代理昵称", allowed)
        self.assertIn("确认码", staged)
        self.assertIn("无权限", removed_confirm)
        self.assertTrue(discarded)
        self.assertIn("没有待确认操作", after_readd)
        self.assertEqual(admin.action_calls, [])

    def test_agent_and_role_queries_format_results(self) -> None:
        processor, agent, _ = self.make_processor()

        agent_response = processor.handle("operator", "/agent player-account 2")
        role_response = processor.handle("operator", "/roles 29 完整")

        self.assertIn("代理昵称", agent_response)
        self.assertIn("7103", agent_response)
        self.assertIn("是否归属369团队：是", agent_response)
        self.assertEqual(agent.calls, [("player-account", 2)])
        self.assertIn("二十九区", role_response)
        self.assertIn("完整昵称", role_response)
        self.assertIn("game-account", role_response)

    def test_369_team_membership_uses_direct_agent_id_only(self) -> None:
        processor, agent, _ = self.make_processor()
        agent.agent_id = 7999
        agent.agent_parents = [7103]

        response = processor.handle("operator", "/agent outside-account")

        self.assertIn("所属代理：代理昵称 (ID 7999)", response)
        self.assertIn("上级代理链：7103", response)
        self.assertIn("是否归属369团队：否", response)
        self.assertEqual(
            TEAM_369_AGENT_IDS,
            frozenset(
                {
                    7080,
                    7181,
                    7078,
                    7103,
                    7167,
                    7111,
                    7184,
                    7105,
                    7185,
                    7114,
                    7600,
                    7597,
                    7685,
                }
            ),
        )
        self.assertTrue(
            all(is_369_team_agent(agent_id) for agent_id in TEAM_369_AGENT_IDS)
        )
        self.assertFalse(is_369_team_agent(True))
        self.assertFalse(is_369_team_agent("invalid"))

    def test_agent_command_accepts_string_game_id(self) -> None:
        processor, agent, _ = self.make_processor()

        response = processor.handle(
            "operator",
            "/agent player-account 6a19707f230ef4f0d45d980f",
        )

        self.assertIn("player-account", response)
        self.assertEqual(
            agent.calls,
            [("player-account", "6a19707f230ef4f0d45d980f")],
        )

    def test_role_action_requires_unique_exact_match_and_confirmation(self) -> None:
        processor, _, admin = self.make_processor()

        staged = processor.handle("operator", "/role ban 29 完整昵称")

        self.assertIn("确认码：ABC123", staged)
        self.assertEqual(admin.action_calls, [])

        confirmed = processor.handle("operator", "/confirm abc123")
        reused = processor.handle("operator", "/confirm ABC123")

        self.assertIn("封禁成功", confirmed)
        self.assertEqual(admin.action_calls[0][0], "ban")
        self.assertEqual(admin.action_calls[0][1]["role_name"], "完整昵称")
        self.assertIn("没有待确认操作", reused)

    def test_role_action_does_not_stage_inexact_or_ambiguous_match(self) -> None:
        processor, _, admin = self.make_processor()
        admin.role_matches = [
            {"roleName": "完整昵称一", "account": "account-1"},
            {"roleName": "完整昵称二", "account": "account-2"},
        ]

        response = processor.handle("operator", "/role mute 29 不完整")
        confirm = processor.handle("operator", "/confirm ABC123")

        self.assertIn("没有完整昵称完全匹配", response)
        self.assertEqual(admin.action_calls, [])
        self.assertIn("没有待确认操作", confirm)

    def test_all_role_actions_map_to_the_expected_domain_method(self) -> None:
        for command_action, expected_action in (
            ("ban", "ban"),
            ("unban", "unban"),
            ("mute", "mute"),
            ("unmute", "unmute"),
        ):
            with self.subTest(action=command_action):
                processor, _, admin = self.make_processor()
                processor.handle(
                    "operator",
                    f"/role {command_action} 29 完整昵称",
                )
                processor.handle("operator", "/confirm ABC123")
                self.assertEqual([call[0] for call in admin.action_calls], [expected_action])

    def test_confirmation_is_bound_to_actor_and_expires(self) -> None:
        clock = MutableClock()
        processor, _, admin = self.make_processor(clock=clock)
        processor.handle("operator-a", "/role ban 29 完整昵称")

        wrong_actor = processor.handle("operator-b", "/confirm ABC123")
        clock.value += 61
        expired = processor.handle("operator-a", "/confirm ABC123")

        self.assertIn("没有待确认操作", wrong_actor)
        self.assertIn("确认码已过期", expired)
        self.assertEqual(admin.action_calls, [])

    def test_password_query_is_private_and_returns_plaintext_when_private(self) -> None:
        processor, _, _ = self.make_processor()

        public_response = processor.handle(
            "operator",
            "/password game-user",
            is_private=False,
        )
        private_response = processor.handle("operator", "/password game-user")

        self.assertIn("只能在私聊", public_response)
        self.assertIn("明文密码：plain-secret", private_response)

    def test_group_role_action_and_confirmation_are_allowed(self) -> None:
        processor, _, admin = self.make_processor()

        staged = processor.handle(
            "operator",
            "/role unmute 29 完整昵称",
            is_private=False,
        )
        confirmed = processor.handle(
            "operator",
            "/confirm ABC123",
            is_private=False,
        )

        self.assertIn("解除禁言", staged)
        self.assertIn("解除禁言成功", confirmed)
        self.assertEqual([call[0] for call in admin.action_calls], ["unmute"])

    def test_password_update_confirmation_stays_private(self) -> None:
        processor, _, admin = self.make_processor()
        processor.handle("operator", "/set_password game-user new-secret-value")

        group_confirm = processor.handle(
            "operator",
            "/confirm ABC123",
            is_private=False,
        )
        private_confirm = processor.handle("operator", "/confirm ABC123")

        self.assertIn("只能在私聊", group_confirm)
        self.assertIn("密码更新成功", private_confirm)
        self.assertEqual(
            admin.password_updates,
            [(10001, "new-secret-value", "game-user", True)],
        )

    def test_password_update_hides_secret_and_requires_confirmation(self) -> None:
        processor, _, admin = self.make_processor()

        staged = processor.handle(
            "operator",
            "/set_password game-user new-secret-value",
        )
        challenge = processor.get_pending_challenge("operator")
        wrong_token = processor.handle("operator", "/confirm WRONG")
        confirmed = processor.handle("operator", "/confirm ABC123")

        self.assertNotIn("new-secret-value", staged)
        self.assertIsNotNone(challenge)
        self.assertNotIn("new-secret-value", repr(challenge))
        self.assertIn("确认码不正确", wrong_token)
        self.assertIn("密码更新成功", confirmed)
        self.assertEqual(
            admin.password_updates,
            [(10001, "new-secret-value", "game-user", True)],
        )
        self.assertNotIn("new-secret-value", confirmed)

    def test_allowlist_and_cancel(self) -> None:
        processor, _, admin = self.make_processor(allowed_actor_ids={"allowed"})

        denied = processor.handle("denied", "/agent player")
        processor.handle("allowed", "/role ban 29 完整昵称")
        cancelled = processor.handle("allowed", "/cancel")
        confirm = processor.handle("allowed", "/confirm ABC123")

        self.assertIn("无权限", denied)
        self.assertIn("已取消", cancelled)
        self.assertIn("没有待确认操作", confirm)
        self.assertEqual(admin.action_calls, [])


if __name__ == "__main__":
    unittest.main()
