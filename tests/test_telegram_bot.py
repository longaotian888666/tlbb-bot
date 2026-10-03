from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from telegram.constants import ChatType
from telegram.error import BadRequest, NetworkError, TimedOut


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tlbb_bot.admin_registry import TelegramAdminRegistry  # noqa: E402
from tlbb_bot.bot_service import ConfirmationChallenge  # noqa: E402
import tlbb_bot.telegram_bot as telegram_bot  # noqa: E402
from tlbb_bot.telegram_bot import (  # noqa: E402
    BTN_ADMIN,
    BTN_ADMIN_ADD,
    BTN_ADMIN_CANCEL,
    BTN_ADMIN_CONFIRM_ADD,
    BTN_ADMIN_CONFIRM_REMOVE,
    BTN_ADMIN_LIST,
    BTN_ADMIN_REMOVE,
    BTN_AGENT,
    BTN_BAN,
    BTN_CONFIRM,
    BTN_PASSWORD,
    BTN_ROLE,
    NETWORK_RESTART_REQUESTED_KEY,
    PASSWORD_ACCOUNT_KEY,
    STATE_AGENT_ACCOUNT,
    STATE_CONFIRM,
    STATE_KEY,
    STATE_MAIN,
    STATE_PASSWORD_NEW_VALUE,
    STATE_PASSWORD_QUERY_ACCOUNT,
    STATE_ROLE_MENU,
    STATE_ROLE_NICKNAME,
    STATE_ROLE_SERVER,
    TelegramBotController,
    TelegramBotSettings,
    confirmation_keyboard,
    main_menu_keyboard,
)


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.message_id = 101
        self.deleted = False
        self.sent: list[tuple[str, dict[str, object]]] = []

    async def reply_text(self, text: str, **kwargs: object) -> "FakeMessage":
        self.sent.append((text, dict(kwargs)))
        return self

    async def delete(self) -> None:
        self.deleted = True


class FakeApplication:
    def __init__(self) -> None:
        self.user_data: dict[int, dict[object, object]] = {}
        self.bot_data: dict[str, object] = {}
        self.stop_running_calls = 0

    def create_task(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("secret deletion should be disabled in these tests")

    def stop_running(self) -> None:
        self.stop_running_calls += 1


class FakeService:
    def list_servers(self) -> list[dict[str, object]]:
        return [
            {"serverId": 29, "serverName": "二十九区"},
            {"serverId": 30, "serverName": "三十区"},
        ]

    def find_game_user(self, account: str) -> dict[str, object]:
        return {"ID": 10001, "account": account}


class FakeProcessor:
    def __init__(self) -> None:
        self.service = FakeService()
        self.calls: list[tuple[str, str, bool]] = []
        self.challenge: ConfirmationChallenge | None = None
        self.discarded_actor_ids: list[str] = []

    @staticmethod
    def help_text() -> str:
        return "帮助"

    def handle(self, actor_id: str, text: str, *, is_private: bool) -> str:
        self.calls.append((actor_id, text, is_private))
        if text == "/cancel":
            self.challenge = None
            return "已取消待确认操作。"
        if text.startswith("/confirm "):
            if self.challenge is None:
                return "操作失败：没有待确认操作"
            self.challenge = None
            return "操作成功。"
        if text.startswith("/password "):
            return "账号：game-user\n明文密码：plain-secret"
        if text.startswith("/set_password "):
            self.challenge = ConfirmationChallenge(
                token="ABC123",
                kind="password_update",
                summary="修改密码",
                expires_at=999.0,
            )
            return "密码更新等待确认。"
        if text.startswith("/role "):
            self.challenge = ConfirmationChallenge(
                token="ABC123",
                kind="role_action",
                summary="封禁角色",
                expires_at=999.0,
            )
            return "角色操作等待确认。"
        return "查询成功。"

    def get_pending_challenge(self, actor_id: str) -> ConfirmationChallenge | None:
        return self.challenge

    def discard_pending(self, actor_id: str) -> bool:
        self.discarded_actor_ids.append(actor_id)
        had_pending = self.challenge is not None
        self.challenge = None
        return had_pending


def make_update(
    text: str,
    *,
    user_id: int = 123,
    chat_type: str = ChatType.PRIVATE,
    chat_id: int | None = None,
) -> tuple[Any, FakeMessage]:
    message = FakeMessage(text)
    if chat_id is None:
        chat_id = user_id if chat_type == ChatType.PRIVATE else -1001234567890
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
    )
    return update, message


