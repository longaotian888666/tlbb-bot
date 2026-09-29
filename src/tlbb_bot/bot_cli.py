"""Run the platform-neutral TLBB bot as a local interactive console."""

from __future__ import annotations

import argparse
import getpass
import shlex
import sys
from pathlib import Path

from tlbb_backend import TLBBBackendClient

from .ban_admin_login import BanAdminClient, load_config
from .bot_service import BotCommandProcessor, TLBBBotService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=".env", help="Path to the shared backend configuration file.")
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--actor-id", default="local-console")
    return parser


def build_command_processor(env_path: Path, timeout: float) -> BotCommandProcessor:
    agent_client = TLBBBackendClient.from_env_file(
        env_path,
        timeout=max(1, int(timeout)),
    )
    admin_client = BanAdminClient(
        load_config(env_path),
        timeout=timeout,
    )
    return BotCommandProcessor(TLBBBotService(agent_client, admin_client))


def prompt_for_hidden_password(command: str) -> str:
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        return command
    if not parts:
        return command
    name = parts[0].lstrip("/").split("@", 1)[0].casefold()
    if name not in {"set_password", "改密"} or len(parts) != 2:
        return command
    password = getpass.getpass("新密码（输入不会回显）: ")
    return f"/set_password {shlex.quote(parts[1])} {shlex.quote(password)}"


def main() -> int:
    args = build_parser().parse_args()
    stdout_reconfigure = getattr(sys.stdout, "reconfigure", None)
    stderr_reconfigure = getattr(sys.stderr, "reconfigure", None)
    if callable(stdout_reconfigure):
        stdout_reconfigure(encoding="utf-8")
    if callable(stderr_reconfigure):
        stderr_reconfigure(encoding="utf-8")

    try:
        processor = build_command_processor(Path(args.env), args.timeout)
    except Exception as exc:  # noqa: BLE001 - startup should report configuration failures.
        print(f"机器人启动失败：{exc}", file=sys.stderr)
        return 1

    print(processor.help_text())
    print("输入 /quit 退出。使用 /set_password <账号> 后按提示隐藏输入新密码。")
    while True:
        try:
            command = input("tlbb> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if command.casefold() in {"/quit", "quit", "退出"}:
            return 0
        if not command:
            continue
        command = prompt_for_hidden_password(command)
        response = processor.handle(args.actor_id, command, is_private=True)
        print(response)


if __name__ == "__main__":
    raise SystemExit(main())
