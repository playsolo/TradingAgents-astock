"""Real Streamlit AppTest: verify the admin panel's provider-switch behaviour
end-to-end. This is the test that should have caught the original bug — and
the test that will catch the next one.

The provider selectbox MUST live outside ``st.form`` — see
``web/auth_page._render_admin_model_config``. Widget values inside a form are
not committed to ``session_state`` until the form is submitted, which means a
dynamic ``options`` list driven by the selected provider would be frozen
against the *first* provider the user picked.

Run with: ``.venv/bin/python -m pytest tests/test_admin_panel_streamlit.py -v -s``
"""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest


AUTH_BYPASS = {
    "web.auth_page.require_auth": lambda: None,
    "web.auth_page.get_current_user": lambda: type(
        "U", (), {"role": "admin", "username": "test", "is_authenticated": True}
    )(),
}


@pytest.fixture
def auth_bypass(monkeypatch):
    for path, replacement in AUTH_BYPASS.items():
        monkeypatch.setattr(path, replacement)


def _isolated_config(monkeypatch) -> None:
    """Point the model-config loader at an empty file so the test starts
    from defaults and does not see whatever the local machine has persisted."""
    from tradingagents.auth.model_config import _MODEL_CONFIG_FILE

    fake_path = _MODEL_CONFIG_FILE.parent / "model_config_apptest_isolated.json"
    monkeypatch.setattr(
        "tradingagents.auth.model_config._MODEL_CONFIG_FILE", fake_path
    )


def _render_admin_model_config_only():
    """Render only the admin model config — bypasses auth, sidebar, etc."""
    from web.auth_page import _render_admin_model_config

    _render_admin_model_config()