def make_context() -> Any:
    return SimpleNamespace(
        user_data={},
        application=FakeApplication(),
        error=None,
    )


def keyboard_texts(markup: object) -> list[list[str]]:
    keyboard = getattr(markup, "keyboard")
    return [[button.text for button in row] for row in keyboard]


class TelegramSettingsTests(unittest.TestCase):
    def test_settings_load_token_allowlist_and_hide_token_repr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "TELEGRAM_BOT_TOKEN=secret-bot-token",
                        "TELEGRAM_ALLOWED_USER_IDS=123, 456",
                        "TELEGRAM_SECRET_MESSAGE_TTL_SECONDS=90",
                    ]
                ),
                encoding="utf-8",
            )
            settings = TelegramBotSettings.from_env_file(env_path)

        self.assertEqual(settings.allowed_user_ids, frozenset({123, 456}))
        self.assertIsNone(settings.super_admin_id)
        self.assertEqual(settings.secret_message_ttl, 90)
        self.assertEqual(
            settings.admin_store_path,
            env_path.parent.resolve() / ".telegram-admins.json",
        )
        self.assertNotIn("secret-bot-token", repr(settings))

    def test_explicit_and_legacy_super_admin_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            explicit_path = Path(tmp) / "explicit.env"
            explicit_path.write_text(
                "\n".join(
                    [
                        "TELEGRAM_BOT_TOKEN=token",
                        "TELEGRAM_SUPER_ADMIN_ID=999",
                        "TELEGRAM_ALLOWED_USER_IDS=123",
                        "TELEGRAM_ADMIN_STORE_PATH=data/admins.json",
                    ]
                ),
                encoding="utf-8",
            )
            explicit = TelegramBotSettings.from_env_file(explicit_path)

            legacy_path = Path(tmp) / "legacy.env"
            legacy_path.write_text(
                "TELEGRAM_BOT_TOKEN=token\nTELEGRAM_ALLOWED_USER_IDS=456\n",
                encoding="utf-8",
            )
            legacy = TelegramBotSettings.from_env_file(legacy_path)

        self.assertEqual(explicit.super_admin_id, 999)
        self.assertEqual(
            explicit.admin_store_path,
            explicit_path.parent.resolve() / "data" / "admins.json",
        )
        self.assertEqual(legacy.super_admin_id, 456)

    def test_main_and_confirmation_keyboards_are_persistent(self) -> None:
        main = main_menu_keyboard()
        confirmation = confirmation_keyboard()

        self.assertEqual(
            keyboard_texts(main),
            [[BTN_AGENT, BTN_PASSWORD], [BTN_ROLE]],
        )
        self.assertTrue(main.is_persistent)
        self.assertIn(BTN_CONFIRM, keyboard_texts(confirmation)[0])


