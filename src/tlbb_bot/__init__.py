"""Reusable helpers for TLBB automation projects."""

from typing import TYPE_CHECKING

__all__ = [
    "BanAdminAPIError",
    "BanAdminClient",
    "BanAdminConfig",
    "BanAdminContractError",
    "BanAdminHTTPError",
    "GameUserPassword",
    "LoginSession",
    "RoleAction",
]

if TYPE_CHECKING:
    from .ban_admin_login import (
        BanAdminAPIError,
        BanAdminClient,
        BanAdminConfig,
        BanAdminContractError,
        BanAdminHTTPError,
        GameUserPassword,
        LoginSession,
        RoleAction,
    )


def __getattr__(name: str):
    if name in __all__:
        from .ban_admin_login import (
            BanAdminAPIError,
            BanAdminClient,
            BanAdminConfig,
            BanAdminContractError,
            BanAdminHTTPError,
            GameUserPassword,
            LoginSession,
            RoleAction,
        )

        exports = {
            "BanAdminAPIError": BanAdminAPIError,
            "BanAdminClient": BanAdminClient,
            "BanAdminConfig": BanAdminConfig,
            "BanAdminContractError": BanAdminContractError,
            "BanAdminHTTPError": BanAdminHTTPError,
            "GameUserPassword": GameUserPassword,
            "LoginSession": LoginSession,
            "RoleAction": RoleAction,
        }
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
