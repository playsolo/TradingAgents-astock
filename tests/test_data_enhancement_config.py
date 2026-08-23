"""Tests for persisted data enhancement (HiThink) admin config."""

from __future__ import annotations

from pathlib import Path

import pytest

from tradingagents.auth import data_enhancement_config as dec


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch):
    path = tmp_path / "data_enhancement.json"
    monkeypatch.setattr(dec, "_CONFIG_FILE", path)
    return path


def test_save_and_load(cfg_path: Path):
    dec.save_data_enhancement_config(
        hithink_enabled=True,
        hithink_api_key="sk-test",
    )
    cfg = dec.load_data_enhancement_config()
    assert cfg["hithink_enabled"] is True
    assert cfg["hithink_api_key"] == "sk-test"


def test_save_preserves_key_when_empty(cfg_path: Path):
    dec.save_data_enhancement_config(hithink_enabled=True, hithink_api_key="sk-old")
    dec.save_data_enhancement_config(hithink_enabled=False, hithink_api_key=None)
    cfg = dec.load_data_enhancement_config()
    assert cfg["hithink_enabled"] is False
    assert cfg["hithink_api_key"] == "sk-old"
