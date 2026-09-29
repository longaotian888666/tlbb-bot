"""Telegram polling adapter for the TLBB bot command service."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from tlbb_backend import TLBBBackendClient

from .admin_registry import TelegramAdminRegistry
from .ban_admin_login import BanAdminClient, load_config, load_env
from .bot_service import (
    BotCommandProcessor,
    ConfirmationChallenge,
    TLBBBotService,
)


logger = logging.getLogger(__name__)
TELEGRAM_MESSAGE_CHUNK_SIZE = 3500
NETWORK_RESTART_REQUESTED_KEY = "network_restart_requested"
NETWORK_RESTART_DELAY_SECONDS = 5
NETWORK_RESTART_MAX_DELAY_SECONDS = 60
NETWORK_STABLE_RESET_SECONDS = 60


def _is_runtime_transport_error(error: object) -> bool:
    # BadRequest and TimedOut also inherit NetworkError in python-telegram-bot.
    # Only the base error represents the broken polling transport observed here.
    return type(error) is NetworkError


def _is_retryable_startup_error(error: object) -> bool:
    return type(error) in {NetworkError, TimedOut}

BTN_AGENT = "查代理"
BTN_PASSWORD = "查改密码"
BTN_ROLE = "封号禁言"
BTN_QUERY_PASSWORD = "查询密码"
BTN_UPDATE_PASSWORD = "修改密码"
BTN_QUERY_ROLE = "查询昵称"
BTN_SERVERS = "区服列表"
BTN_BAN = "封禁"
BTN_UNBAN = "解除封禁"
BTN_MUTE = "禁言"
BTN_UNMUTE = "解除禁言"
BTN_CONFIRM = "确认操作"
BTN_CANCEL = "取消操作"
BTN_BACK = "返回主菜单"
BTN_ADMIN = "管理员管理"
BTN_ADMIN_LIST = "管理员列表"
BTN_ADMIN_ADD = "新增管理员"
BTN_ADMIN_REMOVE = "删除管理员"
BTN_ADMIN_CONFIRM_ADD = "确认新增管理员"
BTN_ADMIN_CONFIRM_REMOVE = "确认删除管理员"
BTN_ADMIN_CANCEL = "取消管理员操作"

STATE_KEY = "telegram_state"
CONFIRMATION_TOKEN_KEY = "confirmation_token"
ROLE_ACTION_KEY = "role_action"
SERVER_ID_KEY = "server_id"
PASSWORD_ACCOUNT_KEY = "password_account"
ADMIN_ACTION_KEY = "admin_action"
ADMIN_TARGET_KEY = "admin_target"
ADMIN_PENDING_AT_KEY = "admin_pending_at"

STATE_MAIN = "main"
STATE_PASSWORD_MENU = "password_menu"
STATE_ROLE_MENU = "role_menu"
STATE_AGENT_ACCOUNT = "agent_account"
STATE_PASSWORD_QUERY_ACCOUNT = "password_query_account"
STATE_PASSWORD_UPDATE_ACCOUNT = "password_update_account"
STATE_PASSWORD_NEW_VALUE = "password_new_value"
STATE_ROLE_SERVER = "role_server"
STATE_ROLE_NICKNAME = "role_nickname"
STATE_CONFIRM = "confirm"
STATE_ADMIN_MENU = "admin_menu"
STATE_ADMIN_ADD_ID = "admin_add_id"
STATE_ADMIN_REMOVE_ID = "admin_remove_id"
STATE_ADMIN_CONFIRM = "admin_confirm"

ADMIN_CONFIRMATION_TTL_SECONDS = 300

ROLE_BUTTON_ACTIONS = {
    BTN_QUERY_ROLE: "query",
    BTN_BAN: "ban",
    BTN_UNBAN: "unban",
    BTN_MUTE: "mute",
    BTN_UNMUTE: "unmute",
}

@dataclass(frozen=True)
class TelegramBotSettings:
    token: str = field(repr=False)
    allowed_user_ids: frozenset[int]
    secret_message_ttl: int = 60
    super_admin_id: int | None = None
    admin_store_path: Path = Path(".telegram-admins.json")

    @classmethod
    def from_env_file(cls, path: str | Path = ".env") -> "TelegramBotSettings":
        rows = load_env(Path(path))
        token = str(
            rows.get("TELEGRAM_BOT_TOKEN")
            or rows.get("BOT_TOKEN")
            or ""
        ).strip()
        if not token:
            raise ValueError(".env 缺少 TELEGRAM_BOT_TOKEN")

        raw_user_ids = str(rows.get("TELEGRAM_ALLOWED_USER_IDS") or "")
        allowed_user_ids: set[int] = set()
        for value in re.split(r"[\s,;]+", raw_user_ids.strip()):
            if not value:
                continue
            try:
                user_id = int(value)
            except ValueError as exc:
                raise ValueError(
                    "TELEGRAM_ALLOWED_USER_IDS 必须是逗号分隔的整数"
                ) from exc
            if user_id < 1:
                raise ValueError("TELEGRAM_ALLOWED_USER_IDS 必须包含正整数")
            allowed_user_ids.add(user_id)

        raw_super_admin_id = str(
            rows.get("TELEGRAM_SUPER_ADMIN_ID")
            or rows.get("TELEGRAM_SUPER_ADMIN_USER_ID")
            or ""
        ).strip()
        super_admin_id: int | None = None
        if raw_super_admin_id:
            try:
                super_admin_id = int(raw_super_admin_id)
            except ValueError as exc:
                raise ValueError("TELEGRAM_SUPER_ADMIN_ID 必须是正整数") from exc
            if super_admin_id < 1 or raw_super_admin_id != str(super_admin_id):
                raise ValueError("TELEGRAM_SUPER_ADMIN_ID 必须是正整数")
        elif len(allowed_user_ids) == 1:
            # Backward compatibility: the original single allowlist entry was
            # already the only fully privileged user.
            super_admin_id = next(iter(allowed_user_ids))

        raw_ttl = str(
            rows.get("TELEGRAM_SECRET_MESSAGE_TTL_SECONDS")
            or rows.get("TELEGRAM_SECRET_MESSAGE_TTL")
            or "60"
        ).strip()
        try:
            secret_message_ttl = int(raw_ttl)
        except ValueError as exc:
            raise ValueError("TELEGRAM_SECRET_MESSAGE_TTL 必须是非负整数") from exc
        if secret_message_ttl < 0:
            raise ValueError("TELEGRAM_SECRET_MESSAGE_TTL 必须是非负整数")

        raw_store_path = str(
            rows.get("TELEGRAM_ADMIN_STORE_PATH") or ".telegram-admins.json"
        ).strip()
        if not raw_store_path:
            raise ValueError("TELEGRAM_ADMIN_STORE_PATH 不能为空")
        admin_store_path = Path(raw_store_path)
        if not admin_store_path.is_absolute():
            admin_store_path = Path(path).resolve().parent / admin_store_path

        return cls(
            token=token,
            allowed_user_ids=frozenset(allowed_user_ids),
            secret_message_ttl=secret_message_ttl,
            super_admin_id=super_admin_id,
            admin_store_path=admin_store_path,
        )


def main_menu_keyboard(
    *,
    include_admin_management: bool = False,
) -> ReplyKeyboardMarkup:
    rows = [
        [BTN_AGENT, BTN_PASSWORD],
        [BTN_ROLE],
    ]
    if include_admin_management:
        rows.append([BTN_ADMIN])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="请选择功能",
    )


def password_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BTN_QUERY_PASSWORD, BTN_UPDATE_PASSWORD],
            [BTN_BACK],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="请选择查密或改密",
    )


def role_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BTN_QUERY_ROLE, BTN_SERVERS],
            [BTN_BAN, BTN_UNBAN],
            [BTN_MUTE, BTN_UNMUTE],
            [BTN_BACK],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="请选择角色操作",
    )


def input_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BTN_BACK]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="请输入内容",
    )


def confirmation_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BTN_CONFIRM, BTN_CANCEL],
            [BTN_BACK],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="请确认或取消",
    )


def admin_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BTN_ADMIN_LIST],
            [BTN_ADMIN_ADD, BTN_ADMIN_REMOVE],
            [BTN_BACK],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="请选择管理员操作",
    )


def admin_confirmation_keyboard(action: str) -> ReplyKeyboardMarkup:
    confirm_button = (
        BTN_ADMIN_CONFIRM_ADD if action == "add" else BTN_ADMIN_CONFIRM_REMOVE
    )
    return ReplyKeyboardMarkup(
        [
            [confirm_button, BTN_ADMIN_CANCEL],
            [BTN_BACK],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="请确认管理员变更",
    )


def server_keyboard(servers: list[dict[str, object]]) -> ReplyKeyboardMarkup:
    labels = [
        f"{server['serverId']} {server['serverName']}"
        for server in servers
    ]
    rows = [labels[index : index + 2] for index in range(0, len(labels), 2)]
    rows.append([BTN_BACK])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="请选择区服",
    )


def require_user_data(
    context: ContextTypes.DEFAULT_TYPE,
) -> dict[object, object]:
    user_data = context.user_data
    if user_data is None:
        raise RuntimeError("Telegram update does not provide user_data")
    return user_data


class TelegramBotController:
    def __init__(
        self,
        processor: BotCommandProcessor,
        settings: TelegramBotSettings,
        admin_registry: TelegramAdminRegistry | None = None,
    ) -> None:
        self.processor = processor
        self.settings = settings
        inferred_super_admin = settings.super_admin_id
        if inferred_super_admin is None and len(settings.allowed_user_ids) == 1:
            inferred_super_admin = next(iter(settings.allowed_user_ids))
        self.admin_registry = admin_registry or TelegramAdminRegistry(
            super_admin_id=inferred_super_admin,
            static_admin_ids=(
                settings.allowed_user_ids - {inferred_super_admin}
                if inferred_super_admin is not None
                else settings.allowed_user_ids
            ),
        )
        self._secret_deletion_tasks: set[asyncio.Task[None]] = set()

    def register(self, application: Application) -> None:
        application.add_handler(CommandHandler(["start", "menu"], self.start))
        application.add_handler(CommandHandler("whoami", self.whoami))
        application.add_handler(CommandHandler("help", self.help))
        application.add_handler(
            CommandHandler(
                ["admins", "add_admin", "remove_admin"],
                self.admin_command,
            )
        )
        application.add_handler(MessageHandler(filters.COMMAND, self.command))
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.text)
        )
        application.add_error_handler(self.error)

    async def start(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._guard(update):
            return
        user = update.effective_user
        if user is None:
            return
        await self._cancel_pending(user.id)
        self._reset_state(context)
        await self._reply(
            update,
            "请选择功能。",
            reply_markup=self._main_menu_keyboard(user.id),
        )

    async def whoami(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context
        user = update.effective_user
        if user is None:
            return
        status = self.admin_registry.role(user.id)
        await self._reply(
            update,
            f"你的 Telegram 用户 ID：{user.id}\n状态：{status}",
        )

    async def help(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._guard(update):
            return
        user = update.effective_user
        if user is None:
            return
        await self._cancel_pending(user.id)
        self._reset_state(context)
        await self._reply(
            update,
            self.processor.help_text(),
            reply_markup=self._main_menu_keyboard(user.id),
        )

    async def admin_command(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._guard_super_admin(update):
            return
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None or not message.text:
            return
        await self._cancel_pending(user.id)
        try:
            parts = shlex.split(message.text.strip(), posix=True)
        except ValueError as exc:
            await self._reply(
                update,
                f"命令格式错误：{exc}",
                reply_markup=admin_menu_keyboard(),
            )
            return
        command_name = parts[0].lstrip("/").split("@", 1)[0].casefold()
        if command_name == "admins":
            if len(parts) != 1:
                await self._reply(
                    update,
                    "用法：/admins",
                    reply_markup=admin_menu_keyboard(),
                )
                return
            self._set_state(context, STATE_ADMIN_MENU)
            await self._show_admin_list(update)
            return
        if len(parts) != 2:
            usage = (
                "/add_admin <Telegram用户ID>"
                if command_name == "add_admin"
                else "/remove_admin <Telegram用户ID>"
            )
            await self._reply(
                update,
                f"用法：{usage}",
                reply_markup=admin_menu_keyboard(),
            )
            return
        action = "add" if command_name == "add_admin" else "remove"
        try:
            await self._stage_admin_change(update, context, action, parts[1])
        except Exception as exc:  # noqa: BLE001 - return a user-safe admin error.
            logger.warning(
                "Telegram admin command failed for user_id=%s: %s",
                user.id,
                type(exc).__name__,
            )
            self._set_state(context, STATE_ADMIN_MENU)
            await self._reply(
                update,
                f"管理员操作失败：{exc}",
                reply_markup=admin_menu_keyboard(),
            )

    async def command(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        message = update.effective_message
        if message is None or not message.text:
            return

        raw_command = message.text
        command_name = raw_command.split(maxsplit=1)[0].lstrip("/").split("@", 1)[0].casefold()
        if command_name == "set_password":
            try:
                await message.delete()
            except TelegramError:
                pass
        if not await self._guard(update):
            return
        user = update.effective_user
        if user is None:
            return

        if command_name not in {"confirm", "cancel"}:
            await self._cancel_pending(user.id)
        response = await self._run_processor(user.id, raw_command)
        protect_content = command_name == "password"
        reply_markup = await self._reply_markup_for_pending(context, user.id)
        sent = await self._reply(
            update,
            response,
            reply_markup=reply_markup,
            protect_content=protect_content,
        )
        if protect_content and sent is not None:
            self._schedule_secret_deletion(sent)

    async def text(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._guard(update):
            return
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None or message.text is None:
            return

        user_data = require_user_data(context)
        raw_text = message.text
        text = raw_text.strip()
        if text == BTN_BACK:
            await self._cancel_pending(user.id)
            self._reset_state(context)
            await self._reply(
                update,
                "已返回主菜单。",
                reply_markup=self._main_menu_keyboard(user.id),
            )
            return
        if text == BTN_ADMIN_CANCEL:
            if not self.admin_registry.is_super_admin(user.id):
                self._reset_state(context)
                await self._reply(
                    update,
                    "只有总管理员可以管理其他管理员。",
                    reply_markup=self._main_menu_keyboard(user.id),
                )
                return
            self._set_state(context, STATE_ADMIN_MENU)
            await self._reply(
                update,
                "已取消管理员变更。",
                reply_markup=admin_menu_keyboard(),
            )
            return
        if text == BTN_CANCEL:
            response = await self._run_processor(user.id, "/cancel")
            self._reset_state(context)
            await self._reply(
                update,
                response,
                reply_markup=self._main_menu_keyboard(user.id),
            )
            return
        if text in {BTN_ADMIN_CONFIRM_ADD, BTN_ADMIN_CONFIRM_REMOVE}:
            await self._confirm_admin_change(update, context, user.id, text)
            return
        if text == BTN_CONFIRM:
            await self._confirm_pending(update, context, user.id)
            return

        if text == BTN_AGENT:
            await self._cancel_pending(user.id)
            user_data[STATE_KEY] = STATE_AGENT_ACCOUNT
            await self._reply(
                update,
                "请输入玩家账号；跨游戏重复时可在账号后加 game_id。",
                reply_markup=input_keyboard(),
            )
            return
        if text == BTN_PASSWORD:
            await self._cancel_pending(user.id)
            user_data[STATE_KEY] = STATE_PASSWORD_MENU
            await self._reply(
                update,
                "请选择查询密码或修改密码。",
                reply_markup=password_menu_keyboard(),
            )
            return
        if text == BTN_ROLE:
            await self._cancel_pending(user.id)
            user_data[STATE_KEY] = STATE_ROLE_MENU
            await self._reply(
                update,
                "请选择昵称查询或角色操作。",
                reply_markup=role_menu_keyboard(),
            )
            return
        if text == BTN_ADMIN:
            if not self.admin_registry.is_super_admin(user.id):
                await self._reply(
                    update,
                    "只有总管理员可以管理其他管理员。",
                    reply_markup=self._main_menu_keyboard(user.id),
                )
                return
            await self._cancel_pending(user.id)
            self._set_state(context, STATE_ADMIN_MENU)
            await self._reply(
                update,
                "请选择管理员操作。",
                reply_markup=admin_menu_keyboard(),
            )
            return

        state = str(user_data.get(STATE_KEY) or STATE_MAIN)
        try:
            if state == STATE_AGENT_ACCOUNT:
                await self._handle_agent_input(update, context, user.id, text)
            elif state == STATE_PASSWORD_MENU:
                await self._handle_password_menu(update, context, text)
            elif state == STATE_PASSWORD_QUERY_ACCOUNT:
                await self._handle_password_query(update, context, user.id, text)
            elif state == STATE_PASSWORD_UPDATE_ACCOUNT:
                await self._handle_password_account(update, context, text)
            elif state == STATE_PASSWORD_NEW_VALUE:
                await self._handle_new_password(update, context, user.id, raw_text)
            elif state == STATE_ROLE_MENU:
                await self._handle_role_menu(update, context, user.id, text)
            elif state == STATE_ROLE_SERVER:
                await self._handle_server_choice(update, context, text)
            elif state == STATE_ROLE_NICKNAME:
                await self._handle_role_nickname(update, context, user.id, text)
            elif state == STATE_ADMIN_MENU:
                await self._handle_admin_menu(update, context, text)
            elif state == STATE_ADMIN_ADD_ID:
                await self._stage_admin_change(update, context, "add", text)
            elif state == STATE_ADMIN_REMOVE_ID:
                await self._stage_admin_change(update, context, "remove", text)
            elif state == STATE_ADMIN_CONFIRM:
                await self._reply(
                    update,
                    "请确认或取消管理员变更。",
                    reply_markup=admin_confirmation_keyboard(
                        str(user_data.get(ADMIN_ACTION_KEY) or "")
                    ),
                )
            elif state == STATE_CONFIRM:
                await self._reply(
                    update,
                    "请点击“确认操作”或“取消操作”。",
                    reply_markup=confirmation_keyboard(),
                )
            else:
                await self._reply(
                    update,
                    "请使用下方按钮选择功能。",
                    reply_markup=self._main_menu_keyboard(user.id),
                )
        except Exception as exc:  # noqa: BLE001 - convert workflow errors to user-safe text.
            logger.warning(
                "Telegram workflow failed for user_id=%s: %s",
                user.id,
                type(exc).__name__,
            )
            await self._cancel_pending(user.id)
            self._reset_state(context)
            await self._reply(
                update,
                f"操作失败：{exc}",
                reply_markup=self._main_menu_keyboard(user.id),
            )

    async def _handle_agent_input(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: int,
        text: str,
    ) -> None:
        response = await self._run_processor(user_id, f"/agent {text}")
        self._reset_state(context)
        await self._reply(
            update,
            response,
            reply_markup=self._main_menu_keyboard(user_id),
        )

    async def _handle_password_menu(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> None:
        user_data = require_user_data(context)
        if text == BTN_QUERY_PASSWORD:
            user_data[STATE_KEY] = STATE_PASSWORD_QUERY_ACCOUNT
            await self._reply(
                update,
                "请输入需要查询密码的玩家账号。",
                reply_markup=input_keyboard(),
            )
            return
        if text == BTN_UPDATE_PASSWORD:
            user_data[STATE_KEY] = STATE_PASSWORD_UPDATE_ACCOUNT
            await self._reply(
                update,
                "请输入需要修改密码的玩家账号。",
                reply_markup=input_keyboard(),
            )
            return
        await self._reply(
            update,
            "请使用下方按钮选择查询密码或修改密码。",
            reply_markup=password_menu_keyboard(),
        )

    async def _handle_password_query(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: int,
        account: str,
    ) -> None:
        response = await self._run_processor(
            user_id,
            f"/password {shlex.quote(account)}",
        )
        self._reset_state(context)
        sent = await self._reply(
            update,
            response,
            reply_markup=self._main_menu_keyboard(user_id),
            protect_content=True,
        )
        if sent is not None:
            self._schedule_secret_deletion(sent)

    async def _handle_password_account(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        account: str,
    ) -> None:
        user_data = require_user_data(context)
        user = await asyncio.to_thread(
            self.processor.service.find_game_user,
            account,
        )
        canonical_account = str(user.get("account") or "")
        user_data[PASSWORD_ACCOUNT_KEY] = canonical_account
        user_data[STATE_KEY] = STATE_PASSWORD_NEW_VALUE
        await self._reply(
            update,
            f"账号已确认：{canonical_account}\n请输入新密码；该消息收到后会立即删除。",
            reply_markup=input_keyboard(),
        )

    async def _handle_new_password(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: int,
        new_password: str,
    ) -> None:
        user_data = require_user_data(context)
        message = update.effective_message
        account = str(user_data.get(PASSWORD_ACCOUNT_KEY) or "")
        if message is not None:
            try:
                await message.delete()
            except TelegramError:
                pass
        response = await self._run_processor(
            user_id,
            f"/set_password {shlex.quote(account)} {shlex.quote(new_password)}",
        )
        challenge = await self._pending_challenge(user_id)
        if challenge is not None:
            user_data[CONFIRMATION_TOKEN_KEY] = challenge.token
            user_data[STATE_KEY] = STATE_CONFIRM
            reply_markup = confirmation_keyboard()
        else:
            self._reset_state(context)
            reply_markup = self._main_menu_keyboard(user_id)
        await self._reply(
            update,
            response,
            reply_markup=reply_markup,
        )

    async def _handle_role_menu(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: int,
        text: str,
    ) -> None:
        user_data = require_user_data(context)
        if text == BTN_SERVERS:
            response = await self._run_processor(
                user_id,
                "/servers",
            )
            await self._reply(
                update,
                response,
                reply_markup=role_menu_keyboard(),
            )
            return
        action = ROLE_BUTTON_ACTIONS.get(text)
        if action is None:
            await self._reply(
                update,
                "请选择昵称查询、封禁、解封、禁言或解除禁言。",
                reply_markup=role_menu_keyboard(),
            )
            return
        user_data[ROLE_ACTION_KEY] = action
        user_data[STATE_KEY] = STATE_ROLE_SERVER
        servers = await asyncio.to_thread(self.processor.service.list_servers)
        await self._reply(
            update,
            "请选择后台区服。",
            reply_markup=server_keyboard(servers),
        )

    async def _handle_server_choice(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> None:
        user_data = require_user_data(context)
        first = text.split(maxsplit=1)[0] if text else ""
        try:
            server_id = int(first)
        except ValueError:
            server_id = 0
        servers = await asyncio.to_thread(self.processor.service.list_servers)
        selected = next(
            (server for server in servers if server["serverId"] == server_id),
            None,
        )
        if selected is None:
            await self._reply(
                update,
                "区服选择无效，请点击列表中的区服。",
                reply_markup=server_keyboard(servers),
            )
            return
        user_data[SERVER_ID_KEY] = server_id
        user_data[STATE_KEY] = STATE_ROLE_NICKNAME
        action = str(user_data.get(ROLE_ACTION_KEY) or "query")
        prompt = "请输入昵称片段。" if action == "query" else "请输入完整玩家昵称。"
        await self._reply(
            update,
            f"已选择：{selected['serverName']} ({server_id})\n{prompt}",
            reply_markup=input_keyboard(),
        )

    async def _handle_role_nickname(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: int,
        nickname: str,
    ) -> None:
        user_data = require_user_data(context)
        server_id = TLBBBotService._positive_int(
            user_data.get(SERVER_ID_KEY),
            "区服 ID",
        )
        action = str(user_data.get(ROLE_ACTION_KEY) or "query")
        if action == "query":
            command = f"/roles {server_id} {shlex.quote(nickname)}"
        else:
            command = f"/role {action} {server_id} {shlex.quote(nickname)}"
        response = await self._run_processor(user_id, command)
        challenge = await self._pending_challenge(user_id)
        if challenge is not None:
            user_data[CONFIRMATION_TOKEN_KEY] = challenge.token
            user_data[STATE_KEY] = STATE_CONFIRM
            reply_markup = confirmation_keyboard()
        else:
            user_data[STATE_KEY] = STATE_ROLE_MENU
            reply_markup = role_menu_keyboard()
        await self._reply(
            update,
            response,
            reply_markup=reply_markup,
        )

    async def _handle_admin_menu(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> None:
        user = update.effective_user
        if user is None or not self.admin_registry.is_super_admin(user.id):
            self._reset_state(context)
            await self._reply(
                update,
                "只有总管理员可以管理其他管理员。",
                reply_markup=(
                    self._main_menu_keyboard(user.id)
                    if user is not None
                    else None
                ),
            )
            return
        if text == BTN_ADMIN_LIST:
            await self._show_admin_list(update)
            return
        if text == BTN_ADMIN_ADD:
            self._set_state(context, STATE_ADMIN_ADD_ID)
            await self._reply(
                update,
                "请输入要新增的管理员 Telegram 数字用户 ID。\n"
                "对方可先私聊机器人发送 /whoami 获取。",
                reply_markup=input_keyboard(),
            )
            return
        if text == BTN_ADMIN_REMOVE:
            self._set_state(context, STATE_ADMIN_REMOVE_ID)
            await self._reply(
                update,
                "请输入要删除的管理员 Telegram 数字用户 ID。",
                reply_markup=input_keyboard(),
            )
            return
        await self._reply(
            update,
            "请选择管理员列表、新增管理员或删除管理员。",
            reply_markup=admin_menu_keyboard(),
        )

    async def _show_admin_list(self, update: Update) -> None:
        super_admin_id = self.admin_registry.super_admin_id
        admin_ids = self.admin_registry.list_admin_ids()
        lines = [
            f"总管理员：{super_admin_id if super_admin_id is not None else '未配置'}",
            f"普通管理员：{len(admin_ids)} 人",
        ]
        lines.extend(str(user_id) for user_id in admin_ids)
        if not admin_ids:
            lines.append("（暂无）")
        await self._reply(
            update,
            "\n".join(lines),
            reply_markup=admin_menu_keyboard(),
        )

    async def _stage_admin_change(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        action: str,
        raw_user_id: str,
    ) -> None:
        actor = update.effective_user
        if actor is None or not self.admin_registry.is_super_admin(actor.id):
            await self._reply(update, "只有总管理员可以管理其他管理员。")
            return
        target_id = self._parse_admin_user_id(raw_user_id)
        if action == "add":
            if self.admin_registry.is_super_admin(target_id):
                raise ValueError("该用户已经是总管理员")
            if self.admin_registry.is_authorized(target_id):
                raise ValueError("该用户已经是管理员")
            summary = f"确认新增管理员：{target_id}"
        elif action == "remove":
            if self.admin_registry.is_super_admin(target_id):
                raise ValueError("不能删除总管理员")
            if target_id in self.admin_registry.static_admin_ids:
                raise ValueError("该管理员来自 .env，请修改配置后重启机器人")
            if not self.admin_registry.is_authorized(target_id):
                raise ValueError("该用户不是管理员")
            summary = f"确认删除管理员：{target_id}"
        else:
            raise ValueError("管理员操作类型无效")

        self._reset_state(context)
        user_data = require_user_data(context)
        user_data[STATE_KEY] = STATE_ADMIN_CONFIRM
        user_data[ADMIN_ACTION_KEY] = action
        user_data[ADMIN_TARGET_KEY] = target_id
        user_data[ADMIN_PENDING_AT_KEY] = time.monotonic()
        await self._reply(
            update,
            f"{summary}\n管理员权限变更会立即生效。",
            reply_markup=admin_confirmation_keyboard(action),
        )

    async def _confirm_admin_change(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: int,
        button_text: str,
    ) -> None:
        if not self.admin_registry.is_super_admin(user_id):
            self._reset_state(context)
            await self._reply(update, "只有总管理员可以管理其他管理员。")
            return
        user_data = require_user_data(context)
        action = str(user_data.get(ADMIN_ACTION_KEY) or "")
        target_id = user_data.get(ADMIN_TARGET_KEY)
        pending_at = user_data.get(ADMIN_PENDING_AT_KEY)
        expected_button = (
            BTN_ADMIN_CONFIRM_ADD if action == "add" else BTN_ADMIN_CONFIRM_REMOVE
        )
        if (
            user_data.get(STATE_KEY) != STATE_ADMIN_CONFIRM
            or button_text != expected_button
            or not isinstance(target_id, int)
            or not isinstance(pending_at, (int, float))
        ):
            self._set_state(context, STATE_ADMIN_MENU)
            await self._reply(
                update,
                "当前没有待确认的管理员变更。",
                reply_markup=admin_menu_keyboard(),
            )
            return
        if time.monotonic() - pending_at > ADMIN_CONFIRMATION_TTL_SECONDS:
            self._set_state(context, STATE_ADMIN_MENU)
            await self._reply(
                update,
                "管理员变更确认已过期，请重新发起。",
                reply_markup=admin_menu_keyboard(),
            )
            return

        try:
            if action == "add":
                changed = await asyncio.to_thread(self.admin_registry.add, target_id)
                response = (
                    f"已新增管理员：{target_id}\n对方现在可以私聊机器人发送 /start。"
                    if changed
                    else f"用户 {target_id} 已经是管理员。"
                )
            else:
                changed = await asyncio.to_thread(self.admin_registry.remove, target_id)
                if changed:
                    await asyncio.to_thread(
                        self.processor.discard_pending,
                        str(target_id),
                    )
                    self._clear_user_workflow(context, target_id)
                    response = f"已删除管理员：{target_id}\n该用户的未确认操作已失效。"
                else:
                    response = f"用户 {target_id} 已不是管理员。"
        except Exception as exc:  # noqa: BLE001 - return a user-safe admin error.
            logger.warning(
                "Telegram admin confirmation failed for user_id=%s: %s",
                user_id,
                type(exc).__name__,
            )
            self._set_state(context, STATE_ADMIN_MENU)
            await self._reply(
                update,
                f"管理员操作失败：{exc}",
                reply_markup=admin_menu_keyboard(),
            )
            return

        self._reset_state(context)
        await self._reply(
            update,
            response,
            reply_markup=self._main_menu_keyboard(user_id),
        )

    @staticmethod
    def _parse_admin_user_id(raw_user_id: str) -> int:
        value = str(raw_user_id).strip()
        if not re.fullmatch(r"[1-9]\d*", value):
            raise ValueError("Telegram 用户 ID 必须是正整数")
        return int(value)

    async def _confirm_pending(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: int,
    ) -> None:
        user_data = require_user_data(context)
        token = str(user_data.get(CONFIRMATION_TOKEN_KEY) or "")
        if not token:
            challenge = await self._pending_challenge(user_id)
            token = challenge.token if challenge is not None else ""
        if not token:
            await self._reply(
                update,
                "当前没有待确认操作。",
                reply_markup=self._main_menu_keyboard(user_id),
            )
            return
        response = await self._run_processor(user_id, f"/confirm {token}")
        self._reset_state(context)
        await self._reply(
            update,
            response,
            reply_markup=self._main_menu_keyboard(user_id),
        )

    async def _run_processor(self, user_id: int, command: str) -> str:
        return await asyncio.to_thread(
            self.processor.handle,
            str(user_id),
            command,
            is_private=True,
        )

    async def _pending_challenge(
        self,
        user_id: int,
    ) -> ConfirmationChallenge | None:
        return await asyncio.to_thread(
            self.processor.get_pending_challenge,
            str(user_id),
        )

    async def _cancel_pending(self, user_id: int) -> None:
        challenge = await self._pending_challenge(user_id)
        if challenge is not None:
            await self._run_processor(user_id, "/cancel")

    def _main_menu_keyboard(self, user_id: int) -> ReplyKeyboardMarkup:
        return main_menu_keyboard(
            include_admin_management=self.admin_registry.is_super_admin(user_id)
        )

    async def _guard(self, update: Update) -> bool:
        chat = update.effective_chat
        user = update.effective_user
        if chat is None or user is None:
            return False
        if chat.type != ChatType.PRIVATE:
            await self._reply(update, "请私聊机器人使用后台功能。")
            return False
        if not self.admin_registry.is_authorized(user.id):
            await self._reply(
                update,
                (
                    f"当前用户未授权。\n你的 Telegram 用户 ID：{user.id}\n"
                    "请将该 ID 提供给总管理员，由总管理员在机器人中新增。"
                ),
            )
            return False
        return True

    async def _guard_super_admin(self, update: Update) -> bool:
        if not await self._guard(update):
            return False
        user = update.effective_user
        if user is None or not self.admin_registry.is_super_admin(user.id):
            await self._reply(update, "只有总管理员可以管理其他管理员。")
            return False
        return True

    async def _reply(
        self,
        update: Update,
        text: str,
        *,
        reply_markup: ReplyKeyboardMarkup | None = None,
        protect_content: bool = False,
    ) -> Message | None:
        message = update.effective_message
        if message is None:
            return None
        chunks = [
            text[index : index + TELEGRAM_MESSAGE_CHUNK_SIZE]
            for index in range(0, max(1, len(text)), TELEGRAM_MESSAGE_CHUNK_SIZE)
        ]
        sent: Message | None = None
        for index, chunk in enumerate(chunks):
            sent = await message.reply_text(
                chunk,
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
                protect_content=protect_content,
                do_quote=False,
            )
        return sent

    async def _reply_markup_for_pending(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: int,
    ) -> ReplyKeyboardMarkup:
        user_data = require_user_data(context)
        challenge = await self._pending_challenge(user_id)
        if challenge is not None:
            user_data[CONFIRMATION_TOKEN_KEY] = challenge.token
            user_data[STATE_KEY] = STATE_CONFIRM
            return confirmation_keyboard()
        self._reset_state(context)
        return self._main_menu_keyboard(user_id)

    def _schedule_secret_deletion(
        self,
        message: Message,
    ) -> None:
        if self.settings.secret_message_ttl < 1:
            return
        task = asyncio.create_task(
            self._delete_later(message, self.settings.secret_message_ttl),
            name=f"delete-secret-message-{message.message_id}",
        )
        self._secret_deletion_tasks.add(task)
        task.add_done_callback(self._secret_deletion_tasks.discard)

    @staticmethod
    async def _delete_later(message: Message, delay: int) -> None:
        await asyncio.sleep(delay)
        retry_delay = NETWORK_RESTART_DELAY_SECONDS
        while True:
            try:
                await message.delete()
                return
            except TelegramError as exc:
                if not _is_retryable_startup_error(exc):
                    return
            except RuntimeError as exc:
                if "not initialized" not in str(exc).casefold():
                    raise
            await asyncio.sleep(retry_delay)
            retry_delay = min(
                retry_delay * 2,
                NETWORK_RESTART_MAX_DELAY_SECONDS,
            )

    @staticmethod
    def _reset_state(context: ContextTypes.DEFAULT_TYPE) -> None:
        user_data = require_user_data(context)
        for key in (
            STATE_KEY,
            CONFIRMATION_TOKEN_KEY,
            ROLE_ACTION_KEY,
            SERVER_ID_KEY,
            PASSWORD_ACCOUNT_KEY,
            ADMIN_ACTION_KEY,
            ADMIN_TARGET_KEY,
            ADMIN_PENDING_AT_KEY,
        ):
            user_data.pop(key, None)
        user_data[STATE_KEY] = STATE_MAIN

    @classmethod
    def _set_state(
        cls,
        context: ContextTypes.DEFAULT_TYPE,
        state: str,
    ) -> None:
        cls._reset_state(context)
        require_user_data(context)[STATE_KEY] = state

    @staticmethod
    def _clear_user_workflow(
        context: ContextTypes.DEFAULT_TYPE,
        user_id: int,
    ) -> None:
        application = getattr(context, "application", None)
        all_user_data = getattr(application, "user_data", None)
        if all_user_data is None:
            return
        target_data = all_user_data.get(user_id)
        if target_data is not None:
            target_data.clear()

    async def error(
        self,
        update: object,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del update
        error = context.error
        if _is_runtime_transport_error(error):
            application = context.application
            if not application.bot_data.get(NETWORK_RESTART_REQUESTED_KEY):
                application.bot_data[NETWORK_RESTART_REQUESTED_KEY] = True
                logger.warning(
                    "Telegram network failed (%s); restarting polling connection",
                    type(error).__name__,
                )
                application.stop_running()
            return
        logger.error(
            "Telegram handler failed: %s",
            type(error).__name__,
            exc_info=error,
        )


def build_application(
    env_path: str | Path = ".env",
    *,
    timeout: float = 25.0,
) -> tuple[Application, TelegramBotSettings]:
    env_path = Path(env_path)
    settings = TelegramBotSettings.from_env_file(env_path)
    agent_client = TLBBBackendClient.from_env_file(
        env_path,
        timeout=max(1, int(timeout)),
    )
    admin_client = BanAdminClient(
        load_config(env_path),
        timeout=timeout,
    )
    static_admin_ids = set(settings.allowed_user_ids)
    if settings.super_admin_id is not None:
        static_admin_ids.discard(settings.super_admin_id)
    admin_registry = TelegramAdminRegistry(
        super_admin_id=settings.super_admin_id,
        static_admin_ids=static_admin_ids,
        store_path=settings.admin_store_path,
    )
    processor = BotCommandProcessor(
        TLBBBotService(agent_client, admin_client),
        authorization_checker=admin_registry.is_authorized,
    )
    controller = TelegramBotController(processor, settings, admin_registry)

    async def post_init(app: Application) -> None:
        bot_user = await app.bot.get_me()
        private_commands = [
            BotCommand("start", "显示主菜单"),
            BotCommand("menu", "显示主菜单"),
            BotCommand("whoami", "查看 Telegram 用户 ID"),
            BotCommand("servers", "查看后台区服"),
            BotCommand("cancel", "取消待确认操作"),
            BotCommand("help", "查看命令帮助"),
        ]
        await app.bot.set_my_commands(
            private_commands,
            scope=BotCommandScopeAllPrivateChats(),
        )
        if settings.super_admin_id is not None:
            await app.bot.set_my_commands(
                [
                    *private_commands,
                    BotCommand("admins", "查看管理员列表"),
                    BotCommand("add_admin", "新增管理员"),
                    BotCommand("remove_admin", "删除管理员"),
                ],
                scope=BotCommandScopeChat(settings.super_admin_id),
            )
        authorized_count = len(admin_registry.list_admin_ids()) + (
            1 if settings.super_admin_id is not None else 0
        )
        logger.info(
            "Telegram bot connected: id=%s username=%s authorized_users=%s",
            bot_user.id,
            bot_user.username,
            authorized_count,
        )
        if settings.super_admin_id is None:
            logger.warning(
                "TELEGRAM_SUPER_ADMIN_ID is not configured; administrator "
                "management is disabled."
            )
        if authorized_count == 0:
            logger.warning("No Telegram users are authorized; backend commands are locked.")

    application = (
        Application.builder()
        .token(settings.token)
        .concurrent_updates(False)
        .post_init(post_init)
        .build()
    )
    controller.register(application)
    application.bot_data["tlbb_controller"] = controller
    application.bot_data["tlbb_admin_registry"] = admin_registry
    return application, settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=".env")
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate Telegram connectivity, install private-chat commands, and exit.",
    )
    parser.add_argument(
        "--keep-pending-updates",
        action="store_true",
        help="Process Telegram updates queued before startup.",
    )
    return parser


async def check_application(application: Application) -> None:
    await application.initialize()
    try:
        if application.post_init is not None:
            await application.post_init(application)
    finally:
        await application.shutdown()


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.request").setLevel(logging.WARNING)
    try:
        application, _ = build_application(args.env, timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001 - startup must report configuration failures.
        logger.error("Telegram bot startup failed: %s", exc)
        return 1
    if args.check:
        try:
            asyncio.run(check_application(application))
        except Exception as exc:  # noqa: BLE001 - health check must report API failures.
            logger.error("Telegram bot connectivity check failed: %s", exc)
            return 1
        return 0
    drop_pending_updates = not args.keep_pending_updates
    next_restart_delay = NETWORK_RESTART_DELAY_SECONDS
    while True:
        restart_requested = False
        run_started_at = time.monotonic()
        try:
            application.run_polling(
                allowed_updates=Update.ALL_TYPES,
                bootstrap_retries=0,
                drop_pending_updates=drop_pending_updates,
                close_loop=False,
                stop_signals=None,
            )
            restart_requested = bool(
                application.bot_data.get(NETWORK_RESTART_REQUESTED_KEY)
            )
        except NetworkError as exc:
            if not _is_retryable_startup_error(exc):
                logger.error(
                    "Telegram polling stopped on API error (%s): %s",
                    type(exc).__name__,
                    exc,
                )
                return 1
            restart_requested = True
            logger.warning(
                "Telegram polling startup failed (%s); retrying",
                type(exc).__name__,
            )
        except Exception as exc:  # noqa: BLE001 - polling failures must be visible.
            logger.error("Telegram polling stopped unexpectedly: %s", exc)
            return 1

        if not restart_requested:
            return 0

        if time.monotonic() - run_started_at >= NETWORK_STABLE_RESET_SECONDS:
            next_restart_delay = NETWORK_RESTART_DELAY_SECONDS
        logger.warning(
            "Restarting Telegram polling in %s seconds",
            next_restart_delay,
        )
        time.sleep(next_restart_delay)
        next_restart_delay = min(
            next_restart_delay * 2,
            NETWORK_RESTART_MAX_DELAY_SECONDS,
        )
        application.bot_data.pop(NETWORK_RESTART_REQUESTED_KEY, None)
        # Updates queued during the outage must be processed after reconnecting.
        drop_pending_updates = False


if __name__ == "__main__":
    raise SystemExit(main())
