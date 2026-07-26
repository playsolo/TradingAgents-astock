"""Streamlit login / register / pending / admin page for TradingAgents-Astock.

Supports:
- Persistent login via server-side session token + browser cookie
- Cookie written by ``st.html(..., unsafe_allow_javascript=True)`` (main page)
- Cookie read server-side by ``st.context.cookies`` (works on new tabs)
- Registration with pending approval (non-admin users)
- Admin review panel for pending accounts
- First-user auto-creates admin (immediately active)
- Admin-only model configuration (applied to all users)
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

import streamlit as st

from tradingagents.auth import UserManager, AuthConfig
from tradingagents.auth.model_config import load_model_config, save_model_config
from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS


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

# Cookie name — written by main-page JS (st.html), read by st.context.cookies
_COOKIE_NAME = "ta_token"

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


def get_current_username() -> str | None:
    """Return the current user's username, or None if auth is disabled."""
    mgr = _get_manager()
    if mgr.disabled:
        return None
    user = get_current_user()
    return user.username if user else None


def get_current_user():
    """Return current User object or None."""
    return st.session_state.get(_SESSION_USER)


def current_username() -> str | None:
    """Username of the logged-in user, or None when auth is disabled / no session.

    None deliberately maps to the legacy global watchlist store, preserving
    single-user behaviour for self-hosted installs with auth disabled.
    """
    user = get_current_user()
    return getattr(user, "username", None) if user else None


def current_watch_store():
    """Per-user :class:`WatchlistStore` for the current session.

    On first access by an admin, the pre-multiuser global ``watchlist.json`` is
    migrated into that admin's store so existing data isn't orphaned.
    """
    from tradingagents.watchlist.store import default_store, migrate_legacy_watchlist

    username = current_username()
    if username is None:
        return default_store()

    user = get_current_user()
    if user is not None and getattr(user, "role", None) == "admin":
        migrate_legacy_watchlist(username)
    return default_store(username)


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


def _read_token() -> str | None:
    """Read session token from session_state, URL query, or browser cookie.

    Order matters: after a fresh login the query param / session_state hold the
    new token, while an expired cookie from a previous session may still be
    present. Prefer the newer sources, then fall back to the cookie.
    """
    # 1. In-memory token from this Streamlit session (set at login / restore).
    mem = st.session_state.get("_auth_token")
    if mem:
        return str(mem)

    # 2. URL query param (same-tab fallback right after login).
    for key in (_COOKIE_NAME, "ta_user"):
        qp = st.query_params.get(key)
        if qp:
            return str(qp)

    # 3. Browser cookie (new tab / cold start).
    try:
        cookie_val = st.context.cookies.get(_COOKIE_NAME)
        if cookie_val:
            return str(cookie_val)
        legacy = st.context.cookies.get("ta_user")
        if legacy:
            return str(legacy)
    except Exception:
        pass

    return None


def _set_browser_cookie(token: str) -> None:
    """Write the session cookie on the main page via st.html JS.

    Must use ``st.html(..., unsafe_allow_javascript=True)`` — both
    ``st.markdown`` and sandboxed ``components.html`` cannot set the
    parent-page cookie reliably.
    """
    st.html(
        f"""<script>
(function() {{
    var name = "{_COOKIE_NAME}";
    var value = "{token}";
    var maxAge = {_SESSION_TTL};
    var secure = (location.protocol === "https:") ? "; Secure" : "";
    document.cookie = name + "=" + encodeURIComponent(value)
        + "; max-age=" + maxAge
        + "; path=/; SameSite=Lax" + secure;
    // Remove old cookie name from previous deploys.
    document.cookie = "ta_user=; max-age=0; path=/; SameSite=Lax" + secure;
    // Drop token from the address bar once the cookie is set.
    try {{
        var url = new URL(window.location.href);
        var dirty = false;
        if (url.searchParams.has(name)) {{ url.searchParams.delete(name); dirty = true; }}
        if (url.searchParams.has("ta_user")) {{ url.searchParams.delete("ta_user"); dirty = true; }}
        if (dirty) window.history.replaceState({{}}, "", url);
    }} catch (e) {{}}
}})();
</script>""",
        unsafe_allow_javascript=True,
    )


