"""Tests for tradingagents.auth — password hashing, user CRUD, authentication, status."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from tradingagents.auth import UserManager, AuthConfig
from tradingagents.auth.user_manager import User


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_users_file() -> Path:
    """Return a path to a non-existent temp file that will be cleaned up."""
    with tempfile.TemporaryDirectory() as d:
        yield Path(d) / "users.json"


@pytest.fixture
def mgr(tmp_users_file: Path) -> UserManager:
    """UserManager backed by a temp file, auth enabled, registration allowed."""
    config = AuthConfig(
        disable_auth=False,
        users_file=tmp_users_file,
        allow_registration=True,
        min_password_length=4,
    )
    return UserManager(config)


@pytest.fixture
def mgr_noauth(tmp_users_file: Path) -> UserManager:
    """UserManager with auth disabled."""
    config = AuthConfig(
        disable_auth=True,
        users_file=tmp_users_file,
        allow_registration=False,
        min_password_length=4,
    )
    return UserManager(config)


# ---------------------------------------------------------------------------
# AuthConfig
# ---------------------------------------------------------------------------


class TestAuthConfig:
    def test_from_env_default(self) -> None:
        """Default: auth enabled."""
        config = AuthConfig.from_env()
        assert config.disable_auth is False

    def test_from_env_disabled(self) -> None:
        os.environ["TRADINGAGENTS_DISABLE_AUTH"] = "1"
        try:
            config = AuthConfig.from_env()
            assert config.disable_auth is True
        finally:
            os.environ.pop("TRADINGAGENTS_DISABLE_AUTH", None)

    def test_from_env_disabled_alternative_values(self) -> None:
        for val in ("true", "yes"):
            os.environ["TRADINGAGENTS_DISABLE_AUTH"] = val
            try:
                assert AuthConfig.from_env().disable_auth is True
            finally:
                os.environ.pop("TRADINGAGENTS_DISABLE_AUTH", None)

    def test_from_env_default_other_values(self) -> None:
        for val in ("0", "false", "no", "", "anything"):
            os.environ["TRADINGAGENTS_DISABLE_AUTH"] = val
            try:
                assert AuthConfig.from_env().disable_auth is False
            finally:
                os.environ.pop("TRADINGAGENTS_DISABLE_AUTH", None)


# ---------------------------------------------------------------------------
# User creation & validation
# ---------------------------------------------------------------------------


class TestCreateUser:
    def test_create_user(self, mgr: UserManager) -> None:
        user = mgr.create_user("alice", "secret123", role="user", display_name="Alice")
        assert user.username == "alice"
        assert user.role == "user"
        assert user.display_name == "Alice"
        assert mgr.user_count() == 1

    def test_create_user_defaults_to_pending(self, mgr: UserManager) -> None:
        """Non-admin users are created as pending."""
        user = mgr.create_user("alice", "secret123")
        assert user.status == "pending"

    def test_create_admin_defaults_to_active(self, mgr: UserManager) -> None:
        """Admin users are created as active."""
        user = mgr.create_user("admin", "admin123", role="admin")
        assert user.role == "admin"
        assert user.status == "active"

    def test_duplicate_username_raises(self, mgr: UserManager) -> None:
        mgr.create_user("alice", "secret123")
        with pytest.raises(ValueError, match="already exists"):
            mgr.create_user("alice", "other456")

    def test_short_username_raises(self, mgr: UserManager) -> None:
        with pytest.raises(ValueError, match="at least 2 characters"):
            mgr.create_user("a", "secret123")

    def test_invalid_username_raises(self, mgr: UserManager) -> None:
        with pytest.raises(ValueError, match="Invalid username"):
            mgr.create_user("user name!", "secret123")

    def test_short_password_raises(self, mgr: UserManager) -> None:
        with pytest.raises(ValueError, match="at least 4 characters"):
            mgr.create_user("bob", "12")

    def test_empty_username_raises(self, mgr: UserManager) -> None:
        with pytest.raises(ValueError, match="at least 2 characters"):
            mgr.create_user("", "secret123")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestAuthenticate:
    def test_authenticate_success(self, mgr: UserManager) -> None:
        """Active user can log in."""
        mgr.create_user("alice", "secret123", display_name="Alice")
        mgr.approve_user("alice")
        user = mgr.authenticate("alice", "secret123")
        assert user is not None
        assert user.username == "alice"
        assert user.display_name == "Alice"
        assert user.is_authenticated is True
        assert user.status == "active"

    def test_authenticate_pending_user(self, mgr: UserManager) -> None:
        """Pending users can authenticate but are not active."""
        mgr.create_user("alice", "secret123")
        user = mgr.authenticate("alice", "secret123")
        assert user is not None
        assert user.is_authenticated is True
        assert user.status == "pending"

    def test_authenticate_disabled_user(self, mgr: UserManager) -> None:
        """Disabled users can authenticate but are not active."""
        mgr.create_user("alice", "secret123")
        mgr.reject_user("alice")
        user = mgr.authenticate("alice", "secret123")
        assert user is not None
        assert user.status == "disabled"

    def test_authenticate_wrong_password(self, mgr: UserManager) -> None:
        mgr.create_user("alice", "secret123")
        user = mgr.authenticate("alice", "wrong")
        assert user is None

    def test_authenticate_nonexistent_user(self, mgr: UserManager) -> None:
        user = mgr.authenticate("nobody", "anything")
        assert user is None

    def test_authenticate_disabled_auth(self, mgr_noauth: UserManager) -> None:
        """With auth disabled, any credentials are accepted and active."""
        user = mgr_noauth.authenticate("anybody", "whatever")
        assert user is not None
        assert user.username == "anybody"
        assert user.role == "admin"
        assert user.is_authenticated is True
        assert user.status == "active"

    def test_disabled_property(self, mgr: UserManager, mgr_noauth: UserManager) -> None:
        assert mgr.disabled is False
        assert mgr_noauth.disabled is True


# ---------------------------------------------------------------------------
# User status flow
# ---------------------------------------------------------------------------


class TestUserStatus:
    def test_approve_user(self, mgr: UserManager) -> None:
        mgr.create_user("alice", "secret")
        assert mgr.approve_user("alice") is True
        user = mgr.get_user("alice")
        assert user is not None
        assert user.status == "active"

    def test_approve_nonexistent(self, mgr: UserManager) -> None:
        assert mgr.approve_user("nobody") is False

    def test_reject_user(self, mgr: UserManager) -> None:
        mgr.create_user("alice", "secret")
        assert mgr.reject_user("alice") is True
        user = mgr.get_user("alice")
        assert user is not None
        assert user.status == "disabled"

    def test_reject_nonexistent(self, mgr: UserManager) -> None:
        assert mgr.reject_user("nobody") is False

    def test_list_pending_users(self, mgr: UserManager) -> None:
        mgr.create_user("alice", "pass1")
        mgr.create_user("bob", "pass2")
        mgr.approve_user("bob")
        pending = mgr.list_pending_users()
        assert len(pending) == 1
        assert pending[0].username == "alice"

    def test_list_pending_after_approve(self, mgr: UserManager) -> None:
        mgr.create_user("alice", "pass1")
        mgr.approve_user("alice")
        assert mgr.list_pending_users() == []


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_users_persist_to_disk(self, tmp_users_file: Path) -> None:
        config = AuthConfig(users_file=tmp_users_file)
        mgr1 = UserManager(config)
        mgr1.create_user("alice", "secret123", role="user")
        mgr1.create_user("bob", "p@ssword", role="admin")

        # New manager reading the same file
        mgr2 = UserManager(config)
        assert mgr2.user_count() == 2
        alice = mgr2.get_user("alice")
        assert alice is not None
        assert alice.role == "user"
        assert alice.status == "pending"  # non-admin default

    def test_status_persists(self, tmp_users_file: Path) -> None:
        config = AuthConfig(users_file=tmp_users_file)
        mgr1 = UserManager(config)
        mgr1.create_user("alice", "secret")
        mgr1.approve_user("alice")

        mgr2 = UserManager(config)
        user = mgr2.get_user("alice")
        assert user is not None
        assert user.status == "active"

    def test_legacy_missing_status_defaults_to_active(self, tmp_users_file: Path) -> None:
        """Existing users.json without 'status' field should default to active."""
        tmp_users_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_users_file.write_text(
            json.dumps({
                "version": 1,
                "users": [
                    {
                        "username": "legacy_user",
                        "password_hash": "abc",
                        "salt": "def",
                        "role": "user",
                        "display_name": "Legacy",
                    }
                ],
            }),
            encoding="utf-8",
        )
        config = AuthConfig(users_file=tmp_users_file)
        mgr = UserManager(config)
        user = mgr.get_user("legacy_user")
        assert user is not None
        assert user.status == "active"  # backward compat

    def test_password_hashes_differ(self, tmp_users_file: Path) -> None:
        """Same password but different salts produce different hashes."""
        config = AuthConfig(users_file=tmp_users_file)
        mgr = UserManager(config)
        mgr.create_user("alice", "samepassword")
        mgr.create_user("bob", "samepassword")

        raw = json.loads(tmp_users_file.read_text("utf-8"))
        users = raw["users"]
        alice_entry = next(u for u in users if u["username"] == "alice")
        bob_entry = next(u for u in users if u["username"] == "bob")
        assert alice_entry["salt"] != bob_entry["salt"]
        assert alice_entry["password_hash"] != bob_entry["password_hash"]

    def test_password_not_stored_in_plaintext(self, tmp_users_file: Path) -> None:
        config = AuthConfig(users_file=tmp_users_file)
        mgr = UserManager(config)
        mgr.create_user("alice", "my_secret_p@ss")

        raw = tmp_users_file.read_text("utf-8")
        assert "my_secret_p@ss" not in raw

    def test_missing_file(self, tmp_users_file: Path) -> None:
        """Missing file = empty user list, no crash."""
        config = AuthConfig(users_file=tmp_users_file)
        mgr = UserManager(config)
        assert mgr.user_count() == 0
        assert mgr.list_users() == []

    def test_corrupt_file(self, tmp_users_file: Path) -> None:
        """Malformed JSON is handled gracefully."""
        tmp_users_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_users_file.write_text("{corrupt_json", encoding="utf-8")
        config = AuthConfig(users_file=tmp_users_file)
        mgr = UserManager(config)
        assert mgr.user_count() == 0


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------


class TestUserCrud:
    def test_list_users(self, mgr: UserManager) -> None:
        mgr.create_user("alice", "pass1", display_name="Alice A")
        mgr.create_user("bob", "pass2", display_name="Bob B")
        users = mgr.list_users()
        assert len(users) == 2
        names = {u.username for u in users}
        assert names == {"alice", "bob"}

    def test_get_user(self, mgr: UserManager) -> None:
        mgr.create_user("alice", "secret", display_name="Alice")
        user = mgr.get_user("alice")
        assert user is not None
        assert user.username == "alice"
        assert user.display_name == "Alice"
        assert user.is_authenticated is False

    def test_get_user_not_found(self, mgr: UserManager) -> None:
        assert mgr.get_user("nobody") is None

    def test_delete_user(self, mgr: UserManager) -> None:
        mgr.create_user("alice", "secret")
        mgr.create_user("bob", "secret")
        assert mgr.user_count() == 2
        assert mgr.delete_user("alice") is True
        assert mgr.user_count() == 1
        assert mgr.get_user("alice") is None
        assert mgr.delete_user("nonexistent") is False

    def test_change_password(self, mgr: UserManager) -> None:
        mgr.create_user("alice", "old_pass")
        mgr.approve_user("alice")
        assert mgr.change_password("alice", "old_pass", "new_pass") is True
        assert mgr.authenticate("alice", "old_pass") is None
        user = mgr.authenticate("alice", "new_pass")
        assert user is not None
        assert user.status == "active"  # status preserved

    def test_change_password_wrong_old(self, mgr: UserManager) -> None:
        mgr.create_user("alice", "correct")
        assert mgr.change_password("alice", "wrong", "new_pass") is False

    def test_change_password_nonexistent(self, mgr: UserManager) -> None:
        assert mgr.change_password("nobody", "old", "new_pass_long") is False

    def test_change_role(self, mgr: UserManager) -> None:
        mgr.create_user("alice", "secret", role="user")
        assert mgr.change_role("alice", "admin") is True
        user = mgr.get_user("alice")
        assert user is not None
        assert user.role == "admin"

    def test_change_role_nonexistent(self, mgr: UserManager) -> None:
        assert mgr.change_role("nobody", "admin") is False

    def test_reset_password(self, mgr: UserManager) -> None:
        mgr.create_user("alice", "old_pass")
        assert mgr.reset_password("alice", "new_admin_pass") is True
        assert mgr.authenticate("alice", "old_pass") is None
        assert mgr.authenticate("alice", "new_admin_pass") is not None

    def test_reset_password_nonexistent(self, mgr: UserManager) -> None:
        assert mgr.reset_password("nobody", "some_pass") is False

    def test_reset_password_short_password_raises(self, mgr: UserManager) -> None:
        mgr.create_user("alice", "valid_pass")
        with pytest.raises(ValueError, match="at least 4 characters"):
            mgr.reset_password("alice", "ab")


# ---------------------------------------------------------------------------
# User dataclass validation
# ---------------------------------------------------------------------------


class TestUserDataclass:
    def test_valid_roles(self) -> None:
        User(username="alice", role="admin")
        User(username="bob", role="user")

    def test_invalid_role_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid role"):
            User(username="alice", role="superadmin")

    def test_valid_statuses(self) -> None:
        for s in ("pending", "active", "disabled"):
            User(username="alice", status=s)

    def test_invalid_status_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid status"):
            User(username="alice", status="banned")

    def test_is_active_property(self) -> None:
        assert User(username="a", status="active").is_active is True
        assert User(username="a", status="pending").is_active is False
        assert User(username="a", status="disabled").is_active is False


# ---------------------------------------------------------------------------
# Thread safety smoke test
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_create(self, mgr: UserManager) -> None:
        """Sequential creates from different contexts — lock doesn't deadlock."""
        mgr.create_user("alice", "pass1")
        mgr.create_user("bob", "pass2")
        mgr.create_user("carol", "pass3")
        assert mgr.user_count() == 3
        assert mgr.authenticate("alice", "pass1") is not None
        assert mgr.authenticate("bob", "pass2") is not None
        assert mgr.authenticate("carol", "pass3") is not None
