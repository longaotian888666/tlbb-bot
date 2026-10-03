"""Platform-neutral command service for the TLBB administration bot."""

from __future__ import annotations

import logging
import secrets
import shlex
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from tlbb_backend import TLBBBackendClient, TLBBBackendError

from .ban_admin_login import (
    BanAdminAPIError,
    BanAdminClient,
    BanAdminContractError,
    BanAdminHTTPError,
    GameUserPassword,
)


logger = logging.getLogger(__name__)

RoleActionName = Literal["ban", "unban", "mute", "unmute"]
PendingKind = Literal["role_action", "password_update"]

ROLE_ACTION_LABELS: dict[RoleActionName, str] = {
    "ban": "封禁",
    "unban": "解除封禁",
    "mute": "禁言",
    "unmute": "解除禁言",
}

ROLE_ACTION_ALIASES: dict[str, RoleActionName] = {
    "ban": "ban",
    "封禁": "ban",
    "封号": "ban",
    "unban": "unban",
    "解除封禁": "unban",
    "解封": "unban",
    "mute": "mute",
    "禁言": "mute",
    "unmute": "unmute",
    "解除禁言": "unmute",
    "解禁": "unmute",
}

COMMAND_ALIASES = {
    "start": "help",
    "help": "help",
    "帮助": "help",
    "agent": "agent",
    "代理": "agent",
    "servers": "servers",
    "区服": "servers",
    "roles": "roles",
    "昵称": "roles",
    "role": "role",
    "操作": "role",
    "password": "password",
    "查密": "password",
    "set_password": "set_password",
    "改密": "set_password",
    "confirm": "confirm",
    "确认": "confirm",
    "cancel": "cancel",
    "取消": "cancel",
}

PRIVATE_ONLY_COMMANDS = {
    "password",
    "set_password",
}

TEAM_369_AGENT_IDS = frozenset(
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
)


