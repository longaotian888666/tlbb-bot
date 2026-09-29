from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tlbb_backend import TLBBBackendClient, TLBBBackendError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query a TLBB player's direct agent.")
    parser.add_argument("account", help="Exact player account")
    parser.add_argument("--game-id", type=int, help="Game ID used to disambiguate duplicate accounts")
    parser.add_argument("--env", default=".env", help="Env file containing API_* credentials")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    client = TLBBBackendClient.from_env_file(args.env)
    result = client.get_player_agent(args.account, game_id=args.game_id)
    print(json.dumps({"found": result is not None, "player": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except TLBBBackendError as exc:
        raise SystemExit(f"TLBB player query failed: {exc}") from exc