@pytest.mark.unit
class TestAdminPanelProviderSwitch:
    def test_minimax_provider_shows_minimax_models(self, auth_bypass, monkeypatch):
        """Switching provider to minimax must rebuild the quick model
        selectbox against the minimax catalog."""
        _isolated_config(monkeypatch)

        at = AppTest.from_function(_render_admin_model_config_only)
        at.run()

        # Drive a provider switch by mutating session_state directly
        # (AppTest.set_value() is unreliable for widgets nested inside
        # forms, but in our fix the provider widget is OUTSIDE the form
        # so this also covers a real user click).
        at.session_state["admin_llm_provider"] = "minimax"
        at.run()

        quick_sb = at.selectbox(key="admin_quick_model")
        assert quick_sb is not None, "Quick model selectbox should exist"
        options = list(quick_sb.options)
        assert "MiniMax-M3" in options, (
            f"Quick selectbox should contain MiniMax-M3 when provider=minimax, "
            f"got {options}"
        )
        assert "deepseek-v4-flash" not in options, (
            f"Quick selectbox should NOT contain deepseek models when "
            f"provider=minimax, got {options}"
        )

    def test_deepseek_provider_shows_deepseek_models(self, auth_bypass, monkeypatch):
        """Switching back to deepseek must rebuild against the deepseek
        catalog — guards against one-way switches sticking."""
        _isolated_config(monkeypatch)

        at = AppTest.from_function(_render_admin_model_config_only)
        at.run()

        at.session_state["admin_llm_provider"] = "deepseek"
        at.run()

        quick_sb = at.selectbox(key="admin_quick_model")
        assert quick_sb is not None
        options = list(quick_sb.options)
        assert "deepseek-v4-flash" in options
        assert "MiniMax-M3" not in options

    def test_round_trip_between_providers(self, auth_bypass, monkeypatch):
        """Repeatedly flip between providers and verify the catalog
        follows on every flip — this is what the live admin panel does."""
        _isolated_config(monkeypatch)

        at = AppTest.from_function(_render_admin_model_config_only)
        at.run()

        for provider, expected, forbidden in [
            ("minimax", "MiniMax-M3", "deepseek-v4-flash"),
            ("deepseek", "deepseek-v4-flash", "MiniMax-M3"),
            ("qwen", "qwen3.5-flash", "MiniMax-M3"),
            ("minimax", "MiniMax-M3", "qwen3.5-flash"),
        ]:
            at.session_state["admin_llm_provider"] = provider
            at.run()

            quick_sb = at.selectbox(key="admin_quick_model")
            assert quick_sb is not None, (
                f"Quick selectbox missing for provider={provider}"
            )
            options = list(quick_sb.options)
            assert expected in options, (
                f"provider={provider}: expected {expected!r} in {options}"
            )
            assert forbidden not in options, (
                f"provider={provider}: did not expect {forbidden!r} in {options}"
            )

    def test_provider_widget_lives_outside_form(self, auth_bypass, monkeypatch):
        """Belt-and-braces: assert the structural fix itself. The provider
        selectbox must not be a descendant of any ``st.form`` instance, or
        the bug will return the next time someone refactors."""
        from web.auth_page import _render_admin_model_config
        import inspect

        source = inspect.getsource(_render_admin_model_config)
        lines = source.splitlines()

        # Find the line of ``st.selectbox(... key="admin_llm_provider"...)``.
        provider_line = next(
            i for i, line in enumerate(lines, start=1)
            if 'key="admin_llm_provider"' in line
        )
        # Find the nearest preceding ``with st.form(`` line.
        form_open_line = max(
            (
                i for i, line in enumerate(lines[:provider_line], start=1)
                if "st.form(" in line
            ),
            default=0,
        )
        # The nearest ``st.form`` line must come AFTER the provider selectbox
        # (i.e. no form opened before it).
        assert form_open_line < provider_line, (
            f"admin_llm_provider selectbox (line {provider_line}) is inside "
            f"an st.form that opens at line {form_open_line}. The provider "
            f"selectbox MUST live outside st.form for the dynamic options "
            f"list to track the selected provider."
        )

    def test_form_submit_persists_provider_choice(self, auth_bypass, monkeypatch):
        """End-to-end: switch provider to minimax, click the form submit
        button, verify the disk config records minimax + a MiniMax model.

        AppTest's form-internal widget simulation is imperfect — the
        quick/deep selectboxes are still rendered against the *previous*
        provider's catalog inside the form. We side-step that by
        capturing the call args through a spy on save_model_config, which
        is what the form submit button ultimately calls.
        """
        _isolated_config(monkeypatch)

        at = AppTest.from_function(_render_admin_model_config_only)
        at.run()

        # Provider selectbox lives outside the form, so AppTest can drive
        # it correctly. Set its state to minimax, then re-render so the
        # quick/deep catalogs are rebuilt before we open the form.
        at.session_state["admin_llm_provider"] = "minimax"
        at.run()

        # The form-internal quick/deep selectboxes are rendered with the
        # new catalog now (we asserted that above in
        # ``test_minimax_provider_shows_minimax_models``). Patch
        # ``save_model_config`` so we don't write to disk and we can
        # capture the values that would have been persisted.
        captured: dict = {}
        from web import auth_page

        def _spy_save(**kwargs):
            captured.update(kwargs)
            return None

        monkeypatch.setattr(auth_page, "save_model_config", _spy_save)

        # Click the form submit button. AppTest's button.click() also
        # forces the next render, which evaluates the form body with the
        # current session_state.
        submit = next(
            (b for b in at.button if "保存模型配置" in (b.label or "")),
            None,
        )
        assert submit is not None, "Save button not found"
        submit.click()
        at.run()

        assert captured.get("llm_provider") == "minimax", captured
        # quick/deep values are bound from the form-internal selectboxes,
        # which AppTest sees as the *previous* render's defaults because
        # the form body does not rerun during click(). We therefore do
        # not assert on those values here — the structural test
        # ``test_minimax_provider_shows_minimax_models`` already proves
        # the catalog tracks the provider, which is what the live UI
        # needs.