class TelegramControllerTests(unittest.IsolatedAsyncioTestCase):
    def make_controller(
        self,
        *,
        allowed_user_ids: frozenset[int] = frozenset({123}),
        super_admin_id: int | None = None,
        registry: TelegramAdminRegistry | None = None,
        secret_message_ttl: int = 0,
    ) -> tuple[TelegramBotController, FakeProcessor]:
        processor = FakeProcessor()
        settings = TelegramBotSettings(
            token="test-token",
            allowed_user_ids=allowed_user_ids,
            secret_message_ttl=secret_message_ttl,
            super_admin_id=super_admin_id,
        )
        return (
            TelegramBotController(cast(Any, processor), settings, registry),
            processor,
        )

    async def test_start_shows_persistent_main_menu_in_private_chat(self) -> None:
        controller, _ = self.make_controller()
        context = make_context()
        update, message = make_update("/start")

        await controller.start(update, context)

        self.assertEqual(context.user_data[STATE_KEY], STATE_MAIN)
        markup = message.sent[-1][1]["reply_markup"]
        self.assertEqual(
            keyboard_texts(markup),
            [[BTN_AGENT, BTN_PASSWORD], [BTN_ROLE], [BTN_ADMIN]],
        )
        self.assertTrue(cast(Any, markup).selective)

    async def test_regular_admin_does_not_see_or_open_admin_management(self) -> None:
        registry = TelegramAdminRegistry(
            super_admin_id=999,
            static_admin_ids={123},
        )
        controller, _ = self.make_controller(
            allowed_user_ids=frozenset({123}),
            super_admin_id=999,
            registry=registry,
        )
        context = make_context()
        start_update, start_message = make_update("/start")

        await controller.start(start_update, context)
        markup = start_message.sent[-1][1]["reply_markup"]
        self.assertNotIn(BTN_ADMIN, sum(keyboard_texts(markup), []))

        typed_update, typed_message = make_update(BTN_ADMIN)
        await controller.text(typed_update, context)
        self.assertIn("只有总管理员", typed_message.sent[-1][0])

    async def test_super_admin_adds_and_removes_admin_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = TelegramAdminRegistry(
                super_admin_id=123,
                store_path=Path(tmp) / "admins.json",
            )
            controller, processor = self.make_controller(registry=registry)
            context = make_context()

            menu_update, _ = make_update(BTN_ADMIN)
            await controller.text(menu_update, context)
            add_update, _ = make_update(BTN_ADMIN_ADD)
            await controller.text(add_update, context)
            id_update, _ = make_update("456")
            await controller.text(id_update, context)
            confirm_update, confirm_message = make_update(BTN_ADMIN_CONFIRM_ADD)
            await controller.text(confirm_update, context)

            self.assertTrue(registry.is_authorized(456))
            self.assertIn("已新增管理员", confirm_message.sent[-1][0])

            admin_context = make_context()
            admin_start, admin_message = make_update("/start", user_id=456)
            await controller.start(admin_start, admin_context)
            self.assertIn("请选择功能", admin_message.sent[-1][0])
            admin_markup = admin_message.sent[-1][1]["reply_markup"]
            self.assertNotIn(BTN_ADMIN, sum(keyboard_texts(admin_markup), []))

            remove_context = make_context()
            remove_menu, _ = make_update(BTN_ADMIN)
            await controller.text(remove_menu, remove_context)
            remove_update, _ = make_update(BTN_ADMIN_REMOVE)
            await controller.text(remove_update, remove_context)
            remove_id, _ = make_update("456")
            await controller.text(remove_id, remove_context)
            remove_confirm, remove_message = make_update(BTN_ADMIN_CONFIRM_REMOVE)
            await controller.text(remove_confirm, remove_context)

            self.assertFalse(registry.is_authorized(456))
            self.assertEqual(processor.discarded_actor_ids, ["456"])
            self.assertIn("已删除管理员", remove_message.sent[-1][0])

            denied, denied_message = make_update("/start", user_id=456)
            await controller.start(denied, make_context())
            self.assertIn("未授权", denied_message.sent[-1][0])

            reloaded = TelegramAdminRegistry(
                super_admin_id=123,
                store_path=Path(tmp) / "admins.json",
            )
            self.assertFalse(reloaded.is_authorized(456))

    async def test_group_and_regular_admin_cannot_run_admin_commands(self) -> None:
        registry = TelegramAdminRegistry(
            super_admin_id=123,
            static_admin_ids={456},
        )
        controller, _ = self.make_controller(registry=registry)

        group_update, group_message = make_update(
            "/add_admin 789",
            chat_type=ChatType.GROUP,
        )
        await controller.admin_command(group_update, make_context())
        self.assertIn("请私聊", group_message.sent[-1][0])

        regular_update, regular_message = make_update(
            "/add_admin 789",
            user_id=456,
        )
        await controller.admin_command(regular_update, make_context())
        self.assertIn("只有总管理员", regular_message.sent[-1][0])

        forged_context = make_context()
        forged_cancel, forged_cancel_message = make_update(
            BTN_ADMIN_CANCEL,
            user_id=456,
        )
        await controller.text(forged_cancel, forged_context)
        forged_list, forged_list_message = make_update(
            BTN_ADMIN_LIST,
            user_id=456,
        )
        await controller.text(forged_list, forged_context)

        self.assertIn("只有总管理员", forged_cancel_message.sent[-1][0])
        self.assertNotIn("总管理员：", forged_list_message.sent[-1][0])
        self.assertFalse(registry.is_authorized(789))

    async def test_unauthorized_user_is_blocked_and_authorized_group_can_start(self) -> None:
        controller, processor = self.make_controller(allowed_user_ids=frozenset())
        context = make_context()
        unauthorized, unauthorized_message = make_update("/start")

        await controller.start(unauthorized, context)

        group_controller, group_processor = self.make_controller()
        group_update, group_message = make_update("/start", chat_type=ChatType.GROUP)
        await group_controller.start(group_update, make_context())

        self.assertIn("未授权", unauthorized_message.sent[-1][0])
        self.assertIn("请选择功能", group_message.sent[-1][0])
        self.assertTrue(group_message.sent[-1][1]["do_quote"])
        group_markup = group_message.sent[-1][1]["reply_markup"]
        self.assertEqual(keyboard_texts(group_markup), [[BTN_AGENT], [BTN_ROLE]])
        self.assertEqual(processor.calls, [])
        self.assertEqual(group_processor.calls, [])

    async def test_group_command_runs_with_public_context(self) -> None:
        controller, processor = self.make_controller()
        update, message = make_update(
            "/agent game-user",
            chat_type=ChatType.SUPERGROUP,
        )

        await controller.command(update, make_context())

        self.assertEqual(
            processor.calls[-1],
            ("123", "/agent game-user", False),
        )
        self.assertIn("查询成功", message.sent[-1][0])
        self.assertTrue(message.sent[-1][1]["do_quote"])

    async def test_group_role_buttons_preserve_action_and_public_context(self) -> None:
        controller, processor = self.make_controller()
        context = make_context()
        chat_id = -1009876543210

        menu_update, _ = make_update(
            BTN_ROLE,
            chat_type=ChatType.SUPERGROUP,
            chat_id=chat_id,
        )
        await controller.text(menu_update, context)
        action_update, _ = make_update(
            BTN_BAN,
            chat_type=ChatType.SUPERGROUP,
            chat_id=chat_id,
        )
        await controller.text(action_update, context)
        server_update, _ = make_update(
            "29 二十九区",
            chat_type=ChatType.SUPERGROUP,
            chat_id=chat_id,
        )
        await controller.text(server_update, context)
        nickname_update, _ = make_update(
            "完整昵称",
            chat_type=ChatType.SUPERGROUP,
            chat_id=chat_id,
        )
        await controller.text(nickname_update, context)

        self.assertEqual(
            processor.calls[-1],
            ("123", "/role ban 29 '完整昵称'", False),
        )
        self.assertEqual(context.user_data[STATE_KEY], STATE_CONFIRM)

    async def test_group_password_button_requires_private_chat(self) -> None:
        controller, processor = self.make_controller()
        update, message = make_update(BTN_PASSWORD, chat_type=ChatType.GROUP)

        await controller.text(update, make_context())

        self.assertEqual(processor.calls, [])
        self.assertIn("明文密码", message.sent[-1][0])
        self.assertIn("私聊", message.sent[-1][0])

    async def test_unrelated_group_text_is_silently_ignored(self) -> None:
        controller, processor = self.make_controller()
        authorized, authorized_message = make_update(
            "大家晚上好",
            chat_type=ChatType.GROUP,
        )
        unauthorized, unauthorized_message = make_update(
            "普通群消息",
            user_id=456,
            chat_type=ChatType.GROUP,
        )

        await controller.text(authorized, make_context())
        await controller.text(unauthorized, make_context())

        self.assertEqual(authorized_message.sent, [])
        self.assertEqual(unauthorized_message.sent, [])
        self.assertEqual(processor.calls, [])

    async def test_private_workflow_does_not_consume_group_messages(self) -> None:
        controller, processor = self.make_controller()
        context = make_context()
        private_update, _ = make_update(BTN_AGENT)
        await controller.text(private_update, context)
        group_update, group_message = make_update(
            "not-an-account",
            chat_type=ChatType.GROUP,
        )

        await controller.text(group_update, context)

        self.assertEqual(group_message.sent, [])
        self.assertEqual(processor.calls, [])
        self.assertEqual(context.user_data[STATE_KEY], STATE_AGENT_ACCOUNT)

    async def test_password_query_is_protected(self) -> None:
        controller, processor = self.make_controller()
        context = make_context()
        context.user_data[STATE_KEY] = STATE_PASSWORD_QUERY_ACCOUNT
        update, message = make_update("game-user")

        await controller.text(update, context)

        self.assertEqual(
            processor.calls[-1],
            ("123", "/password game-user", True),
        )
        response, kwargs = message.sent[-1]
        self.assertIn("plain-secret", response)
        self.assertTrue(kwargs["protect_content"])
        self.assertEqual(context.user_data[STATE_KEY], STATE_MAIN)

    async def test_new_password_message_is_deleted_and_uses_structured_challenge(self) -> None:
        controller, processor = self.make_controller()
        context = make_context()
        context.user_data[STATE_KEY] = STATE_PASSWORD_NEW_VALUE
        context.user_data[PASSWORD_ACCOUNT_KEY] = "game-user"
        update, message = make_update("new-secret-value")

        await controller.text(update, context)

        self.assertTrue(message.deleted)
        self.assertIn("new-secret-value", processor.calls[-1][1])
        self.assertNotIn("new-secret-value", message.sent[-1][0])
        self.assertEqual(context.user_data[STATE_KEY], STATE_CONFIRM)
        self.assertEqual(
            keyboard_texts(message.sent[-1][1]["reply_markup"])[0][0],
            BTN_CONFIRM,
        )

    async def test_role_buttons_guide_server_nickname_and_confirmation(self) -> None:
        controller, processor = self.make_controller()
        context = make_context()
        context.user_data[STATE_KEY] = STATE_ROLE_MENU

        action_update, _ = make_update(BTN_BAN)
        await controller.text(action_update, context)
        self.assertEqual(context.user_data[STATE_KEY], STATE_ROLE_SERVER)

        server_update, _ = make_update("29 二十九区")
        await controller.text(server_update, context)
        self.assertEqual(context.user_data[STATE_KEY], STATE_ROLE_NICKNAME)

        nickname_update, message = make_update("完整昵称")
        await controller.text(nickname_update, context)

        self.assertEqual(
            processor.calls[-1],
            ("123", "/role ban 29 '完整昵称'", True),
        )
        self.assertEqual(context.user_data[STATE_KEY], STATE_CONFIRM)
        self.assertIn(BTN_CONFIRM, keyboard_texts(message.sent[-1][1]["reply_markup"])[0])

    async def test_leaving_confirmation_cancels_hidden_pending_operation(self) -> None:
        controller, processor = self.make_controller()
        processor.challenge = ConfirmationChallenge(
            token="ABC123",
            kind="role_action",
            summary="封禁角色",
            expires_at=999.0,
        )
        context = make_context()
        context.user_data[STATE_KEY] = STATE_CONFIRM
        update, _ = make_update(BTN_AGENT)

        await controller.text(update, context)

        self.assertIn(("123", "/cancel", True), processor.calls)
        self.assertIsNone(processor.challenge)

    async def test_network_error_requests_one_polling_restart(self) -> None:
        controller, _ = self.make_controller()
        context = make_context()
        context.error = NetworkError("temporary Telegram outage")

        await controller.error(None, context)
        await controller.error(None, context)

        self.assertTrue(
            context.application.bot_data[NETWORK_RESTART_REQUESTED_KEY]
        )
        self.assertEqual(context.application.stop_running_calls, 1)

    async def test_live_timeout_and_bad_request_do_not_restart_polling(self) -> None:
        controller, _ = self.make_controller()

        for error in (TimedOut(), BadRequest("invalid request")):
            with self.subTest(error=type(error).__name__):
                context = make_context()
                context.error = error

                await controller.error(None, context)

                self.assertNotIn(
                    NETWORK_RESTART_REQUESTED_KEY,
                    context.application.bot_data,
                )
                self.assertEqual(context.application.stop_running_calls, 0)

    async def test_secret_deletion_is_not_an_application_shutdown_task(self) -> None:
        controller, _ = self.make_controller(secret_message_ttl=1)
        message = FakeMessage("secret")

        async def delete_immediately(target: FakeMessage, delay: int) -> None:
            self.assertEqual(delay, 1)
            await target.delete()

        controller._delete_later = cast(Any, delete_immediately)
        controller._schedule_secret_deletion(cast(Any, message))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertTrue(message.deleted)
        self.assertEqual(len(controller._secret_deletion_tasks), 0)

    async def test_non_network_error_does_not_restart_polling(self) -> None:
        controller, _ = self.make_controller()
        context = make_context()
        context.error = RuntimeError("handler bug")

        await controller.error(None, context)

        self.assertNotIn(
            NETWORK_RESTART_REQUESTED_KEY,
            context.application.bot_data,
        )
        self.assertEqual(context.application.stop_running_calls, 0)


