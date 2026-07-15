"""Streamlit login / register / pending / admin page for TradingAgents-Astock.

Supports:
- Persistent login via server-side session token + browser cookie (set via JS)
- Reliable behind nginx reverse proxy (no HttpOnly cookie dependency)
- Registration with pending approval (non-admin users)
- Admin review panel for pending accounts
- First-user auto-creates admin (immediately active)
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from tradingagents.auth import UserManager, AuthConfig


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SESSION_USER = "auth_user"
_SESSION_PAGE = "auth_page"
_SESSION_LOGOUT_PENDING = "_auth_logout_pending"

_PENDING_SESSION = "_auth_pending_username"

# Session token stored server-side at ~/.tradingagents/auth/sessions/<token>.json
_SESSIONS_DIR = Path.home() / ".tradingagents" / "auth" / "sessions"
_SESSION_TTL = 30 * 24 * 3600  # 30 days

# Cookie name set via frontend JS
_COOKIE_NAME = "ta_user"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_manager() -> UserManager:
    if "auth_manager" not in st.session_state:
        st.session_state["auth_manager"] = UserManager(AuthConfig.from_env())
    return st.session_state["auth_manager"]


def is_authenticated() -> bool:
    """Check if the current session has a logged-in user."""
    if _get_manager().disabled:
        return True
    user = st.session_state.get(_SESSION_USER)
    return bool(user and getattr(user, "is_authenticated", False))


def get_current_user():
    """Return current User object or None."""
    return st.session_state.get(_SESSION_USER)


def _ensure_sessions_dir() -> None:
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _create_session_token(username: str) -> str:
    """Create a server-side session file and return the token."""
    _ensure_sessions_dir()
    token = secrets.token_urlsafe(32)
    payload = {
        "username": username,
        "expires": time.time() + _SESSION_TTL,
    }
    (_SESSIONS_DIR / f"{token}.json").write_text(json.dumps(payload))
    return token


def _validate_session_token(token: str) -> str | None:
    """Validate a session token and return the username, or None."""
    session_file = _SESSIONS_DIR / f"{token}.json"
    if not session_file.exists():
        return None
    try:
        data = json.loads(session_file.read_text())
        if time.time() > data.get("expires", 0):
            session_file.unlink(missing_ok=True)
            return None
        return data.get("username")
    except (json.JSONDecodeError, OSError):
        return None


def _delete_session_token(token: str) -> None:
    session_file = _SESSIONS_DIR / f"{token}.json"
    session_file.unlink(missing_ok=True)


def _read_cookie_via_query() -> str | None:
    """Read the session token from the URL query param.

    On page load, a tiny frontend script copies the cookie value into the URL
    query param so the Streamlit server can read it.
    """
    token = st.query_params.get(_COOKIE_NAME)
    return str(token) if token else None


def _set_cookie_query(token: str) -> None:
    """Write the token to URL query param so the server can read it."""
    st.query_params[_COOKIE_NAME] = token


def _remove_cookie_query() -> None:
    st.query_params.pop(_COOKIE_NAME, None)


def _restore_session() -> bool:
    """Try to restore a logged-in session from server-side token.

    Priority:
    1. st.session_state already has user
    2. token in URL query param (from cookie on page load)
    3. nothing → return False

    Called once per page load in ``require_auth()``.
    Returns ``True`` if a session was restored.
    """
    if _SESSION_USER in st.session_state:
        return True

    token = _read_cookie_via_query()
    if not token:
        return False

    username = _validate_session_token(token)
    if username is None:
        # Token expired or invalid — clear query param.
        _remove_cookie_query()
        return False

    mgr = _get_manager()
    stored_user = mgr.get_user(username)
    if stored_user is None or stored_user.status != "active":
        _remove_cookie_query()
        _delete_session_token(token)
        return False

    stored_user.is_authenticated = True
    st.session_state[_SESSION_USER] = stored_user
    st.session_state[_SESSION_PAGE] = "login"
    return True


def _save_session(username: str) -> None:
    """Create a server-side session and set the cookie via JS in an iframe.

    On the current page load the iframe JS hasn't set the cookie yet, but on
    *subsequent* page loads the cookie will be available.
    """
    token = _create_session_token(username)
    _set_cookie_query(token)
    # Use components.html (iframe) to execute JS — st.markdown strips <script> tags.
    components.html(
        f"""<script>
