"""侧边栏默认 LLM 供应商解析测试。

刷新 UI（新 Streamlit 会话）后 session_state 重置，供应商下拉会退回第一项
(MiniMax)。通过 DEFAULT_LLM_PROVIDER 环境变量可把默认项固定为 deepseek 等，
避免每次都要手动重选。
"""

import pytest


def test_default_provider_index_from_env(monkeypatch):
    from web.components import sidebar

    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "deepseek")
    assert sidebar._default_provider_index() == sidebar._PROVIDER_KEYS.index("deepseek")


def test_default_provider_index_is_case_insensitive(monkeypatch):
    from web.components import sidebar

    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "  DeepSeek  ")
    assert sidebar._default_provider_index() == sidebar._PROVIDER_KEYS.index("deepseek")


def test_default_provider_index_falls_back_to_first_when_unset(monkeypatch):
    from web.components import sidebar

    monkeypatch.delenv("DEFAULT_LLM_PROVIDER", raising=False)
    assert sidebar._default_provider_index() == 0


def test_default_provider_index_falls_back_on_invalid(monkeypatch):
    from web.components import sidebar

    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "not-a-provider")
    assert sidebar._default_provider_index() == 0