def is_369_team_agent(agent_id: object) -> bool:
    """Return whether a direct agent ID belongs to the configured 369 team."""
    if isinstance(agent_id, bool):
        return False
    try:
        normalized = int(agent_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return normalized in TEAM_369_AGENT_IDS


class BotServiceError(RuntimeError):
    """Raised when a bot workflow cannot be completed safely."""


@dataclass(frozen=True)
class PendingOperation:
    token: str
    actor_id: str
    kind: PendingKind
    summary: str
    expires_at: float
    payload: dict[str, object] = field(repr=False)


@dataclass(frozen=True)
class ConfirmationChallenge:
    token: str
    kind: PendingKind
    summary: str
    expires_at: float


class TLBBBotService:
    """Thread-safe facade over the agent and ban-admin clients."""

    def __init__(
        self,
        agent_client: TLBBBackendClient,
        admin_client: BanAdminClient,
    ) -> None:
        self.agent_client = agent_client
        self.admin_client = admin_client
        self._agent_lock = threading.RLock()
        self._admin_lock = threading.RLock()

    @staticmethod
    def _positive_int(value: object, label: str) -> int:
        if isinstance(value, bool):
            raise BotServiceError(f"{label} 必须是正整数")
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise BotServiceError(f"{label} 必须是正整数") from exc
        if parsed < 1:
            raise BotServiceError(f"{label} 必须是正整数")
        return parsed

    def lookup_agents(
        self,
        account: str,
        *,
        game_id: int | str | None = None,
    ) -> list[dict]:
        with self._agent_lock:
            return self.agent_client.get_player_agents(account, game_id=game_id)

    def list_servers(self) -> list[dict[str, object]]:
        with self._admin_lock:
            rows = self.admin_client.get_servers()
            servers: list[dict[str, object]] = []
            for row in rows:
                try:
                    server_id = self._positive_int(row.get("ID"), "区服 ID")
                except BotServiceError:
                    continue
                name = str(row.get("configName") or "").strip()
                if name:
                    servers.append({"serverId": server_id, "serverName": name})
            if not servers:
                raise BotServiceError("后台没有返回有效区服")
            return servers

    def _resolve_server_unlocked(self, server_id: int) -> dict[str, object]:
        server_id = self._positive_int(server_id, "区服 ID")
        matches = [
            server
            for server in self.list_servers()
            if server["serverId"] == server_id
        ]
        if not matches:
            raise BotServiceError(f"未找到后台区服 ID {server_id}")
        if len(matches) > 1:
            raise BotServiceError(f"后台区服 ID {server_id} 存在重复配置")
        return matches[0]

    def find_similar_roles(
        self,
        *,
        nickname: str,
        server_id: int,
    ) -> dict[str, object]:
        nickname = str(nickname or "")
        if not nickname.strip():
            raise BotServiceError("玩家昵称不能为空")
        if nickname != nickname.strip():
            raise BotServiceError("玩家昵称不能包含首尾空白")

        with self._admin_lock:
            server = self._resolve_server_unlocked(server_id)
            resolved_server_id = self._positive_int(server["serverId"], "区服 ID")
            matches = self.admin_client.find_similar_roles(
                role_name=nickname,
                server_id=resolved_server_id,
                expected_server_name=str(server["serverName"]),
            )
            return {
                "query": nickname,
                "serverId": server["serverId"],
                "serverName": server["serverName"],
                "matches": matches,
            }

    def perform_role_action(
        self,
        *,
        action: RoleActionName,
        role_name: str,
        account: str,
        server_id: int,
        expected_server_name: str,
    ) -> dict[str, object]:
        if action not in ROLE_ACTION_LABELS:
            raise BotServiceError(f"不支持的角色操作：{action}")

        with self._admin_lock:
            server = self._resolve_server_unlocked(server_id)
            actual_server_name = str(server["serverName"])
            if actual_server_name != expected_server_name:
                raise BotServiceError(
                    f"区服配置已变化：原为 {expected_server_name!r}，当前为 {actual_server_name!r}"
                )

            resolved_server_id = self._positive_int(server["serverId"], "区服 ID")
            if action == "ban":
                response = self.admin_client.ban_role(
                    role_name=role_name,
                    server_id=resolved_server_id,
                    expected_server_name=actual_server_name,
                )
            elif action == "unban":
                response = self.admin_client.unban_role(
                    role_name=role_name,
                    server_id=resolved_server_id,
                    expected_server_name=actual_server_name,
                )
            elif action == "mute":
                response = self.admin_client.mute_role(
                    role_name=role_name,
                    server_id=resolved_server_id,
                    expected_server_name=actual_server_name,
                )
            else:
                response = self.admin_client.unmute_role(
                    role_name=role_name,
                    server_id=resolved_server_id,
                    expected_server_name=actual_server_name,
                )

            return {
                "action": action,
                "roleName": role_name,
                "account": account,
                "serverId": server["serverId"],
                "serverName": actual_server_name,
                "message": str(response.get("msg") or response.get("message") or ""),
            }

    def find_game_user(self, account: str) -> dict[str, object]:
        with self._admin_lock:
            return self.admin_client.find_game_user_by_account(account)

    def get_game_password(self, account: str) -> GameUserPassword:
        with self._admin_lock:
            return self.admin_client.get_game_user_password_by_account(account)

    def update_game_password(
        self,
        account: str,
        new_password: str,
        *,
        expected_user_id: int,
    ) -> dict[str, object]:
        expected_user_id = self._positive_int(expected_user_id, "游戏用户 ID")
        with self._admin_lock:
            current = self.admin_client.find_game_user_by_account(account)
            current_user_id = self._positive_int(current.get("ID"), "游戏用户 ID")
            if current_user_id != expected_user_id:
                raise BotServiceError(
                    f"账号 {account!r} 的用户 ID 已从 {expected_user_id} 变为 {current_user_id}"
                )
            canonical_account = str(current.get("account") or "")
            return self.admin_client.update_game_user_password(
                current_user_id,
                new_password,
                expected_account=canonical_account,
                verify=True,
            )


class BotCommandProcessor:
    """Parse text commands and enforce confirmations around write operations."""

    def __init__(
        self,
        service: TLBBBotService,
        *,
        allowed_actor_ids: set[str] | None = None,
        authorization_checker: Callable[[str], bool] | None = None,
        confirmation_ttl_seconds: int = 300,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if confirmation_ttl_seconds < 1:
            raise ValueError("confirmation_ttl_seconds must be positive")
        if allowed_actor_ids is not None and authorization_checker is not None:
            raise ValueError(
                "allowed_actor_ids and authorization_checker are mutually exclusive"
            )
        self.service = service
        self.allowed_actor_ids = (
            {str(actor_id) for actor_id in allowed_actor_ids}
            if allowed_actor_ids is not None
            else None
        )
        self.authorization_checker = authorization_checker
        self.confirmation_ttl_seconds = confirmation_ttl_seconds
        self.clock = clock
        self.token_factory = token_factory or (lambda: secrets.token_hex(3).upper())
        self._pending: dict[str, PendingOperation] = {}
        self._pending_lock = threading.Lock()

    @staticmethod
    def help_text() -> str:
        return "\n".join(
            [
                "可用命令：",
                "/agent <账号> [game_id]",
                "/servers",
                "/roles <区服ID> <昵称片段>",
                "/role <ban|unban|mute|unmute> <区服ID> <完整昵称>",
                "/password <账号>",
                "/set_password <账号> <新密码>",
                "/confirm <确认码>",
                "/cancel",
            ]
        )

    def handle(self, actor_id: str, text: str, *, is_private: bool = True) -> str:
        actor_id = str(actor_id)
        if (
            self.authorization_checker is not None
            and not self.authorization_checker(actor_id)
        ):
            return "无权限使用此机器人。"
        if (
            self.authorization_checker is None
            and self.allowed_actor_ids is not None
            and actor_id not in self.allowed_actor_ids
        ):
            return "无权限使用此机器人。"

        try:
            parts = shlex.split(str(text or "").strip(), posix=True)
        except ValueError as exc:
            return f"命令格式错误：{exc}"
        if not parts:
            return self.help_text()

        command_name = parts[0].lstrip("/").split("@", 1)[0].casefold()
        command = COMMAND_ALIASES.get(command_name)
        if command is None:
            return "未知命令。发送 /help 查看命令列表。"
        if command in PRIVATE_ONLY_COMMANDS and not is_private:
            return "此命令只能在私聊中使用。"
        if command == "confirm" and not is_private:
            challenge = self.get_pending_challenge(actor_id)
            if challenge is not None and challenge.kind == "password_update":
                return "密码更新确认只能在私聊中进行。"

        try:
            if command == "help":
                return self.help_text()
            if command == "agent":
                return self._handle_agent(parts)
            if command == "servers":
                return self._handle_servers(parts)
            if command == "roles":
                return self._handle_roles(parts)
            if command == "role":
                return self._handle_role(actor_id, parts)
            if command == "password":
                return self._handle_password(parts)
            if command == "set_password":
                return self._handle_set_password(actor_id, parts)
            if command == "confirm":
                return self._handle_confirm(actor_id, parts)
            if command == "cancel":
                return self._handle_cancel(actor_id, parts)
        except (
            BanAdminAPIError,
            BanAdminContractError,
            BanAdminHTTPError,
            BotServiceError,
            TLBBBackendError,
            ValueError,
        ) as exc:
            return f"操作失败：{exc}"
        except Exception:
            logger.exception("Unexpected bot command failure for actor_id=%s", actor_id)
            return "操作失败：发生未预期的后台错误。"
        return "未知命令。发送 /help 查看命令列表。"

    def _handle_agent(self, parts: list[str]) -> str:
        if len(parts) not in (2, 3):
            return "用法：/agent <账号> [game_id]"
        game_id: int | str | None = None
        if len(parts) == 3:
            raw_game_id = parts[2].strip()
            if not raw_game_id or any(char in raw_game_id for char in ("\x00", "\r", "\n")):
                return "game_id 不能为空或包含控制字符。"
            game_id = int(raw_game_id) if raw_game_id.isdecimal() else raw_game_id
            if isinstance(game_id, int) and game_id < 1:
                return "game_id 必须是正整数或非空字符串。"
        rows = self.service.lookup_agents(parts[1], game_id=game_id)
        if not rows:
            return f"未找到账号：{parts[1]}"

        lines = [f"找到 {len(rows)} 条账号记录："]
        for index, row in enumerate(rows, 1):
            agent_name = row.get("nickname") or row.get("username") or "名称未返回"
            parents = row.get("agentParents")
            parent_text = " > ".join(str(item) for item in parents) if isinstance(parents, list) else ""
            lines.extend(
                [
                    f"{index}. 账号：{row.get('account')}",
                    f"   游戏：{row.get('gameName') or '-'} ({row.get('gameId') or '-'})",
                    f"   所属代理：{agent_name} (ID {row.get('agentId')})",
                    (
                        "   是否归属369团队："
                        f"{'是' if is_369_team_agent(row.get('agentId')) else '否'}"
                    ),
                    f"   上级代理链：{parent_text or '-'}",
                ]
            )
        return "\n".join(lines)

    def _handle_servers(self, parts: list[str]) -> str:
        if len(parts) != 1:
            return "用法：/servers"
        servers = self.service.list_servers()
        return "\n".join(
            ["后台区服："]
            + [
                f"{server['serverId']}: {server['serverName']}"
                for server in servers
            ]
        )

    def _handle_roles(self, parts: list[str]) -> str:
        if len(parts) < 3:
            return "用法：/roles <区服ID> <昵称片段>"
        server_id = TLBBBotService._positive_int(parts[1], "区服 ID")
        nickname = " ".join(parts[2:])
        result = self.service.find_similar_roles(
            nickname=nickname,
            server_id=server_id,
        )
        matches = result["matches"]
        assert isinstance(matches, list)
        header = (
            f"区服：{result['serverName']} ({result['serverId']})\n"
            f"查询昵称：{nickname}"
        )
        return header + "\n" + self._format_role_matches(matches)

    def _handle_role(self, actor_id: str, parts: list[str]) -> str:
        if len(parts) < 4:
            return "用法：/role <ban|unban|mute|unmute> <区服ID> <完整昵称>"
        action = ROLE_ACTION_ALIASES.get(parts[1].casefold())
        if action is None:
            return "角色操作必须是 ban、unban、mute 或 unmute。"
        server_id = TLBBBotService._positive_int(parts[2], "区服 ID")
        role_name = " ".join(parts[3:])
        result = self.service.find_similar_roles(
            nickname=role_name,
            server_id=server_id,
        )
        matches = result["matches"]
        assert isinstance(matches, list)
        exact_matches = [
            match
            for match in matches
            if isinstance(match, dict) and match.get("roleName") == role_name
        ]
        if len(exact_matches) != 1:
            reason = (
                "没有完整昵称完全匹配，未生成操作。"
                if not exact_matches
                else "完整昵称对应多个账号，未生成操作。"
            )
            return reason + "\n" + self._format_role_matches(matches)

        match = exact_matches[0]
        account = str(match.get("account") or "")
        server_name = str(result["serverName"])
        summary = (
            f"{ROLE_ACTION_LABELS[action]}角色 {role_name!r}，"
            f"账号 {account!r}，区服 {server_name!r}"
        )
        token = self._stage(
            actor_id,
            "role_action",
            summary,
            {
                "action": action,
                "roleName": role_name,
                "account": account,
                "serverId": server_id,
                "serverName": server_name,
            },
        )
        return (
            f"待确认：{summary}\n"
            f"确认码：{token}\n"
            f"请在 {self.confirmation_ttl_seconds} 秒内发送 /confirm {token}"
        )

    def _handle_password(self, parts: list[str]) -> str:
        if len(parts) != 2:
            return "用法：/password <账号>"
        credential = self.service.get_game_password(parts[1])
        return "\n".join(
            [
                f"账号：{credential.account}",
                f"用户 ID：{credential.user_id}",
                f"明文密码：{credential.password}",
            ]
        )

    def _handle_set_password(self, actor_id: str, parts: list[str]) -> str:
        if len(parts) < 3:
            return "用法：/set_password <账号> <新密码>"
        account = parts[1]
        new_password = " ".join(parts[2:])
        if not new_password or "\x00" in new_password:
            return "新密码不能为空，也不能包含 NUL 字符。"

        user = self.service.find_game_user(account)
        user_id = TLBBBotService._positive_int(user.get("ID"), "游戏用户 ID")
        canonical_account = str(user.get("account") or "")
        summary = f"修改账号 {canonical_account!r}（用户 ID {user_id}）的密码"
        token = self._stage(
            actor_id,
            "password_update",
            summary,
            {
                "account": canonical_account,
                "userId": user_id,
                "newPassword": new_password,
            },
        )
        return (
            f"待确认：{summary}\n"
            "新密码不会在确认信息中回显。\n"
            f"确认码：{token}\n"
            f"请在 {self.confirmation_ttl_seconds} 秒内发送 /confirm {token}"
        )

    def _handle_confirm(self, actor_id: str, parts: list[str]) -> str:
        if len(parts) != 2:
            return "用法：/confirm <确认码>"
        pending = self._take_pending(actor_id, parts[1])

        if pending.kind == "role_action":
            action = str(pending.payload["action"])
            if action not in ROLE_ACTION_LABELS:
                raise BotServiceError("待确认的角色操作无效")
            role_name = str(pending.payload["roleName"])
            account = str(pending.payload["account"])
            server_id = TLBBBotService._positive_int(pending.payload["serverId"], "区服 ID")
            server_name = str(pending.payload["serverName"])

            fresh = self.service.find_similar_roles(
                nickname=role_name,
                server_id=server_id,
            )
            fresh_matches = fresh["matches"]
            assert isinstance(fresh_matches, list)
            still_exists = [
                match
                for match in fresh_matches
                if isinstance(match, dict)
                and match.get("roleName") == role_name
                and match.get("account") == account
            ]
            if len(still_exists) != 1:
                raise BotServiceError("确认前候选已变化，操作已取消")

            result = self.service.perform_role_action(
                action=action,
                role_name=role_name,
                account=account,
                server_id=server_id,
                expected_server_name=server_name,
            )
            message = str(result.get("message") or "后台已接受操作")
            return f"{ROLE_ACTION_LABELS[action]}成功：{role_name}（{account}）\n{message}"

        account = str(pending.payload["account"])
        user_id = TLBBBotService._positive_int(pending.payload["userId"], "游戏用户 ID")
        new_password = str(pending.payload["newPassword"])
        result = self.service.update_game_password(
            account,
            new_password,
            expected_user_id=user_id,
        )
        verified = result.get("verified")
        verification_text = "，读取验证通过" if verified is True else ""
        return f"账号 {account} 密码更新成功{verification_text}。"

    def _handle_cancel(self, actor_id: str, parts: list[str]) -> str:
        if len(parts) != 1:
            return "用法：/cancel"
        with self._pending_lock:
            removed = self._pending.pop(actor_id, None)
        return "已取消待确认操作。" if removed else "当前没有待确认操作。"

    def _stage(
        self,
        actor_id: str,
        kind: PendingKind,
        summary: str,
        payload: dict[str, object],
    ) -> str:
        token = str(self.token_factory()).strip().upper()
        if not token or any(char.isspace() for char in token):
            raise BotServiceError("无法生成有效确认码")
        now = self.clock()
        pending = PendingOperation(
            token=token,
            actor_id=actor_id,
            kind=kind,
            summary=summary,
            expires_at=now + self.confirmation_ttl_seconds,
            payload=payload,
        )
        with self._pending_lock:
            self._pending[actor_id] = pending
        return token

    def _take_pending(self, actor_id: str, token: str) -> PendingOperation:
        normalized_token = str(token or "").strip().upper()
        with self._pending_lock:
            pending = self._pending.get(actor_id)
            if pending is None:
                raise BotServiceError("没有待确认操作")
            if pending.expires_at <= self.clock():
                self._pending.pop(actor_id, None)
                raise BotServiceError("确认码已过期，操作已取消")
            if not secrets.compare_digest(pending.token, normalized_token):
                raise BotServiceError("确认码不正确")
            self._pending.pop(actor_id, None)
        return pending

    def get_pending_challenge(self, actor_id: str) -> ConfirmationChallenge | None:
        actor_id = str(actor_id)
        with self._pending_lock:
            pending = self._pending.get(actor_id)
            if pending is None:
                return None
            if pending.expires_at <= self.clock():
                self._pending.pop(actor_id, None)
                return None
            return ConfirmationChallenge(
                token=pending.token,
                kind=pending.kind,
                summary=pending.summary,
                expires_at=pending.expires_at,
            )

    def discard_pending(self, actor_id: str) -> bool:
        """Discard an actor's pending write without running authorization checks."""
        with self._pending_lock:
            return self._pending.pop(str(actor_id), None) is not None

    @staticmethod
    def _format_role_matches(matches: list[object], *, limit: int = 50) -> str:
        rows = [match for match in matches if isinstance(match, dict)]
        if not rows:
            return "没有找到近似昵称。"
        lines = [f"找到 {len(rows)} 个候选："]
        for index, match in enumerate(rows[:limit], 1):
            lines.append(
                f"{index}. {match.get('roleName') or '-'}（账号 {match.get('account') or '-'}）"
            )
        if len(rows) > limit:
            lines.append(f"其余 {len(rows) - limit} 条未显示，请缩小昵称范围。")
        return "\n".join(lines)