(function() {{
    var name = "{_COOKIE_NAME}";
    var value = "{token}";
    var expires = new Date(Date.now() + {_SESSION_TTL * 1000}).toUTCString();
    document.cookie = name + "=" + value + "; expires=" + expires + "; path=/; SameSite=Lax";
}})();
</script>""",
        height=0,
        width=0,
    )


def _clear_session() -> None:
    """Destroy the server-side session and remove the URL query param."""
    token = _read_cookie_via_query()
    if token:
        _delete_session_token(token)
    _remove_cookie_query()
    # Also clear the cookie via JS in an iframe.
    components.html(
        f"""<script>
(function() {{
    document.cookie = "{_COOKIE_NAME}=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/; SameSite=Lax";
    var url = new URL(window.location.href);
    url.searchParams.delete("{_COOKIE_NAME}");
    window.history.replaceState({{}}, "", url);
}})();
</script>""",
        height=0,
        width=0,
    )


# ---------------------------------------------------------------------------
# Auth gate (must be called from app.py after set_page_config)
# ---------------------------------------------------------------------------


def require_auth() -> None:
    """Render login/register/pending/admin page if not authenticated.

    Must be called from ``web/app.py`` **after** ``st.set_page_config()``.
    """
    # On every page load, try to read the session cookie and copy it into the
    # URL query param so the Streamlit server can read it.
    # Uses components.html (iframe) because st.markdown strips <script> tags.
    components.html(
        f"""<script>
(function() {{
    var name = "{_COOKIE_NAME}";
    var match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
    if (match) {{
        var url = new URL(window.location.href);
        if (!url.searchParams.has(name)) {{
            url.searchParams.set(name, match[2]);
            window.location.replace(url.toString());
        }}
    }}
}})();
</script>""",
        height=0,
        width=0,
    )

    mgr = _get_manager()

    # Handle deferred logout: clear everything and rerun.
    if st.session_state.pop(_SESSION_LOGOUT_PENDING, False):
        st.session_state.pop(_SESSION_USER, None)
        st.session_state[_SESSION_PAGE] = "login"
        _clear_session()
        st.rerun()

    # Try server-side-session-based restore (on refresh / new tab).
    if _SESSION_USER not in st.session_state:
        _restore_session()

    user = st.session_state.get(_SESSION_USER)

    # Authenticated + active → proceed to the app.
    if user and getattr(user, "is_authenticated", False):
        if user.status == "active":
            return
        if user.status == "pending":
            _render_pending_page(user)
            st.stop()
        # disabled → force logout.
        st.session_state.pop(_SESSION_USER, None)
        _clear_session()

    page = st.session_state.get(_SESSION_PAGE, "login")

    # No users at all → force registration (first admin).
    if mgr.user_count() == 0:
        page = "register"
        st.session_state[_SESSION_PAGE] = "register"

    st.set_page_config(page_title="TradingAgents — 登录", page_icon="🔐", layout="centered")
    _inject_auth_styles()

    if page == "login":
        _render_login(mgr)
    elif page == "register":
        _render_register(mgr)
    elif page == "pending":
        _render_pending_page(None)
    else:
        _render_login(mgr)

    st.stop()


# ---------------------------------------------------------------------------
# Pending-approval page
# ---------------------------------------------------------------------------


def _render_pending_page(user) -> None:
    uname = user.display_name if user else st.session_state.get(_PENDING_SESSION, "")
    st.markdown(
        f"""
        <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:80vh;text-align:center;">
            <div style="font-size:4rem;margin-bottom:1rem;">⏳</div>
            <div style="font-size:1.8rem;font-weight:700;margin-bottom:0.5rem;color:#f5f1eb;">
                账号审核中
            </div>
            <div style="color:#888;font-size:1rem;max-width:400px;line-height:1.6;">
                <strong>{uname}</strong>，你的账号已注册成功，正在等待管理员审核。<br><br>
                审核通过后，你将可以正常使用系统。<br>
                请稍后再来查看。
            </div>
            <div style="margin-top:2rem;">
                <button onclick="setTimeout(function(){{location.reload();}},10000)"
                    style="background:#161616;color:#ff5a1f;border:1px solid #ff5a1f;
                           padding:0.5rem 1.5rem;border-radius:8px;cursor:pointer;font-size:0.9rem;">
                    点击刷新检查状态
                </button>
            </div>
            <div style="margin-top:1rem;">
                <a href="/" style="color:#555;font-size:0.8rem;">刷新页面</a>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Logout button (sidebar)
# ---------------------------------------------------------------------------


