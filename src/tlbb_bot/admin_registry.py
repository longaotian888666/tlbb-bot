"""Persistent Telegram administrator registry."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Iterable


STORE_VERSION = 1


class AdminRegistryError(RuntimeError):
    """Raised when the administrator store cannot be loaded or saved safely."""


def _normalize_user_id(value: int | str) -> int:
    if isinstance(value, bool):
        raise ValueError("Telegram 用户 ID 必须是正整数")
    try:
        user_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Telegram 用户 ID 必须是正整数") from exc
    if user_id < 1 or str(value).strip() != str(user_id):
        raise ValueError("Telegram 用户 ID 必须是正整数")
    return user_id


class TelegramAdminRegistry:
    """Combine immutable configured admins with persistent runtime admins."""

    def __init__(
        self,
        *,
        super_admin_id: int | None,
        static_admin_ids: Iterable[int] = (),
        store_path: str | Path | None = None,
    ) -> None:
        self.super_admin_id = (
            _normalize_user_id(super_admin_id)
            if super_admin_id is not None
            else None
        )
        self.static_admin_ids = frozenset(
            _normalize_user_id(user_id) for user_id in static_admin_ids
        )
        self.store_path = Path(store_path) if store_path is not None else None
        self._lock = threading.RLock()
        self._dynamic_admin_ids = self._load()

    def is_super_admin(self, user_id: int | str) -> bool:
        try:
            normalized = _normalize_user_id(user_id)
        except ValueError:
            return False
        return self.super_admin_id is not None and normalized == self.super_admin_id

    def is_authorized(self, user_id: int | str) -> bool:
        try:
            normalized = _normalize_user_id(user_id)
        except ValueError:
            return False
        with self._lock:
            return (
                self.is_super_admin(normalized)
                or normalized in self.static_admin_ids
                or normalized in self._dynamic_admin_ids
            )

    def role(self, user_id: int | str) -> str:
        if self.is_super_admin(user_id):
            return "总管理员"
        if self.is_authorized(user_id):
            return "管理员"
        return "未授权"

    def list_admin_ids(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(
                sorted(self.static_admin_ids | self._dynamic_admin_ids)
            )

    def add(self, user_id: int | str) -> bool:
        normalized = _normalize_user_id(user_id)
        with self._lock:
            if self.is_super_admin(normalized):
                raise ValueError("该用户已经是总管理员")
            if (
                normalized in self.static_admin_ids
                or normalized in self._dynamic_admin_ids
            ):
                return False
            updated = set(self._dynamic_admin_ids)
            updated.add(normalized)
            self._save(updated)
            self._dynamic_admin_ids = updated
            return True

    def remove(self, user_id: int | str) -> bool:
        normalized = _normalize_user_id(user_id)
        with self._lock:
            if self.is_super_admin(normalized):
                raise ValueError("不能删除总管理员")
            if normalized in self.static_admin_ids:
                raise ValueError("该管理员来自 .env，请修改配置后重启机器人")
            if normalized not in self._dynamic_admin_ids:
                return False
            updated = set(self._dynamic_admin_ids)
            updated.remove(normalized)
            self._save(updated)
            self._dynamic_admin_ids = updated
            return True

    def _load(self) -> set[int]:
        if self.store_path is None or not self.store_path.exists():
            return set()
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AdminRegistryError(
                f"管理员文件无法读取：{self.store_path}"
            ) from exc
        if not isinstance(raw, dict) or raw.get("version") != STORE_VERSION:
            raise AdminRegistryError("管理员文件版本或结构无效")
        values = raw.get("admin_user_ids")
        if not isinstance(values, list):
            raise AdminRegistryError("管理员文件缺少 admin_user_ids 数组")
        try:
            admin_ids = {_normalize_user_id(value) for value in values}
        except ValueError as exc:
            raise AdminRegistryError("管理员文件包含无效用户 ID") from exc
        if self.super_admin_id in admin_ids:
            raise AdminRegistryError("管理员文件不能包含总管理员 ID")
        if admin_ids & self.static_admin_ids:
            raise AdminRegistryError("管理员文件不能重复保存 .env 静态管理员")
        return admin_ids

    def _save(self, admin_ids: set[int]) -> None:
        if self.store_path is None:
            return
        parent = self.store_path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": STORE_VERSION,
                "admin_user_ids": sorted(admin_ids),
            }
            handle, temporary_name = tempfile.mkstemp(
                prefix=f".{self.store_path.name}.",
                suffix=".tmp",
                dir=parent,
                text=True,
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(payload, stream, ensure_ascii=True, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, self.store_path)
            except BaseException:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                raise
        except OSError as exc:
            raise AdminRegistryError(
                f"管理员文件无法写入：{self.store_path}"
            ) from exc
