from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tlbb_bot.admin_registry import (  # noqa: E402
    AdminRegistryError,
    TelegramAdminRegistry,
)


class TelegramAdminRegistryTests(unittest.TestCase):
    def test_add_remove_and_reload_persistent_admins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "admins.json"
            registry = TelegramAdminRegistry(
                super_admin_id=100,
                static_admin_ids={200},
                store_path=store,
            )

            self.assertTrue(registry.is_authorized(100))
            self.assertTrue(registry.is_authorized(200))
            self.assertTrue(registry.add(300))
            self.assertFalse(registry.add(300))
            self.assertEqual(registry.list_admin_ids(), (200, 300))

            reloaded = TelegramAdminRegistry(
                super_admin_id=100,
                static_admin_ids={200},
                store_path=store,
            )
            self.assertTrue(reloaded.is_authorized(300))
            self.assertTrue(reloaded.remove(300))
            self.assertFalse(reloaded.remove(300))

            final = TelegramAdminRegistry(
                super_admin_id=100,
                static_admin_ids={200},
                store_path=store,
            )
            self.assertFalse(final.is_authorized(300))
            self.assertEqual(
                json.loads(store.read_text(encoding="utf-8")),
                {"version": 1, "admin_user_ids": []},
            )

    def test_super_and_static_admins_cannot_be_changed(self) -> None:
        registry = TelegramAdminRegistry(
            super_admin_id=100,
            static_admin_ids={200},
        )

        with self.assertRaisesRegex(ValueError, "总管理员"):
            registry.add(100)
        with self.assertRaisesRegex(ValueError, "不能删除总管理员"):
            registry.remove(100)
        with self.assertRaisesRegex(ValueError, "来自 .env"):
            registry.remove(200)

    def test_invalid_ids_and_corrupt_store_fail_closed(self) -> None:
        registry = TelegramAdminRegistry(super_admin_id=100)
        for value in (0, -1, "01", "abc", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                registry.add(value)

        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "admins.json"
            store.write_text("{broken", encoding="utf-8")
            with self.assertRaises(AdminRegistryError):
                TelegramAdminRegistry(
                    super_admin_id=100,
                    store_path=store,
                )

    def test_failed_atomic_replace_does_not_change_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "admins.json"
            registry = TelegramAdminRegistry(
                super_admin_id=100,
                store_path=store,
            )

            with patch(
                "tlbb_bot.admin_registry.os.replace",
                side_effect=OSError("disk failure"),
            ), self.assertRaises(AdminRegistryError):
                registry.add(300)

            self.assertFalse(registry.is_authorized(300))
            self.assertEqual(registry.list_admin_ids(), ())


if __name__ == "__main__":
    unittest.main()