def render_logout_button() -> None:
    mgr = _get_manager()
    if mgr.disabled:
        return
    user = get_current_user()
    if not user:
        return

    pending_count = len(mgr.list_pending_users()) if user.role == "admin" else 0
    pending_tag = f" 🔔 {pending_count}" if pending_count else ""

    col1, col2 = st.columns([3, 1])
    status_tag = {"pending": "⏳", "active": "✅", "disabled": "🚫"}.get(user.status, "")
    col1.caption(f"👤 {user.display_name} {status_tag}{pending_tag}")
    if col2.button("退出", key="auth_logout_btn", use_container_width=True):
        st.session_state[_SESSION_LOGOUT_PENDING] = True
        st.rerun()


# ---------------------------------------------------------------------------
# Admin panel (sidebar)
# ---------------------------------------------------------------------------


def render_admin_panel() -> None:
    user = get_current_user()
    if not user or user.role != "admin":
        return

    mgr = _get_manager()

    with st.expander(
        "🔐 用户管理（管理员）",
        expanded=bool(mgr.list_pending_users()),
    ):
        # ── Pending review section ───────────────────────────────────
        pending_list = mgr.list_pending_users()
        if pending_list:
            st.markdown("##### ⏳ 待审核用户")
            for pu in pending_list:
                cols = st.columns([2, 1, 1])
                cols[0].write(pu.display_name)
                if cols[1].button("通过", key=f"approve_{pu.username}", use_container_width=True):
                    mgr.approve_user(pu.username)
                    st.success(f"已通过 {pu.display_name}")
                    st.rerun()
                if cols[2].button("拒绝", key=f"reject_{pu.username}", use_container_width=True):
                    mgr.reject_user(pu.username)
                    st.warning(f"已拒绝 {pu.display_name}")
                    st.rerun()
            st.divider()

        # ── All users list ───────────────────────────────────────────
        st.markdown("##### 所有用户")
        for u in mgr.list_users():
            status_icon = {"pending": "⏳", "active": "✅", "disabled": "🚫"}.get(u.status, "❓")
            cols = st.columns([2, 1, 1, 1])
            cols[0].write(f"{status_icon} {u.display_name}")
            cols[1].write(f"`{u.role}`")
            if u.username != user.username and u.status != "disabled":
                if cols[2].button("删除", key=f"del_{u.username}", use_container_width=True):
                    mgr.delete_user(u.username)
                    st.rerun()
            if u.username != user.username:
                if cols[3].button("改密", key=f"pw_{u.username}", use_container_width=True):
                    st.session_state["_auth_change_pw_for"] = u.username

        # Inline change-password dialog.
        target = st.session_state.pop("_auth_change_pw_for", None)
        if target:
            st.markdown(f"##### 修改 {target} 密码")
            new_pw = st.text_input("新密码", type="password", key="admin_new_pw")
            confirm = st.text_input("确认密码", type="password", key="admin_confirm_pw")
            if st.button("确认修改", key="admin_pw_confirm"):
                if new_pw != confirm:
                    st.error("两次密码不一致")
                elif len(new_pw) < mgr._config.min_password_length:
                    st.error(f"密码至少 {mgr._config.min_password_length} 位")
                else:
                    _force_change_password(mgr, target, new_pw)
                    st.success(f"{target} 密码已更新")
                    st.rerun()

        st.divider()
        st.markdown("##### 新建用户")
        with st.form("admin_create_user_form", clear_on_submit=True):
            new_username = st.text_input("用户名")
            new_pw = st.text_input("密码", type="password")
            new_role = st.selectbox("角色", ["user", "admin"], index=0)
            new_display = st.text_input("显示名称（可选）")
            if st.form_submit_button("创建用户"):
                try:
                    mgr.create_user(new_username, new_pw, role=new_role, display_name=new_display or "")
                    st.success(f"用户 {new_username} 创建成功")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))


def _force_change_password(mgr: UserManager, username: str, new_password: str) -> None:
    """Admin force-set a user's password (no old-password check)."""
    mgr.reset_password(username, new_password)


# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------


