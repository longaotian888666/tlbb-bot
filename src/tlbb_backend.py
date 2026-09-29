from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import requests


logger = logging.getLogger(__name__)

AUTH_EXPIRED_MESSAGE_MARKERS = (
    "tokenexpired",
    "tokenhasexpired",
    "invalidtoken",
    "tokeninvalid",
    "token已过期",
    "token过期",
    "token已失效",
    "token失效",
    "unauthorized",
    "未登录",
    "未登陆",
    "请重新登录",
    "请重新登陆",
    "登录失效",
    "登陆失效",
    "登录已失效",
    "登陆已失效",
    "登录过期",
    "登陆过期",
    "登录已过期",
    "登陆已过期",
    "授权已过期",
    "授权过期",
    "授权已失效",
    "授权失效",
)


def response_requires_relogin(response: dict[str, Any]) -> bool:
    """Return whether a failed agent-backend response means auth expired."""
    value = response.get("code")
    code: int | None = None
    if isinstance(value, (int, str)):
        try:
            code = int(value)
        except (TypeError, ValueError):
            code = None
    if code == 401:
        return True
    if code in (0, 200):
        return False
    message = str(
        response.get("msg")
        or response.get("message")
        or response.get("error")
        or ""
    ).casefold()
    normalized_message = "".join(message.split())
    return any(marker in normalized_message for marker in AUTH_EXPIRED_MESSAGE_MARKERS)


class TLBBBackendError(Exception):
    """Raised when the TLBB backend request or response is invalid."""


