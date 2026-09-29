from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tlbb_bot.ban_admin_login import BanAdminClient, load_config


def main() -> None:
    config = load_config(Path(".env"))
    client = BanAdminClient(config, session_path=Path("ban_admin_session.json"), timeout=25)
    session = client.login()
    print(
        {
            "backend": session.backend,
            "token_present": bool(session.token),
            "token_len": len(session.token),
            "user": session.user,
            "session_path": "ban_admin_session.json",
        }
    )


if __name__ == "__main__":
    main()
