from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tlbb_backend import TLBBBackendClient, TLBBBackendError


def main() -> None:
    client = TLBBBackendClient.from_env()

    index_data, agent_rows = client.fetch_date(date.today())
    today_stats = index_data.get("todayStats") or {}
    print(
        "today:",
        "recharge=", today_stats.get("total_recharge", 0),
        "paid_orders=", today_stats.get("paid_order_count", 0),
        "new_users=", today_stats.get("user_reg_count", 0),
    )

    for row in agent_rows:
        print(
            row.get("agentId"),
            row.get("nickname") or row.get("rolename") or row.get("username"),
            row.get("total_recharge", 0),
            row.get("paid_order_count", 0),
            row.get("user_reg_count", 0),
        )


if __name__ == "__main__":
    try:
        main()
    except TLBBBackendError as exc:
        raise SystemExit(f"TLBB backend query failed: {exc}") from exc
