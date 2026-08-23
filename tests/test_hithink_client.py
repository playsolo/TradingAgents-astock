"""Unit tests for HiThink client (mocked HTTP)."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from tradingagents.dataflows.hithink_client import (
    HiThinkAPIError,
    HiThinkClient,
    HiThinkRateLimitError,
    code_to_thscode,
    is_hithink_enabled,
)


@pytest.mark.unit
class TestCodeToThscode:
    def test_sh_main_board(self):
        assert code_to_thscode("600519") == "600519.SH"

    def test_sz_gem(self):
        assert code_to_thscode("300033") == "300033.SZ"

    def test_bj(self):
        assert code_to_thscode("920001") == "920001.BJ"


@pytest.mark.unit
class TestHiThinkEnabled:
    def test_disabled_without_env(self, monkeypatch):
        monkeypatch.delenv("HITHINK_ENABLED", raising=False)
        monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
        monkeypatch.setattr(
            "tradingagents.auth.data_enhancement_config.load_data_enhancement_config",
            lambda: {"hithink_enabled": False, "hithink_api_key": ""},
        )
        assert is_hithink_enabled() is False

    def test_enabled_with_flag_and_key(self, monkeypatch):
        monkeypatch.setenv("HITHINK_ENABLED", "true")
        monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "sk-test")
        assert is_hithink_enabled() is True


@pytest.mark.unit
class TestHiThinkClientGet:
    def _client(self) -> HiThinkClient:
        return HiThinkClient(api_key="sk-test", max_retries=2)

    def test_success_returns_data(self):
        client = self._client()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 0,
            "message": "success",
            "data": {"item": [{"thscode": "600519.SH"}]},
        }
        mock_resp.raise_for_status = MagicMock()
        client._session = MagicMock()
        client._session.get.return_value = mock_resp

        data = client.get("/api/meta/tickers/search", {"q": "600519"})
        assert data["item"][0]["thscode"] == "600519.SH"

    def test_business_error_raises(self):
        client = self._client()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 2003,
            "message": "Invalid key",
            "data": None,
        }
        mock_resp.raise_for_status = MagicMock()
        client._session = MagicMock()
        client._session.get.return_value = mock_resp

        with pytest.raises(HiThinkAPIError) as exc:
            client.get("/api/meta/tickers/search")
        assert exc.value.code == 2003

    @patch("tradingagents.dataflows.hithink_client.time.sleep")
    def test_rate_limit_retries_then_raises(self, _sleep):
        client = self._client()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 4001,
            "message": "rate limited",
            "data": None,
        }
        mock_resp.raise_for_status = MagicMock()
        client._session = MagicMock()
        client._session.get.return_value = mock_resp

        with pytest.raises(HiThinkRateLimitError):
            client.get("/api/meta/tickers/search")

    def test_http_error_raises(self):
        client = self._client()
        client._session = MagicMock()
        client._session.get.side_effect = requests.Timeout("timeout")

        with pytest.raises(HiThinkAPIError, match="HTTP request failed"):
            client.get("/api/meta/tickers/search")