def _clear_browser_cookie() -> None:
    """Clear session cookie + URL params via main-page JS."""
    for key in (_COOKIE_NAME, "ta_user"):
        st.query_params.pop(key, None)

    st.html(
        f"""<script>
(function() {{
    var secure = (location.protocol === "https:") ? "; Secure" : "";
    document.cookie = "{_COOKIE_NAME}=; max-age=0; path=/; SameSite=Lax" + secure;
    document.cookie = "ta_user=; max-age=0; path=/; SameSite=Lax" + secure;
    try {{
        var url = new URL(window.location.href);
        url.searchParams.delete("{_COOKIE_NAME}");
        url.searchParams.delete("ta_user");
        window.history.replaceState({{}}, "", url);
    }} catch (e) {{}}
}})();
</script>""",
        unsafe_allow_javascript=True,
    )


def _cookie_matches(token: str) -> bool:
    """Return True if the browser cookie already equals ``token``."""
    try:
        current = st.context.cookies.get(_COOKIE_NAME) or st.context.cookies.get("ta_user")
        return bool(current) and str(current) == str(token)
    except Exception:
        return False


def _restore_session() -> bool:
    """Try to restore a logged-in session from cookie / URL token.

    Priority:
    1. st.session_state already has user
    2. token from session_state / URL query / cookie
    3. nothing → return False
    """
    if _SESSION_USER in st.session_state:
        return True

    token = _read_token()
    if not token:
        return False

    username = _validate_session_token(token)
    if username is None:
        # Stale token — clear cookie/URL so a future login can succeed.
        st.session_state.pop("_auth_token", None)
        _clear_browser_cookie()
        return False

    mgr = _get_manager()
    stored_user = mgr.get_user(username)
    if stored_user is None or stored_user.status != "active":
        _delete_session_token(token)
        st.session_state.pop("_auth_token", None)
        _clear_browser_cookie()
        return False

    stored_user.is_authenticated = True
    st.session_state[_SESSION_USER] = stored_user
    st.session_state[_SESSION_PAGE] = "login"
    st.session_state["_auth_token"] = token
    return True


def _save_session(username: str) -> None:
    """Create a server-side session; cookie is set on the next authenticated render.

    Login handlers call ``st.rerun()`` immediately after this, so injecting JS
    here would be discarded.  Token is kept in session_state and the cookie is
    written when ``require_auth`` returns into the main app.
    """
    token = _create_session_token(username)
    st.session_state["_auth_token"] = token
    st.query_params[_COOKIE_NAME] = token
    st.query_params.pop("ta_user", None)


def _ensure_session_cookie() -> None:
    """If the browser cookie is missing or stale, inject JS to set the current token."""
    token = st.session_state.get("_auth_token") or _read_token()
    if not token:
        return
    if _cookie_matches(token):
        # Cookie is good — scrub token from URL if still present.
        if _COOKIE_NAME in st.query_params or "ta_user" in st.query_params:
            st.query_params.pop(_COOKIE_NAME, None)
            st.query_params.pop("ta_user", None)
        return
    _set_browser_cookie(token)


def _clear_session() -> None:
    """Destroy the server-side session and clear the browser cookie."""
    token = st.session_state.pop("_auth_token", None) or _read_token()
    if token:
        _delete_session_token(token)
    _clear_browser_cookie()


# ---------------------------------------------------------------------------
# Auth gate (must be called from app.py after set_page_config)
# ---------------------------------------------------------------------------


def require_auth() -> None:
    """Render login/register/pending/admin page if not authenticated.

    Must be called from ``web/app.py`` **after** ``st.set_page_config()``.
    """
    mgr = _get_manager()

    # Handle deferred logout: clear everything and rerun.
    if st.session_state.pop(_SESSION_LOGOUT_PENDING, False):
        st.session_state.pop(_SESSION_USER, None)
        st.session_state[_SESSION_PAGE] = "login"
        _clear_session()
        st.rerun()

    # Try cookie/URL-based restore (works on refresh and new tab).
    if _SESSION_USER not in st.session_state:
        _restore_session()

    user = st.session_state.get(_SESSION_USER)

    # Authenticated + active → proceed to the app.
    if user and getattr(user, "is_authenticated", False):
        if user.status == "active":
            # Persist cookie on the main document so new tabs restore login.
            _ensure_session_cookie()
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


