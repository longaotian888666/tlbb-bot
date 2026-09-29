#!/usr/bin/env python
"""OCR-assisted login client for the ban-permission admin backend."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


DEFAULT_OCR_URL = "http://127.0.0.1:8000/ocr6"
DEFAULT_TOKEN_HEADER = "x-token"
SAFE_AUTH_RETRY_METHODS = {"GET", "HEAD", "OPTIONS"}
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
    "登录已过期",
    "登陆已过期",
    "登录已失效",
    "登陆已失效",
    "授权已过期",
    "授权过期",
    "授权已失效",
    "授权失效",
)
SENSITIVE_RESPONSE_KEYS = {
    "captcha",
    "cookie",
    "image",
    "passwd",
    "password",
    "token",
}
GAME_USER_UPDATE_FIELDS = ("ID", "account", "passwd", "closed", "belong", "agentId")
FUZZY_ROLE_MATCH_PATTERN = re.compile(
    r"角色名\s*[：:]\s*(?P<role_name>.*?)\s*[,，]\s*"
    r"账号\s*[：:]\s*(?P<account>[^;；\r\n]*?)\s*(?=[;；]|$)"
)

LOGIN_URL_KEYS = ("BAN_ADMIN_LOGIN_URL", "封禁账号后台登陆地址", "封禁账号后台登录地址", "后台登陆地址", "后台登录地址")
USERNAME_KEYS = ("BAN_ADMIN_USERNAME", "账号", "用户名", "账户")
PASSWORD_KEYS = ("BAN_ADMIN_PASSWORD", "密码")
OCR_URL_KEYS = ("BAN_ADMIN_OCR_URL", "OCR_URL", "验证码识别接口")


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if str(key).lower() in SENSITIVE_RESPONSE_KEYS else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


class HttpClient(Protocol):
    cookie_jar: http.cookiejar.CookieJar

    def post_json(self, url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
        ...

    def post_form(self, url: str, payload: dict[str, str], timeout: float) -> dict[str, object]:
        ...

    def request_json(
        self,
        url: str,
        method: str,
        payload: dict[str, object] | None,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, object]:
        ...


class BanAdminHTTPError(RuntimeError):
    def __init__(self, url: str, status: int, response: dict[str, object] | None = None) -> None:
        self.url = url
        self.status = status
        self.response = redact_sensitive(response or {})
        message = self.response.get("msg") or self.response.get("message") or self.response.get("error") or ""
        detail = f": {message}" if message else ""
        super().__init__(f"HTTP {status} {url}{detail}")


class BanAdminAPIError(RuntimeError):
    def __init__(self, api_path: str, response: dict[str, object]) -> None:
        self.api_path = api_path
        self.response: dict[str, object] = redact_sensitive(response)
        self.code = self.response.get("code")
        self.message = str(
            self.response.get("msg")
            or self.response.get("message")
            or self.response.get("error")
            or ""
        )
        detail = f": {self.message}" if self.message else ""
        super().__init__(f"Ban admin API {api_path} failed with code {self.code}{detail}")


class BanAdminContractError(RuntimeError):
    """Raised when a successful business response does not match its documented shape."""


class RoleAction(str, Enum):
    MUTE = "1"
    UNMUTE = "2"
    BAN = "3"
    UNBAN = "4"


@dataclass(frozen=True)
class GameUserPassword:
    user_id: int
    account: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class BanAdminConfig:
    login_url: str
    username: str
    password: str
    ocr_url: str = DEFAULT_OCR_URL


@dataclass(frozen=True)
class LoginSession:
    created_at: str
    backend: str
    api_base: str
    login_url: str
    token: str
    token_header: str = DEFAULT_TOKEN_HEADER
    cookies: list[dict[str, object]] = field(default_factory=list)
    user: dict[str, object] = field(default_factory=dict)

    def auth_headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {self.token_header: self.token}

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class UrllibHttpClient:
    def __init__(self) -> None:
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))

    def post_json(self, url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "Accept": "application/json, text/plain, */*",
                "User-Agent": "TLBBBotBanAdminLogin/1.0",
            },
            method="POST",
        )
        with self.opener.open(request, timeout=timeout) as response:
            return parse_json_response(url, response.read())

    def post_form(self, url: str, payload: dict[str, str], timeout: float) -> dict[str, object]:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Accept": "application/json",
                "User-Agent": "TLBBBotBanAdminLogin/1.0",
            },
            method="POST",
        )
        with self.opener.open(request, timeout=timeout) as response:
            return parse_json_response(url, response.read())

    def request_json(
        self,
        url: str,
        method: str,
        payload: dict[str, object] | None,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, object]:
        method = method.upper()
        data = None
        if method == "GET" and payload:
            separator = "&" if urllib.parse.urlparse(url).query else "?"
            url = f"{url}{separator}{urllib.parse.urlencode(payload)}"
        elif payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "Accept": "application/json, text/plain, */*",
                "User-Agent": "TLBBBotBanAdminLogin/1.0",
                **headers,
            },
            method=method,
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                body = response.read()
                return parse_json_response(url, body) if body else {}
        except urllib.error.HTTPError as exc:
            body = exc.read()
            parsed: dict[str, object] = {}
            if body:
                try:
                    parsed = parse_json_response(url, body)
                except Exception:
                    parsed = {}
            raise BanAdminHTTPError(url, exc.code, parsed) from exc


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_json_response(url: str, body: bytes) -> dict[str, object]:
    text = body.decode("utf-8", errors="replace")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"接口返回不是 JSON object: {url}")
    return parsed


def load_env(path: Path) -> dict[str, str]:
    rows: dict[str, str] = {}
    text = path.read_text(encoding="utf-8-sig")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        separator = ""
        if "=" in line:
            separator = "="
        elif "：" in line:
            separator = "："
        elif ":" in line:
            separator = ":"
        if not separator:
            continue
        key, value = line.split(separator, 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            rows[key] = value
    return rows


def first_config_value(rows: dict[str, str], keys: tuple[str, ...], label: str) -> str:
    for key in keys:
        value = rows.get(key)
        if value:
            return value
    raise ValueError(f".env 缺少 {label}")


def load_config(path: Path, ocr_url_override: str | None = None) -> BanAdminConfig:
    rows = load_env(path)
    return BanAdminConfig(
        login_url=first_config_value(rows, LOGIN_URL_KEYS, "后台登录地址"),
        username=first_config_value(rows, USERNAME_KEYS, "账号"),
        password=first_config_value(rows, PASSWORD_KEYS, "密码"),
        ocr_url=ocr_url_override or next((rows[key] for key in OCR_URL_KEYS if rows.get(key)), DEFAULT_OCR_URL),
    )


def build_api_url(login_url: str, api_path: str) -> str:
    parsed = urllib.parse.urlparse(login_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("后台登录地址必须包含 scheme 和 host")
    path = api_path if api_path.startswith("/") else f"/{api_path}"
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def build_session_api_url(session: LoginSession, api_path: str) -> str:
    path = api_path if api_path.startswith("/api/") else f"/api/{api_path.lstrip('/')}"
    return f"{session.backend.rstrip('/')}{path}"


def captcha_image_for_ocr(pic_path: str) -> str:
    value = str(pic_path or "").strip()
    if "," in value and value.lower().startswith("data:"):
        return value.split(",", 1)[1]
    return value


def parse_ocr_text(response: dict[str, object]) -> str:
    value = response.get("code")
    try:
        code = int(value) if isinstance(value, (int, str)) else 0
    except (TypeError, ValueError):
        code = 0
    if code != 200:
        raise ValueError(f"OCR 识别失败: {response.get('message') or response.get('msg') or response.get('code')}")
    data = response.get("data")
    if isinstance(data, list) and data:
        text = str(data[0] or "").strip()
    elif isinstance(data, str):
        text = data.strip()
    else:
        text = ""
    if not text:
        raise ValueError("OCR 返回空验证码")
    return text


def build_login_payload(username: str, password: str, captcha_id: str, captcha: str) -> dict[str, object]:
    return {
        "username": username,
        "password": password,
        "captcha": captcha,
        "captchaId": captcha_id,
    }


def api_code(response: dict[str, object], default: int) -> int:
    value = response.get("code")
    if value is None:
        return default
    try:
        return int(value) if isinstance(value, (int, str)) else default
    except (TypeError, ValueError):
        return default


def extract_token(login_response: dict[str, object]) -> str:
    data = login_response.get("data")
    if isinstance(data, dict):
        token = data.get("token")
        if isinstance(token, str):
            return token
    return ""


def extract_user(login_response: dict[str, object]) -> dict[str, object]:
    data = login_response.get("data")
    user: dict[str, object] = {}
    if isinstance(data, dict):
        candidate = data.get("user")
        if isinstance(candidate, dict):
            user = candidate
    return {
        "id": user.get("ID") or user.get("id") or user.get("uuid") or "",
        "username": user.get("userName") or user.get("username") or "",
        "nick_name": user.get("nickName") or user.get("nick_name") or "",
    }


def cookies_to_rows(cookie_jar: http.cookiejar.CookieJar) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for cookie in cookie_jar:
        rows.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "expires": cookie.expires,
                "secure": cookie.secure,
            }
        )
    return rows


def response_requires_relogin(response: dict[str, object]) -> bool:
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
    message = str(response.get("msg") or response.get("message") or response.get("error") or "").lower()
    normalized_message = "".join(message.split())
    return any(marker in normalized_message for marker in AUTH_EXPIRED_MESSAGE_MARKERS)


def http_error_requires_relogin(error: BanAdminHTTPError) -> bool:
    if error.status == 401:
        return True
    if error.status == 403:
        return response_requires_relogin(error.response)
    return False


def require_api_success(api_path: str, response: dict[str, object]) -> dict[str, object]:
    if api_code(response, default=-1) not in (0, 200):
        raise BanAdminAPIError(api_path, response)
    return response


def require_data_object(api_path: str, response: dict[str, object]) -> dict[str, object]:
    require_api_success(api_path, response)
    data = response.get("data")
    if not isinstance(data, dict):
        raise BanAdminContractError(f"{api_path} response does not contain a data object")
    return data


def require_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{label} must be a positive integer")
    return parsed


def require_clean_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{label} must not contain leading or trailing whitespace")
    if any(char in value for char in ("\x00", "\r", "\n")):
        raise ValueError(f"{label} contains unsupported control characters")
    return value


def sanitize_game_user(row: dict[str, object]) -> dict[str, object]:
    item = dict(row)
    item.pop("passwd", None)
    return item


def parse_fuzzy_role_matches(response: dict[str, object]) -> list[dict[str, str]]:
    message = str(response.get("msg") or response.get("message") or "")
    matches: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in FUZZY_ROLE_MATCH_PATTERN.finditer(message):
        role_name = match.group("role_name").strip()
        account = match.group("account").strip()
        identity = (role_name, account)
        if not role_name or not account or identity in seen:
            continue
        seen.add(identity)
        matches.append({"roleName": role_name, "account": account})
    return matches


class BanAdminClient:
    def __init__(
        self,
        config: BanAdminConfig,
        *,
        session_path: Path | None = None,
        timeout: float = 15.0,
        http_client: HttpClient | None = None,
    ) -> None:
        self.config = config
        self.session_path = session_path
        self.timeout = timeout
        self.http = http_client or UrllibHttpClient()
        self.session: LoginSession | None = None

    def fetch_captcha(self) -> dict[str, object]:
        return self.http.post_json(build_api_url(self.config.login_url, "/api/base/captcha"), {}, self.timeout)

    def recognize_captcha(self, pic_path: str) -> str:
        response = self.http.post_form(
            self.config.ocr_url,
            {
                "image": captcha_image_for_ocr(pic_path),
                "probability": "false",
                "png_fix": "false",
                "charsets": "0123456789",
            },
            self.timeout,
        )
        return parse_ocr_text(response)

    def submit_login(self, captcha_id: str, captcha: str) -> dict[str, object]:
        payload = build_login_payload(self.config.username, self.config.password, captcha_id, captcha)
        return self.http.post_json(build_api_url(self.config.login_url, "/api/base/login"), payload, self.timeout)

    def login(self) -> LoginSession:
        captcha_response = self.fetch_captcha()
        if api_code(captcha_response, default=-1) != 0:
            raise ValueError(f"验证码获取失败: {captcha_response.get('msg') or captcha_response.get('code')}")
        data = captcha_response.get("data")
        if not isinstance(data, dict):
            raise ValueError("验证码接口返回缺少 data object")
        captcha_id = str(data.get("captchaId") or "")
        pic_path = str(data.get("picPath") or "")
        if not captcha_id or not pic_path:
            raise ValueError("验证码接口返回缺少 captchaId 或 picPath")

        captcha = self.recognize_captcha(pic_path)
        login_response = self.submit_login(captcha_id, captcha)
        if api_code(login_response, default=-1) != 0:
            raise ValueError(f"后台登录失败: {login_response.get('msg') or login_response.get('code')}")
        token = extract_token(login_response)
        if not token:
            raise ValueError("后台登录成功但响应缺少 data.token")

        session = LoginSession(
            created_at=now_iso(),
            backend=build_api_url(self.config.login_url, "/"),
            api_base=build_api_url(self.config.login_url, "/api"),
            login_url=self.config.login_url,
            token=token,
            token_header=DEFAULT_TOKEN_HEADER,
            cookies=cookies_to_rows(self.http.cookie_jar),
            user=extract_user(login_response),
        )
        self.session = session
        if self.session_path:
            self.write_session(session)
        return session

    def request_json(
        self,
        method: str,
        api_path: str,
        payload: dict[str, object] | None = None,
        *,
        retry_on_auth_expired: bool | None = None,
    ) -> dict[str, object]:
        method = method.upper()
        should_retry = (
            method in SAFE_AUTH_RETRY_METHODS
            if retry_on_auth_expired is None
            else retry_on_auth_expired
        )
        session = self.session or self.login()
        try:
            response = self._request_json_with_session(session, method, api_path, payload)
        except BanAdminHTTPError as exc:
            if should_retry and http_error_requires_relogin(exc):
                session = self.login()
                return self._request_json_with_session(session, method, api_path, payload)
            raise

        if should_retry and response_requires_relogin(response):
            session = self.login()
            return self._request_json_with_session(session, method, api_path, payload)
        return response

    def _request_json_with_session(
        self,
        session: LoginSession,
        method: str,
        api_path: str,
        payload: dict[str, object] | None,
    ) -> dict[str, object]:
        return self.http.request_json(
            build_session_api_url(session, api_path),
            method,
            payload,
            session.auth_headers(),
            self.timeout,
        )

    def get_current_user(self) -> dict[str, object]:
        api_path = "/user/getUserInfo"
        data = require_data_object(api_path, self.request_json("GET", api_path))
        user = data.get("userInfo") or data.get("user")
        if not isinstance(user, dict):
            raise BanAdminContractError(f"{api_path} response does not contain userInfo or user")
        return dict(user)

    def get_servers(self) -> list[dict[str, object]]:
        api_path = "/gameOrder/getServers"
        data = require_data_object(api_path, self.request_json("GET", api_path))
        rows = data.get("list")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise BanAdminContractError(f"{api_path} response does not contain a valid data.list")
        return [dict(row) for row in rows]

    def _build_role_target_payload(
        self,
        *,
        channel: str,
        role_name: str,
        server_id: int,
        expected_server_name: str,
        expected_agent_id: int | None,
    ) -> dict[str, object]:
        role_name = require_clean_text(role_name, "role_name")
        server_id = require_positive_int(server_id, "server_id")
        expected_server_name = require_clean_text(expected_server_name, "expected_server_name")

        current_user = self.get_current_user()
        agent_id = require_positive_int(current_user.get("ID") or current_user.get("id"), "current user ID")
        agent_name = require_clean_text(
            current_user.get("userName") or current_user.get("username"),
            "current username",
        )
        if expected_agent_id is not None and agent_id != require_positive_int(
            expected_agent_id,
            "expected_agent_id",
        ):
            raise ValueError("Current backend user does not match expected_agent_id")

        server = next(
            (
                row
                for row in self.get_servers()
                if not isinstance(row.get("ID"), bool)
                and str(row.get("ID") or "").isdigit()
                and int(str(row["ID"])) == server_id
            ),
            None,
        )
        if server is None:
            raise ValueError(f"Backend server ID {server_id} was not found")
        actual_server_name = str(server.get("configName") or "")
        if actual_server_name != expected_server_name:
            raise ValueError(
                f"Backend server {server_id} name mismatch: expected {expected_server_name!r}, "
                f"got {actual_server_name!r}"
            )

        return {
            "agentid": agent_id,
            "agent": agent_name,
            "serverid": server_id,
            "servername": actual_server_name,
            "channel": channel,
            "name": role_name,
            "noticeId": "",
            "beginTime": "",
            "endTime": "",
            "rollingContent": "",
            "frequency": "",
        }

    def find_similar_roles(
        self,
        *,
        role_name: str,
        server_id: int,
        expected_server_name: str,
        expected_agent_id: int | None = None,
    ) -> list[dict[str, str]]:
        payload = self._build_role_target_payload(
            channel=RoleAction.MUTE.value,
            role_name=role_name,
            server_id=server_id,
            expected_server_name=expected_server_name,
            expected_agent_id=expected_agent_id,
        )
        api_path = "/gameOrder/getFuzNames"
        response = self.request_json(
            "POST",
            api_path,
            payload,
            retry_on_auth_expired=True,
        )
        require_api_success(api_path, response)
        return parse_fuzzy_role_matches(response)

    def apply_role_action(
        self,
        action: RoleAction,
        *,
        role_name: str,
        server_id: int,
        expected_server_name: str,
        expected_agent_id: int | None = None,
    ) -> dict[str, object]:
        try:
            action = RoleAction(action)
        except ValueError as exc:
            raise ValueError(f"Unsupported role action: {action}") from exc
        api_path = "/gameConfig/banUser"
        payload = self._build_role_target_payload(
            channel=action.value,
            role_name=role_name,
            server_id=server_id,
            expected_server_name=expected_server_name,
            expected_agent_id=expected_agent_id,
        )
        response = self.request_json(
            "POST",
            api_path,
            payload,
            retry_on_auth_expired=False,
        )
        return require_api_success(api_path, response)

    def mute_role(
        self,
        *,
        role_name: str,
        server_id: int,
        expected_server_name: str,
        expected_agent_id: int | None = None,
    ) -> dict[str, object]:
        return self.apply_role_action(
            RoleAction.MUTE,
            role_name=role_name,
            server_id=server_id,
            expected_server_name=expected_server_name,
            expected_agent_id=expected_agent_id,
        )

    def unmute_role(
        self,
        *,
        role_name: str,
        server_id: int,
        expected_server_name: str,
        expected_agent_id: int | None = None,
    ) -> dict[str, object]:
        return self.apply_role_action(
            RoleAction.UNMUTE,
            role_name=role_name,
            server_id=server_id,
            expected_server_name=expected_server_name,
            expected_agent_id=expected_agent_id,
        )

    def ban_role(
        self,
        *,
        role_name: str,
        server_id: int,
        expected_server_name: str,
        expected_agent_id: int | None = None,
    ) -> dict[str, object]:
        return self.apply_role_action(
            RoleAction.BAN,
            role_name=role_name,
            server_id=server_id,
            expected_server_name=expected_server_name,
            expected_agent_id=expected_agent_id,
        )

    def unban_role(
        self,
        *,
        role_name: str,
        server_id: int,
        expected_server_name: str,
        expected_agent_id: int | None = None,
    ) -> dict[str, object]:
        return self.apply_role_action(
            RoleAction.UNBAN,
            role_name=role_name,
            server_id=server_id,
            expected_server_name=expected_server_name,
            expected_agent_id=expected_agent_id,
        )

    def list_game_users(
        self,
        *,
        account: str | None = None,
        page: int = 1,
        page_size: int = 10,
    ) -> list[dict[str, object]]:
        page = require_positive_int(page, "page")
        page_size = require_positive_int(page_size, "page_size")
        rows, _ = self._list_game_users_page(
            account=account,
            page=page,
            page_size=page_size,
        )
        return rows

    def _list_game_users_page(
        self,
        *,
        account: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, object]], int | None]:
        payload: dict[str, object] = {"page": page, "pageSize": page_size}
        if account is not None:
            payload["account"] = require_clean_text(account, "account")

        api_path = "/gameUsers/getGameUsersList"
        data = require_data_object(api_path, self.request_json("GET", api_path, payload))
        rows = data.get("list")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise BanAdminContractError(f"{api_path} response does not contain a valid data.list")
        total_value = data.get("count")
        total: int | None = None
        if total_value is not None:
            if isinstance(total_value, bool):
                raise BanAdminContractError(f"{api_path} response contains an invalid data.count")
            try:
                total = int(total_value)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise BanAdminContractError(
                    f"{api_path} response contains an invalid data.count"
                ) from exc
            if total < 0:
                raise BanAdminContractError(f"{api_path} response contains an invalid data.count")
        return [sanitize_game_user(row) for row in rows], total

    def find_game_users_by_account(
        self,
        account: str,
        *,
        page_size: int = 100,
        max_pages: int = 100,
    ) -> list[dict[str, object]]:
        account = require_clean_text(account, "account")
        page_size = require_positive_int(page_size, "page_size")
        max_pages = require_positive_int(max_pages, "max_pages")
        normalized_account = account.casefold()
        matches: list[dict[str, object]] = []
        seen_matches: set[tuple[object, str]] = set()
        seen_pages: set[tuple[tuple[object, str], ...]] = set()
        seen_rows = 0

        for page in range(1, max_pages + 1):
            rows, total = self._list_game_users_page(
                account=account,
                page=page,
                page_size=page_size,
            )
            if not rows:
                return matches

            page_identity = tuple(
                (row.get("ID"), str(row.get("account") or "").casefold())
                for row in rows
            )
            if page_identity in seen_pages:
                raise BanAdminContractError(
                    "/gameUsers/getGameUsersList repeated a page while searching by account"
                )
            seen_pages.add(page_identity)
            seen_rows += len(rows)

            for row in rows:
                row_account = str(row.get("account") or "")
                if row_account.casefold() != normalized_account:
                    continue
                identity = (row.get("ID"), row_account.casefold())
                if identity in seen_matches:
                    continue
                seen_matches.add(identity)
                matches.append(row)

            if total is not None and seen_rows >= total:
                return matches

        raise BanAdminContractError(
            f"/gameUsers/getGameUsersList exceeded {max_pages} pages while searching by account"
        )

    def _find_unique_game_user_by_account(self, account: str) -> dict[str, object]:
        matches = self.find_game_users_by_account(account)
        if not matches:
            raise ValueError(f"Game user account {account!r} was not found")
        if len(matches) > 1:
            ids = ", ".join(str(row.get("ID") or "?") for row in matches)
            raise ValueError(f"Game user account {account!r} is ambiguous; matching IDs: {ids}")
        return matches[0]

    def find_game_user_by_account(self, account: str) -> dict[str, object]:
        return dict(self._find_unique_game_user_by_account(account))

    def get_game_user_password_by_account(self, account: str) -> GameUserPassword:
        row = self._find_unique_game_user_by_account(account)
        user_id = require_positive_int(row.get("ID"), "game user ID")
        actual_account = require_clean_text(row.get("account"), "game user account")
        detail = self._find_game_user_raw(user_id)
        detail_account = require_clean_text(detail.get("account"), "game user account")
        if detail_account != actual_account:
            raise BanAdminContractError(
                "/gameUsers/findGameUsers returned an account that does not match the list result"
            )
        password = detail.get("passwd")
        if not isinstance(password, str):
            raise BanAdminContractError("/gameUsers/findGameUsers response does not contain passwd")
        return GameUserPassword(
            user_id=user_id,
            account=actual_account,
            password=password,
        )

    def update_game_user_password_by_account(
        self,
        account: str,
        new_password: str,
        *,
        verify: bool = False,
    ) -> dict[str, object]:
        row = self._find_unique_game_user_by_account(account)
        user_id = require_positive_int(row.get("ID"), "game user ID")
        actual_account = require_clean_text(row.get("account"), "game user account")
        return self.update_game_user_password(
            user_id,
            new_password,
            expected_account=actual_account,
            verify=verify,
        )

    def find_game_user(self, user_id: int) -> dict[str, object]:
        row = self._find_game_user_raw(user_id)
        return sanitize_game_user(row)

    def get_game_user_password(self, user_id: int) -> str:
        row = self._find_game_user_raw(user_id)
        password = row.get("passwd")
        if not isinstance(password, str):
            raise BanAdminContractError("/gameUsers/findGameUsers response does not contain passwd")
        return password

    def _find_game_user_raw(self, user_id: int) -> dict[str, object]:
        user_id = require_positive_int(user_id, "user_id")
        api_path = "/gameUsers/findGameUsers"
        data = require_data_object(
            api_path,
            self.request_json("GET", api_path, {"ID": user_id}),
        )
        row = data.get("regameUsers")
        if not isinstance(row, dict):
            raise BanAdminContractError(f"{api_path} response does not contain data.regameUsers")
        result = dict(row)
        returned_id = require_positive_int(result.get("ID"), "returned game user ID")
        if returned_id != user_id:
            raise BanAdminContractError(
                f"{api_path} returned game user ID {returned_id}, expected {user_id}"
            )
        return result

    def update_game_user_password(
        self,
        user_id: int,
        new_password: str,
        *,
        expected_account: str,
        verify: bool = False,
    ) -> dict[str, object]:
        user_id = require_positive_int(user_id, "user_id")
        if not isinstance(new_password, str) or not new_password or "\x00" in new_password:
            raise ValueError("new_password must be a non-empty string without NUL characters")
        expected_account = require_clean_text(expected_account, "expected_account")

        row = self._find_game_user_raw(user_id)
        account = str(row.get("account") or "")
        if account != expected_account:
            raise ValueError(
                f"Game user {user_id} account mismatch: expected {expected_account!r}, got {account!r}"
            )
        missing_fields = [key for key in GAME_USER_UPDATE_FIELDS if key not in row]
        if missing_fields:
            raise BanAdminContractError(
                "/gameUsers/findGameUsers response is missing update fields: "
                + ", ".join(missing_fields)
            )
        payload = {key: row[key] for key in GAME_USER_UPDATE_FIELDS}
        payload["passwd"] = new_password

        api_path = "/gameUsers/updateGameUsers"
        response = self.request_json(
            "PUT",
            api_path,
            payload,
            retry_on_auth_expired=False,
        )
        require_api_success(api_path, response)

        verified: bool | None = None
        if verify:
            verified = self.get_game_user_password(user_id) == new_password
            if not verified:
                raise BanAdminContractError(
                    f"{api_path} accepted the update but password verification did not match"
                )
        return {
            "userId": user_id,
            "account": account,
            "updated": True,
            "verified": verified,
            "message": str(response.get("msg") or response.get("message") or ""),
        }

    def reset_admin_password_to_default(
        self,
        admin_user_id: int,
        *,
        confirmation: str,
        allow_self_reset: bool = False,
    ) -> dict[str, object]:
        admin_user_id = require_positive_int(admin_user_id, "admin_user_id")
        expected_confirmation = f"RESET ADMIN {admin_user_id} TO 123456"
        if confirmation != expected_confirmation:
            raise ValueError(f"confirmation must equal {expected_confirmation!r}")

        current_user = self.get_current_user()
        current_user_id = require_positive_int(
            current_user.get("ID") or current_user.get("id"),
            "current user ID",
        )
        if current_user_id == admin_user_id and not allow_self_reset:
            raise ValueError("Refusing to reset the current backend user's password")

        api_path = "/user/resetPassword"
        response = self.request_json(
            "POST",
            api_path,
            {"ID": admin_user_id},
            retry_on_auth_expired=False,
        )
        require_api_success(api_path, response)
        return {
            "adminUserId": admin_user_id,
            "temporaryDefaultSet": True,
            "requiresImmediateRotation": True,
            "message": str(response.get("msg") or response.get("message") or ""),
        }

    def write_session(self, session: LoginSession) -> None:
        if not self.session_path:
            return
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_path.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=".env", help="Path to env file containing backend login URL and credentials.")
    parser.add_argument("--ocr-url", help=f"OCR endpoint. Defaults to {DEFAULT_OCR_URL} or BAN_ADMIN_OCR_URL.")
    parser.add_argument("--session-out", default="ban_admin_session.json", help="Where to write the sensitive session artifact.")
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser


def session_summary(session: LoginSession, session_path: Path) -> dict[str, object]:
    return {
        "ok": True,
        "backend": session.backend,
        "api_base": session.api_base,
        "token_present": bool(session.token),
        "token_len": len(session.token),
        "cookie_count": len(session.cookies),
        "user": session.user,
        "session_path": str(session_path),
    }


def main() -> int:
    args = build_parser().parse_args()
    session_path = Path(args.session_out)
    try:
        config = load_config(Path(args.env), args.ocr_url)
        client = BanAdminClient(config, session_path=session_path, timeout=args.timeout)
        session = client.login()
    except Exception as exc:  # noqa: BLE001 - CLI should return a concise operational error.
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(session_summary(session, session_path), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
