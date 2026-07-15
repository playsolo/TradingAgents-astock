"""Authentication module for TradingAgents-Astock.

Provides password-based user authentication for the Web UI.
Supports multiple users with admin/user roles.

Usage:
    from tradingagents.auth import UserManager

    mgr = UserManager()
    user = mgr.authenticate("username", "password")
    if user:
        print("Login OK, role:", user.role)

Environment variable:
    TRADINGAGENTS_DISABLE_AUTH=1  — skip authentication (dev/self-host)
"""

from __future__ import annotations

from tradingagents.auth.user_manager import UserManager, User, AuthConfig

__all__ = ["UserManager", "User", "AuthConfig"]