def _render_admin_model_config() -> None:
    """Admin-only form to set the model config that all users will use.

    Persisted to disk — survives server restart.
    Call inside an expander; the expander label provides the section title.
    """
    admin_config = load_model_config()

    provider_keys = [
        "minimax", "deepseek", "qwen", "glm", "openai",
        "anthropic", "google", "xai", "openrouter", "ollama",
    ]
    provider_labels = {
        "minimax": "MiniMax（推荐·国内直连）",
        "deepseek": "DeepSeek",
        "qwen": "通义千问 Qwen",
        "glm": "智谱 GLM",
        "openai": "OpenAI",
        "anthropic": "Anthropic",
        "google": "Google Gemini",
        "xai": "xAI Grok",
        "openrouter": "OpenRouter（聚合）",
        "ollama": "Ollama（本地）",
    }

    current_provider = admin_config.get("llm_provider", "deepseek")
    prov_idx = provider_keys.index(current_provider) if current_provider in provider_keys else 0

    # The provider selectbox MUST live outside ``st.form`` for the admin
    # panel to work correctly. Inside a form, widget values are not
    # committed to ``session_state`` until the user clicks the form's
    # submit button, which means a dynamic ``options`` list driven by the
    # selected provider (i.e. the per-provider model catalog) would be
    # frozen against the *first* provider the user picked, no matter how
    # many times they switch providers before saving. Putting the provider
    # selectbox above the form lets every rerun pick up the new value,
    # which in turn rebuilds the quick/deep model selectboxes below with
    # the right catalog.
    selected_provider = st.selectbox(
        "LLM 供应商",
        options=provider_keys,
        index=prov_idx,
        format_func=lambda k: provider_labels.get(k, k),
        key="admin_llm_provider",
        help="切换后下方模型下拉会自动刷新为该供应商的可用模型。",
    )

    quick_models: list[str] = []
    deep_models: list[str] = []
    if selected_provider in MODEL_OPTIONS:
        quick_models = [v for _, v in MODEL_OPTIONS[selected_provider]["quick"]]
        deep_models = [v for _, v in MODEL_OPTIONS[selected_provider]["deep"]]

    current_quick = admin_config.get("quick_think_llm", "deepseek-v4-flash")
    current_deep = admin_config.get("deep_think_llm", "deepseek-v4-pro")
    quick_idx = quick_models.index(current_quick) if current_quick in quick_models else 0
    deep_idx = deep_models.index(current_deep) if current_deep in deep_models else 0

    with st.form("admin_model_config_form", clear_on_submit=False):
        if quick_models:
            quick_val = st.selectbox(
                "快速思考模型",
                options=quick_models,
                index=quick_idx,
                key="admin_quick_model",
            )
        else:
            quick_val = st.text_input("快速思考模型 ID", value=current_quick, key="admin_quick_model")

        if deep_models:
            deep_val = st.selectbox(
                "深度思考模型",
                options=deep_models,
                index=deep_idx,
                key="admin_deep_model",
            )
        else:
            deep_val = st.text_input("深度思考模型 ID", value=current_deep, key="admin_deep_model")

        backend_val = st.text_input(
            "API Base URL（可选）",
            value=admin_config.get("backend_url") or "",
            key="admin_llm_base_url",
            placeholder="例: https://your-proxy.com/v1",
        )

        if st.form_submit_button("保存模型配置", type="primary", use_container_width=True):
            save_model_config(
                llm_provider=selected_provider,
                deep_think_llm=deep_val,
                quick_think_llm=quick_val,
                backend_url=backend_val.strip() or None,
            )
            st.success("模型配置已保存，全体用户立即生效")
            st.rerun()


def render_model_config_info() -> None:
    """Show non-admin users the inherited model config (read-only)."""
    cfg = load_model_config()
    provider_labels = {
        "minimax": "MiniMax", "deepseek": "DeepSeek", "qwen": "Qwen",
        "glm": "GLM", "openai": "OpenAI", "anthropic": "Anthropic",
        "google": "Gemini", "xai": "Grok", "openrouter": "OpenRouter",
        "ollama": "Ollama",
    }
    st.caption(
        f"🤖 模型：{provider_labels.get(cfg['llm_provider'], cfg['llm_provider'])}"
        f"（快速 {cfg['quick_think_llm']} · 深度 {cfg['deep_think_llm']}）"
    )


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

    # ── Model config (below user management, same collapsible style) ─
    with st.expander("⚙️ 模型配置（全局）", expanded=False):
        _render_admin_model_config()


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