class TLBBBackendClient:
    """
    Standalone client for the TLBB agent backend.

    Authentication uses POST /admin/agent/login and sends the returned token
    in the `token` header for subsequent requests.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        session: Optional[requests.Session] = None,
        timeout: int = 15,
        max_retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.token: Optional[str] = None
        self.user_info: dict[str, Any] = {}
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://tlcw.sxjzsz.com/admin_game/",
            }
        )

    @classmethod
    def from_env(cls, prefix: str = "API_", **kwargs) -> "TLBBBackendClient":
        base_url = os.getenv(f"{prefix}BASE_URL")
        username = os.getenv(f"{prefix}USERNAME")
        password = os.getenv(f"{prefix}PASSWORD")
        missing = [
            name
            for name, value in (
                (f"{prefix}BASE_URL", base_url),
                (f"{prefix}USERNAME", username),
                (f"{prefix}PASSWORD", password),
            )
            if not value
        ]
        if missing:
            raise TLBBBackendError("Missing environment variables: {}".format(", ".join(missing)))
        return cls(str(base_url), str(username), str(password), **kwargs)

    @classmethod
    def from_env_file(
        cls,
        path: str | Path = ".env",
        prefix: str = "API_",
        **kwargs,
    ) -> "TLBBBackendClient":
        """Build a client from a simple KEY=VALUE env file without extra dependencies."""
        env_path = Path(path)
        try:
            text = env_path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise TLBBBackendError(f"Failed to read env file {env_path}: {exc}") from exc

        values: dict[str, str] = {}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key:
                values[key] = value

        keys = {
            "base_url": f"{prefix}BASE_URL",
            "username": f"{prefix}USERNAME",
            "password": f"{prefix}PASSWORD",
        }
        missing = [env_key for env_key in keys.values() if not values.get(env_key)]
        if missing:
            raise TLBBBackendError("Missing env-file variables: {}".format(", ".join(missing)))
        return cls(
            values[keys["base_url"]],
            values[keys["username"]],
            values[keys["password"]],
            **kwargs,
        )

    def login(self) -> str:
        payload = {
            "username": self.username,
            "password": self.password,
            "code": "",
            "uuid": "",
        }
        data = self.request_json("POST", "/admin/agent/login", payload)
        token = ((data.get("data") or {}).get("token") or "").strip()
        if not token:
            raise TLBBBackendError("Login response does not contain data.token")
        self.token = token
        self.user_info = (data.get("data") or {}).get("userInfo") or {}
        self.session.headers["token"] = token
        logger.info("TLBB backend login succeeded, agentId=%s", self.user_info.get("agentId"))
        return token

    def ensure_login(self) -> str:
        if not self.token:
            return self.login()
        return self.token

    def request_json(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        *,
        retry_on_auth_expired: bool | None = None,
    ) -> dict:
        method = method.upper()
        should_retry_auth = (
            method == "GET"
            if retry_on_auth_expired is None
            else retry_on_auth_expired
        )
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                if method == "GET":
                    response = self.session.get(self._url(path), timeout=self.timeout)
                elif method == "POST":
                    response = self.session.post(self._url(path), json=payload or {}, timeout=self.timeout)
                else:
                    raise TLBBBackendError(f"Unsupported HTTP method: {method}")
                response.raise_for_status()
                data = response.json()
            except TLBBBackendError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    time.sleep(2**attempt)
                    continue
                raise TLBBBackendError(f"{method} {path} request failed: {exc}") from exc

            if data.get("code") not in (0, 200):
                if should_retry_auth and response_requires_relogin(data):
                    self._clear_auth()
                    self.login()
                    return self.request_json(
                        method,
                        path,
                        payload,
                        retry_on_auth_expired=False,
                    )
                raise TLBBBackendError(f"{method} {path} failed: {data.get('msg') or data.get('code')}")
            return data
        raise TLBBBackendError(f"{method} {path} request failed: {last_error}")

    def _clear_auth(self) -> None:
        self.token = None
        self.user_info = {}
        self.session.headers.pop("token", None)

    def get_index_data(self) -> dict:
        self.ensure_login()
        return self.request_json("GET", "/admin/data/indexData")["data"]

    def get_sub_agents(self, *, page_size: int = 500) -> list[dict]:
        self.ensure_login()
        rows: list[dict] = []
        page = 1
        while True:
            data = self.request_json(
                "POST",
                "/admin/agent/getSubAgentList",
                {"currentPage": page, "pageSize": page_size},
                retry_on_auth_expired=True,
            )["data"]
            batch = data.get("list", [])
            rows.extend(batch)
            total = int(data.get("count", 0) or 0)
            if not batch or len(rows) >= total:
                break
            page += 1
        return rows

    def get_player_agents(
        self,
        account: str,
        *,
        game_id: Optional[int | str] = None,
        page_size: int = 100,
        include_agent_metadata: bool = True,
    ) -> list[dict]:
        """
        Return exact player-account matches with their direct agent and agent chain.

        The backend performs a fuzzy account search, so results are filtered again
        locally using a case-insensitive exact match.
        """
        account = str(account or "").strip()
        if not account:
            raise TLBBBackendError("Player account must not be empty")
        try:
            page_size = int(page_size)
        except (TypeError, ValueError) as exc:
            raise TLBBBackendError("Player page_size must be a positive integer") from exc
        if page_size < 1:
            raise TLBBBackendError("Player page_size must be a positive integer")

        self.ensure_login()
        normalized_account = account.casefold()
        matches: list[dict] = []
        page = 1
        seen = 0

        while True:
            payload = {
                "currentPage": page,
                "pageSize": page_size,
                "search": {
                    "account": account,
                    "gameId": game_id if game_id is not None else "",
                },
            }
            response = self.request_json(
                "POST",
                "/admin/user/list",
                payload,
                retry_on_auth_expired=True,
            )
            data = response.get("data")
            if not isinstance(data, dict):
                raise TLBBBackendError("Player list response does not contain a data object")
            batch = data.get("list")
            if not isinstance(batch, list):
                raise TLBBBackendError("Player list response does not contain data.list")

            for row in batch:
                if not isinstance(row, dict):
                    continue
                row_account = str(row.get("account") or "").strip()
                if row_account.casefold() != normalized_account:
                    continue
                game_id_value = row.get("gid")
                if game_id_value is None:
                    game_id_value = row.get("gameId")
                if game_id is not None and str(game_id_value) != str(game_id):
                    continue
                agent_id = row.get("agentId")
                if agent_id is None:
                    raise TLBBBackendError(f"Player {account!r} does not contain agentId")
                try:
                    agent_id = int(agent_id)
                except (TypeError, ValueError) as exc:
                    raise TLBBBackendError(f"Player {account!r} contains an invalid agentId") from exc

                parents = row.get("agentParents")
                matches.append(
                    {
                        "account": row_account,
                        "agentId": agent_id,
                        "agentParents": list(parents) if isinstance(parents, list) else [],
                        "gameId": game_id_value,
                        "gameName": row.get("gameName"),
                        "isStop": row.get("isStop"),
                        "createTime": row.get("createTime"),
                    }
                )

            seen += len(batch)
            total_value = data.get("count")
            total: Optional[int] = None
            if total_value is not None:
                try:
                    total = int(total_value)
                except (TypeError, ValueError) as exc:
                    raise TLBBBackendError("Player list response contains an invalid data.count") from exc
            if (
                not batch
                or (total is not None and seen >= total)
                or (total is None and len(batch) < page_size)
            ):
                break
            page += 1

        if include_agent_metadata and matches:
            matches = self._attach_agent_metadata(matches)
        return matches

    def get_player_agent(
        self,
        account: str,
        *,
        game_id: Optional[int | str] = None,
        include_agent_metadata: bool = True,
    ) -> Optional[dict]:
        """Return one exact player-account match, or None when it does not exist."""
        matches = self.get_player_agents(
            account,
            game_id=game_id,
            include_agent_metadata=include_agent_metadata,
        )
        if not matches:
            return None
        if len(matches) > 1:
            raise TLBBBackendError(
                f"Multiple player records matched {account!r}; pass game_id to disambiguate"
            )
        return matches[0]

    def get_agent_rank(
        self,
        start_date: str,
        end_date: str,
        *,
        data_type: Optional[str] = None,
        scope: str = "all",
        order: str = "total_recharge",
        sort: int = -1,
        page_size: int = 50,
        include_agent_metadata: bool = True,
    ) -> list[dict]:
        self.ensure_login()
        rows: list[dict] = []
        page = 1
        while True:
            payload: dict[str, Any] = {
                "currentPage": page,
                "pageSize": page_size,
                "sort": sort,
                "order": order,
                "startDate": start_date,
                "endDate": end_date,
                "scope": scope,
            }
            if data_type:
                payload["dataType"] = data_type

            data = self.request_json(
                "POST",
                "/admin/data/agentDataRank",
                payload,
                retry_on_auth_expired=True,
            )["data"]
            batch = data.get("list", [])
            rows.extend(batch)
            total = int(data.get("count", 0) or 0)
            if not batch or len(rows) >= total:
                break
            page += 1

        if include_agent_metadata:
            rows = self._attach_agent_metadata(rows)
        return rows

    def get_agent_performance(
        self,
        start_date: str,
        end_date: str,
        *,
        agent_ids: Optional[Iterable[int]] = None,
        data_type: Optional[str] = None,
    ) -> dict[int, dict]:
        rows = self.get_agent_rank(start_date, end_date, data_type=data_type)
        wanted = {int(agent_id) for agent_id in agent_ids} if agent_ids is not None else None
        result: dict[int, dict] = {}
        for row in rows:
            agent_id = row.get("agentId")
            if agent_id is None:
                continue
            agent_id = int(agent_id)
            if wanted is not None and agent_id not in wanted:
                continue
            result[agent_id] = row
        return result

    def fetch_date(self, target_date: date) -> tuple[dict, list[dict]]:
        start_at = target_date.strftime("%Y-%m-%d 00:00:00")
        end_at = target_date.strftime("%Y-%m-%d 23:59:59")
        return self.get_index_data(), self.get_agent_rank(start_at, end_at)

    def fetch_today_realtime(self, *, timezone_name: str = "Asia/Shanghai") -> list[dict]:
        try:
            from zoneinfo import ZoneInfo

            today = datetime.now(ZoneInfo(timezone_name)).date()
        except Exception:
            today = date.today()
        start_at = today.strftime("%Y-%m-%d 00:00:00")
        end_at = today.strftime("%Y-%m-%d 23:59:59")
        return self.get_agent_rank(start_at, end_at, data_type="today")

    def _attach_agent_metadata(self, rows: list[dict]) -> list[dict]:
        try:
            agents = self.get_sub_agents()
        except TLBBBackendError as exc:
            logger.warning("Failed to fetch sub-agent metadata, using raw rank rows: %s", exc)
            return rows

        meta_by_agent_id = {}
        current_agent_id = self.user_info.get("agentId")
        if current_agent_id is not None:
            meta_by_agent_id[int(current_agent_id)] = {
                "currentAgentId": current_agent_id,
                "legacyAgentId": (self.user_info.get("legacy") or {}).get("oldAgentId"),
                "nickname": self.user_info.get("nickname"),
                "username": self.user_info.get("username"),
            }
        for agent in agents:
            agent_id = agent.get("agentId")
            if agent_id is None:
                continue
            meta_by_agent_id[int(agent_id)] = {
                "currentAgentId": agent_id,
                "legacyAgentId": (agent.get("legacy") or {}).get("oldAgentId"),
                "nickname": agent.get("nickname"),
                "username": agent.get("username"),
            }

        normalized = []
        for row in rows:
            item = dict(row)
            item_agent_id = item.get("agentId")
            meta = meta_by_agent_id.get(int(item_agent_id)) if item_agent_id is not None else None
            if meta:
                item.update(meta)
            normalized.append(item)
        return normalized

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"
