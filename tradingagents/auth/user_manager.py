"""User management: password hashing, CRUD, authentication.

All user data stored in a single JSON file at ``~/.tradingagents/auth/users.json``.
Uses only Python standard library — no extra dependencies.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USERS_FILE = Path.home() / ".tradingagents" / "auth" / "users.json"

# PBKDF2 parameters
_HASH_ALGO = "sha256"
_SALT_BYTES = 32
_ITERATIONS = 600_000

# Env var to disable auth entirely (dev / self-host)
_DISABLE_AUTH_ENV = "TRADINGAGENTS_DISABLE_AUTH"

Role = Literal["admin", "user"]
UserStatus = Literal["pending", "active", "disabled"]

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class User:
    """A registered user (public view, no hash material)."""

    username: str
    role: Role = "user"
    display_name: str = ""
    status: UserStatus = "pending"

    # Runtime-only flags — never serialised.
    is_authenticated: bool = False

    def __post_init__(self) -> None:
        if self.role not in ("admin", "user"):
            msg = f"Invalid role {self.role!r}; must be 'admin' or 'user'"
            raise ValueError(msg)
        if self.status not in ("pending", "active", "disabled"):
            msg = f"Invalid status {self.status!r}"
            raise ValueError(msg)

    @property
    def is_active(self) -> bool:
        return self.status == "active"


@dataclass
class _StoredUser:
    """Internal on-disk representation (includes hash material + status)."""

    username: str
    password_hash: str  # hex digest
    salt: str  # hex
    role: Role = "user"
    display_name: str = ""
    status: UserStatus = "pending"

    @classmethod
    def create(
        cls,
        username: str,
        password: str,
        role: Role = "user",
        display_name: str = "",
        status: UserStatus = "pending",
    ) -> _StoredUser:
        salt = secrets.token_hex(_SALT_BYTES)
        pwh = _hash_password(password, salt)
        return cls(
            username=username,
            password_hash=pwh,
            salt=salt,
            role=role,
            display_name=display_name or username,
            status=status,
        )

    def verify(self, password: str) -> bool:
        return secrets.compare_digest(_hash_password(password, self.salt), self.password_hash)

    def to_user(self) -> User:
        return User(
            username=self.username,
            role=self.role,
            display_name=self.display_name,
            status=self.status,
        )


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def _hash_password(password: str, salt_hex: str) -> str:
    """PBKDF2-HMAC-SHA256 hex digest."""
    return hashlib.pbkdf2_hmac(
        _HASH_ALGO,
        password.encode("utf-8"),
        salt_hex.encode("ascii"),
        _ITERATIONS,
    ).hex()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class AuthConfig:
    """Settings exposed to callers.

    All fields can be overridden via constructor kwargs.
    """

    disable_auth: bool = False
    users_file: Path = _USERS_FILE
    allow_registration: bool = True
    min_password_length: int = 6

    @classmethod
    def from_env(cls) -> AuthConfig:
        return cls(
            disable_auth=os.getenv(_DISABLE_AUTH_ENV, "0").strip() in {"1", "true", "yes"},
        )


# ---------------------------------------------------------------------------
# User Manager
# ---------------------------------------------------------------------------


class UserManager:
    """Thread-safe user authentication & management.

    All mutations acquire a module-level lock because Streamlit runs in a
    single process — this is sufficient for safe file I/O.

    Usage::

        mgr = UserManager()
        user = mgr.authenticate("admin", "secret")
        if user:
            print("Welcome", user.display_name)

        mgr.create_user("alice", "p@ss", role="user")
    """

    def __init__(self, config: AuthConfig | None = None) -> None:
        self._config = config or AuthConfig.from_env()
        self._lock = threading.Lock()

    # -- Public queries -----------------------------------------------------

    @property
    def disabled(self) -> bool:
        return self._config.disable_auth

    def authenticate(self, username: str, password: str) -> User | None:
        """Return a :class:`User` if credentials are valid, else ``None``."""
        if self.disabled:
            return User(
                username=username, role="admin", display_name=username,
                status="active", is_authenticated=True,
            )

        stored = self._load_user(username)
        if stored is None:
            return None
        if not stored.verify(password):
            return None

        user = stored.to_user()
        user.is_authenticated = True
        return user

    def get_user(self, username: str) -> User | None:
        """Return public info for *username* (without authentication)."""
        stored = self._load_user(username)
        return stored.to_user() if stored else None

    def list_users(self) -> list[User]:
        """Return all registered users (public info only)."""
        return [s.to_user() for s in self._load_all().values()]

    def list_pending_users(self) -> list[User]:
        """Return all users with ``pending`` status."""
        return [s.to_user() for s in self._load_all().values() if s.status == "pending"]

    def user_count(self) -> int:
        return len(self._load_all())

    # -- Mutations ----------------------------------------------------------

    def create_user(
        self,
        username: str,
        password: str,
        role: Role = "user",
        display_name: str = "",
    ) -> User:
        """Register a new user. Raises :exc:`ValueError` on duplicate or weak password.

        New non-admin users are created with ``pending`` status and must be
        approved by an admin before they can log in.
        """
        self._validate_username(username)
        self._validate_password(password)

        with self._lock:
            store = self._load_all()
            if username in store:
                msg = f"User {username!r} already exists"
                raise ValueError(msg)

            # Admins created by another admin are active immediately; users need approval.
            status: UserStatus = "active" if role == "admin" else "pending"
            stored = _StoredUser.create(username, password, role=role, display_name=display_name, status=status)
            store[username] = stored
            self._save_all(store)

        user = stored.to_user()
        user.is_authenticated = False
        return user

    def approve_user(self, username: str) -> bool:
        """Set user status to ``active``. Returns ``True`` on success."""
        with self._lock:
            store = self._load_all()
            stored = store.get(username)
            if stored is None:
                return False
            stored.status = "active"
            self._save_all(store)
        return True

    def reject_user(self, username: str) -> bool:
        """Set user status to ``disabled``. Returns ``True`` on success."""
        with self._lock:
            store = self._load_all()
            stored = store.get(username)
            if stored is None:
                return False
            stored.status = "disabled"
            self._save_all(store)
        return True

    def delete_user(self, username: str) -> bool:
        """Remove a user. Returns ``True`` if deleted, ``False`` if not found."""
        with self._lock:
            store = self._load_all()
            if username not in store:
                return False
            del store[username]
            self._save_all(store)
        return True

    def change_password(self, username: str, old_password: str, new_password: str) -> bool:
        """Change password for *username*. Returns ``True`` on success."""
        with self._lock:
            store = self._load_all()
            stored = store.get(username)
            if stored is None or not stored.verify(old_password):
                return False
            self._validate_password(new_password)
            new_stored = _StoredUser.create(
                username, new_password, role=stored.role,
                display_name=stored.display_name, status=stored.status,
            )
            store[username] = new_stored
            self._save_all(store)
        return True

    def change_role(self, username: str, new_role: Role) -> bool:
        """Change a user's role. Returns ``True`` on success."""
        with self._lock:
            store = self._load_all()
            stored = store.get(username)
            if stored is None:
                return False
            stored.role = new_role
            self._save_all(store)
        return True

    def reset_password(self, username: str, new_password: str) -> bool:
        """Admin force-reset a user's password (no old-password check).

        Thread-safe: acquires the internal lock. Returns ``True`` on success.
        """
        self._validate_password(new_password)
        with self._lock:
            store = self._load_all()
            stored = store.get(username)
            if stored is None:
                return False
            new_stored = _StoredUser.create(
                username, new_password, role=stored.role,
                display_name=stored.display_name, status=stored.status,
            )
            store[username] = new_stored
            self._save_all(store)
        return True

    # -- Internals ----------------------------------------------------------

    def _load_user(self, username: str) -> _StoredUser | None:
        return self._load_all().get(username)

    def _load_all(self) -> dict[str, _StoredUser]:
        path = self._config.users_file
        if not path.exists():
            return {}
        try:
            raw = path.read_text("utf-8")
            data = json.loads(raw)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        result: dict[str, _StoredUser] = {}
        for entry in data.get("users", []):
            try:
                su = _StoredUser(
                    username=entry["username"],
                    password_hash=entry["password_hash"],
                    salt=entry["salt"],
                    role=entry.get("role", "user"),
                    display_name=entry.get("display_name", entry["username"]),
                    status=entry.get("status", "active"),  # legacy: old entries default to active
                )
                result[su.username] = su
            except (KeyError, TypeError):
                continue  # skip corrupt entries
        return result

    def _save_all(self, users: dict[str, _StoredUser]) -> None:
        path = self._config.users_file
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 2,
            "users": [
                {
                    "username": su.username,
                    "password_hash": su.password_hash,
                    "salt": su.salt,
                    "role": su.role,
                    "display_name": su.display_name,
                    "status": su.status,
                }
                for su in users.values()
            ],
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _validate_username(username: str) -> None:
        if not username or len(username) < 2:
            msg = "Username must be at least 2 characters"
            raise ValueError(msg)
        if not username.isidentifier():
            msg = f"Invalid username {username!r} — use letters, digits, and underscores only"
            raise ValueError(msg)

    def _validate_password(self, password: str) -> None:
        min_len = self._config.min_password_length
        if len(password) < min_len:
            msg = f"Password must be at least {min_len} characters"
            raise ValueError(msg)
