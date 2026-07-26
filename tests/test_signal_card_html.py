"""Plan A: signal card shows providers that actually answered."""

from __future__ import annotations

from web.components.report_viewer import signal_card_html


def test_actual_single_provider_no_fallback_line():
    html = signal_card_html(
        "Buy",
        "NFLX 通信服务",
        "2026-07-18",
        elapsed=None,
        final_state={
            "llm_provider": "minimax",
            "deep_think_llm": "MiniMax-M3",
            "quick_think_llm": "MiniMax-M3",
            "llm_providers_used": ["minimax"],
            "llm_models_used": ["MiniMax-M3"],
            "llm_fallback_chain": [
                {"provider": "deepseek", "model": "deepseek-v4-flash"},
            ],
        },
    )
    assert "🤖 minimax" in html
    assert "模型 MiniMax-M3" in html
    assert "兜底" not in html
    assert "deepseek" not in html


def test_actual_switched_providers_joined_with_plus():
    html = signal_card_html(
        "Hold",
        "600519",
        "2026-07-18",
        final_state={
            "llm_provider": "minimax",
            "llm_providers_used": ["minimax", "deepseek"],
            "llm_models_used": ["MiniMax-M3", "deepseek-v4-flash"],
        },
    )
    assert "🤖 minimax+deepseek" in html
    assert "MiniMax-M3 → deepseek-v4-flash" in html
    assert "兜底" not in html


def test_legacy_report_omits_configured_fallback_line():
    """Old logs only had llm_fallback_chain — do not imply it was used."""
    html = signal_card_html(
        "Hold",
        "ISRG",
        "2026-07-18",
        final_state={
            "llm_provider": "minimax",
            "deep_think_llm": "MiniMax-M3",
            "quick_think_llm": "MiniMax-M3",
            "llm_fallback_chain": [
                {"provider": "deepseek", "model": "deepseek-v4-flash"},
            ],
        },
    )
    assert "模型 MiniMax-M3" in html
    assert "🤖 minimax" in html
    assert "兜底" not in html


def test_elapsed_and_provenance_both_render():
    html = signal_card_html(
        "Buy",
        "600519",
        "2026-07-01",
        elapsed=125.0,
        final_state={
            "llm_provider": "deepseek",
            "llm_providers_used": ["deepseek"],
            "llm_models_used": ["deepseek-v4-flash"],
        },
    )
    assert "耗时 2:05" in html
    assert "模型 deepseek-v4-flash" in html
    assert "🤖 deepseek" in html


def test_provenance_escapes_untrusted_model_names():
    html = signal_card_html(
        "Hold",
        "TEST",
        "2026-01-01",
        final_state={
            "llm_providers_used": ['<script>alert(1)</script>'],
            "llm_models_used": ["safe"],
        },
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