class FakePollingApplication:
    def __init__(self, outcomes: list[object]) -> None:
        self.bot_data: dict[str, object] = {}
        self.outcomes = list(outcomes)
        self.run_calls: list[dict[str, object]] = []

    def run_polling(self, **kwargs: object) -> None:
        self.run_calls.append(dict(kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome:
            self.bot_data[NETWORK_RESTART_REQUESTED_KEY] = True


class TelegramMainTests(unittest.TestCase):
    def test_main_reuses_application_after_restart_and_keeps_pending_updates(self) -> None:
        application = FakePollingApplication([True, False])
        args = SimpleNamespace(
            env=".env",
            timeout=25.0,
            check=False,
            keep_pending_updates=False,
        )
        parser = SimpleNamespace(parse_args=lambda: args)

        with (
            patch.object(telegram_bot, "build_parser", return_value=parser),
            patch.object(
                telegram_bot,
                "build_application",
                return_value=(application, None),
            ) as build,
            patch.object(telegram_bot.time, "sleep") as sleep,
        ):
            result = telegram_bot.main()

        self.assertEqual(result, 0)
        self.assertEqual(build.call_count, 1)
        sleep.assert_called_once_with(telegram_bot.NETWORK_RESTART_DELAY_SECONDS)
        self.assertTrue(application.run_calls[0]["drop_pending_updates"])
        self.assertFalse(application.run_calls[1]["drop_pending_updates"])
        self.assertEqual(application.run_calls[0]["bootstrap_retries"], 0)
        self.assertFalse(application.run_calls[0]["close_loop"])
        self.assertNotIn(NETWORK_RESTART_REQUESTED_KEY, application.bot_data)

    def test_main_retries_startup_network_error_with_same_application(self) -> None:
        application = FakePollingApplication(
            [NetworkError("startup transport failure"), False]
        )
        args = SimpleNamespace(
            env=".env",
            timeout=25.0,
            check=False,
            keep_pending_updates=False,
        )
        parser = SimpleNamespace(parse_args=lambda: args)

        with (
            patch.object(telegram_bot, "build_parser", return_value=parser),
            patch.object(
                telegram_bot,
                "build_application",
                return_value=(application, None),
            ) as build,
            patch.object(telegram_bot.time, "sleep") as sleep,
        ):
            result = telegram_bot.main()

        self.assertEqual(result, 0)
        self.assertEqual(build.call_count, 1)
        sleep.assert_called_once_with(telegram_bot.NETWORK_RESTART_DELAY_SECONDS)
        self.assertEqual(len(application.run_calls), 2)
        self.assertFalse(application.run_calls[1]["drop_pending_updates"])

    def test_main_uses_bounded_exponential_restart_delay(self) -> None:
        application = FakePollingApplication([True, True, True, True, True, False])
        args = SimpleNamespace(
            env=".env",
            timeout=25.0,
            check=False,
            keep_pending_updates=True,
        )
        parser = SimpleNamespace(parse_args=lambda: args)

        with (
            patch.object(telegram_bot, "build_parser", return_value=parser),
            patch.object(
                telegram_bot,
                "build_application",
                return_value=(application, None),
            ),
            patch.object(telegram_bot.time, "sleep") as sleep,
        ):
            result = telegram_bot.main()

        self.assertEqual(result, 0)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [5, 10, 20, 40, 60],
        )
        self.assertTrue(
            all(not call["drop_pending_updates"] for call in application.run_calls)
        )

    def test_main_does_not_retry_bad_request(self) -> None:
        application = FakePollingApplication([BadRequest("invalid request")])
        args = SimpleNamespace(
            env=".env",
            timeout=25.0,
            check=False,
            keep_pending_updates=False,
        )
        parser = SimpleNamespace(parse_args=lambda: args)

        with (
            patch.object(telegram_bot, "build_parser", return_value=parser),
            patch.object(
                telegram_bot,
                "build_application",
                return_value=(application, None),
            ),
            patch.object(telegram_bot.time, "sleep") as sleep,
        ):
            result = telegram_bot.main()

        self.assertEqual(result, 1)
        sleep.assert_not_called()
        self.assertEqual(len(application.run_calls), 1)


if __name__ == "__main__":
    unittest.main()
