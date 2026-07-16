"""L2 消息催化剂 B2：有新闻且涨幅/预期未透支才计分。"""

from tradingagents.strategies.value_swing import (
    StockInfo,
    _NEWS_RET_5D_MAX,
    l2_factor_hits,
    news_catalyst_hit,
    run_l2_filter_impl,
)


class TestNewsCatalystHit:
    def test_no_news_never_hits(self):
        assert news_catalyst_hit(StockInfo(code="001")) is False
        assert news_catalyst_hit({"news_found": False, "ret_5d": 0.01}) is False

    def test_news_alone_hits_when_no_overextension_signals(self):
        assert news_catalyst_hit(StockInfo(code="001", news_found=True)) is True

    def test_news_blocked_by_high_5d_return(self):
        info = StockInfo(code="001", news_found=True, ret_5d=_NEWS_RET_5D_MAX)
        assert news_catalyst_hit(info) is False
        assert news_catalyst_hit(
            StockInfo(code="001", news_found=True, ret_5d=_NEWS_RET_5D_MAX + 0.01)
        ) is False

    def test_news_allowed_when_5d_return_below_cap(self):
        assert news_catalyst_hit(
            StockInfo(code="001", news_found=True, ret_5d=_NEWS_RET_5D_MAX - 0.01)
        ) is True

    def test_news_blocked_by_expectation_overhang(self):
        assert news_catalyst_hit(
            StockInfo(code="001", news_found=True, exp_score_delta=-1)
        ) is False

    def test_news_allowed_with_expectation_bonus(self):
        assert news_catalyst_hit(
            StockInfo(code="001", news_found=True, exp_score_delta=1, ret_5d=0.05)
        ) is True


class TestL2NewsCatalystScoring:
    def test_news_without_overextension_scores_one(self):
        info = StockInfo(code="001", news_found=True)
        assert run_l2_filter_impl([info])[0].signal_score == 1

    def test_news_with_high_return_scores_zero(self):
        info = StockInfo(code="001", news_found=True, ret_5d=0.15)
        assert run_l2_filter_impl([info])[0].signal_score == 0

    def test_news_with_exp_penalty_scores_zero_floor(self):
        # news blocked + exp −1 → floor at 0
        info = StockInfo(code="001", news_found=True, exp_score_delta=-1)
        assert run_l2_filter_impl([info])[0].signal_score == 0

    def test_factor_hit_matches_scoring_gate(self):
        blocked = StockInfo(code="001", news_found=True, ret_5d=0.20)
        by_key = {h["key"]: h for h in l2_factor_hits(blocked)}
        assert by_key["news_found"]["hit"] is False
        assert run_l2_filter_impl([blocked])[0].signal_score == 0