def _render_login(mgr: UserManager) -> None:
    st.markdown(
        """
        <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:80vh;text-align:center;">
            <div style="font-size:3rem;margin-bottom:0.5rem;">🔐</div>
            <div style="font-size:2rem;font-weight:900;margin-bottom:1.5rem;">
                <span style="color:#ff5a1f;">Trading</span><span style="color:#f5f1eb;">Agents</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.container():
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            with st.form("login_form"):
                username = st.text_input("用户名", placeholder="输入用户名")
                password = st.text_input("密码", type="password", placeholder="输入密码")
                submitted = st.form_submit_button("登录", type="primary", use_container_width=True)

                if submitted:
                    if not username or not password:
                        st.error("请输入用户名和密码")
                    else:
                        user = mgr.authenticate(username, password)
                        if user:
                            if user.status == "pending":
                                st.session_state[_PENDING_SESSION] = user.display_name
                                st.session_state[_SESSION_PAGE] = "pending"
                                st.rerun()
                            elif user.status == "disabled":
                                st.error("该账号已被禁用，请联系管理员")
                            else:
                                st.session_state[_SESSION_USER] = user
                                st.session_state[_SESSION_PAGE] = "login"
                                _save_session(user.username)
                                st.rerun()
                        else:
                            st.error("用户名或密码错误")

            if mgr._config.allow_registration:
                st.markdown("---")
                if st.button("注册新账号", use_container_width=True):
                    st.session_state[_SESSION_PAGE] = "register"
                    st.rerun()


# ---------------------------------------------------------------------------
# Register page
# ---------------------------------------------------------------------------


def _render_register(mgr: UserManager) -> None:
    is_first_user = mgr.user_count() == 0

    st.markdown(
        f"""
        <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:80vh;text-align:center;">
            <div style="font-size:3rem;margin-bottom:0.5rem;">{'🆕' if is_first_user else '📝'}</div>
            <div style="font-size:2rem;font-weight:900;margin-bottom:0.5rem;color:#f5f1eb;">
                {'创建管理员账号' if is_first_user else '注册新账号'}
            </div>
            <div style="color:#888;font-size:0.9rem;margin-bottom:1.5rem;">
                {'首次使用，请创建管理员账号以开始使用系统。' if is_first_user else '注册后需等待管理员审核通过方可登录。'}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.container():
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            with st.form("register_form"):
                new_username = st.text_input("用户名", placeholder="字母、数字、下划线")
                new_pw = st.text_input("密码", type="password", placeholder=f"至少 {mgr._config.min_password_length} 位")
                confirm_pw = st.text_input("确认密码", type="password", placeholder="再次输入密码")
                display_name = st.text_input("显示名称（可选）", placeholder="留空则使用用户名")

                submitted = st.form_submit_button("注册", type="primary", use_container_width=True)

                if submitted:
                    errors = []
                    if not new_username:
                        errors.append("请输入用户名")
                    if not new_pw:
                        errors.append("请输入密码")
                    if new_pw != confirm_pw:
                        errors.append("两次密码不一致")
                    if errors:
                        for e in errors:
                            st.error(e)
                    else:
                        try:
                            role: str = "admin" if is_first_user else "user"
                            mgr.create_user(
                                new_username,
                                new_pw,
                                role=role,
                                display_name=display_name or "",
                            )
                            if is_first_user:
                                st.success(f"管理员账号 {new_username} 创建成功！")
                                user = mgr.authenticate(new_username, new_pw)
                                if user:
                                    st.session_state[_SESSION_USER] = user
                                    st.session_state[_SESSION_PAGE] = "login"
                                    _save_session(user.username)
                                    st.rerun()
                            else:
                                st.success(f"账号 {new_username} 注册成功！请等待管理员审核通过后登录。")
                                st.session_state[_PENDING_SESSION] = display_name or new_username
                                st.session_state[_SESSION_PAGE] = "pending"
                                time.sleep(1.5)
                                st.rerun()
                        except ValueError as exc:
                            st.error(str(exc))

            if not is_first_user:
                st.markdown("---")
                if st.button("返回登录", use_container_width=True):
                    st.session_state[_SESSION_PAGE] = "login"
                    st.rerun()


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------


def _inject_auth_styles() -> None:
    st.markdown(
        """
        <style>
        html, body, [class*="css"] {
            font-family: 'Inter', -apple-system, sans-serif;
        }
        .stApp {
            background: #0a0a0a;
        }
        input[data-testid="stTextInputRootElement"] input,
        .stTextInput input {
            background: #161616 !important;
            border-color: #2a2a2a !important;
            color: #f5f1eb !important;
        }
        .stTextInput input:focus {
            border-color: #ff5a1f !important;
            box-shadow: 0 0 0 1px #ff5a1f !important;
        }
        button[kind="primary"] {
            background: linear-gradient(135deg, #ff5a1f, #ff8c42) !important;
            border: none !important;
            font-weight: 700 !important;
            letter-spacing: 0.05em !important;
            box-shadow: 0 4px 15px rgba(255,90,31,0.3) !important;
        }
        button[kind="primary"]:hover {
            background: linear-gradient(135deg, #e04d15, #ff5a1f) !important;
            box-shadow: 0 6px 20px rgba(255,90,31,0.4) !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
