"""美股展示名：拒收交易所标签、yfinance 取名、报告抽取优先 fundamentals。"""

import web.stock_display as stock_display


def test_rejects_exchange_label_from_market_report_parens(monkeypatch):
    monkeypatch.setattr(stock_display, "resolve_stock_name", lambda ticker: None)

    state = {
        "market_report": "**分析标的：** MNSO (纽约证券交易所)\n行业：零售",
        "fundamentals_report": "MINISO（名创优品）是一家全球化生活方式零售商",
    }

    assert stock_display.stock_display_label("MNSO", state) == "MNSO 名创优品"


def test_clean_extracted_name_does_not_chop_exchange_into_fake_name():
    # regression: 「纽约证券交易所」曾被「交易」切开成「纽约证券」
    assert stock_display._is_exchange_or_market_label("纽约证券交易所")
    assert stock_display._is_exchange_or_market_label("纽约证券")
    assert stock_display._is_exchange_or_market_label("NYSE")
    assert stock_display._is_exchange_or_market_label("纳斯达克")
    assert not stock_display._is_plausible_stock_name("纽约证券", "MNSO")
    assert not stock_display._is_plausible_stock_name("纽约证券交易所", "MNSO")


def test_prefers_fundamentals_chinese_over_earlier_market_noise(monkeypatch):
    monkeypatch.setattr(stock_display, "resolve_stock_name", lambda ticker: None)

    state = {
        "market_report": "MNSO (NYSE) 技术分析\nMNSO 走势偏弱",
        "sentiment_report": "情绪中性",
        "fundamentals_report": "公司 MINISO（名创优品）市值约 38 亿美元",
    }

    assert (
        stock_display._extract_stock_name_from_state("MNSO", state) == "名创优品"
    )
    assert stock_display.stock_display_label("MNSO", state) == "MNSO 名创优品"


def test_resolve_us_name_uses_yfinance_and_cache(monkeypatch, tmp_path):
    cache = stock_display.StockNameCache(tmp_path / "names.json")
    monkeypatch.setattr(stock_display, "_NAME_CACHE", cache)
    stock_display.resolve_stock_name.cache_clear()
    stock_display._yfinance_name.cache_clear()

    monkeypatch.setattr(
        stock_display,
        "_yfinance_name",
        lambda code: "MINISO Group Holding Limited",
    )
    # no CN alias so English short/long name is used
    monkeypatch.setattr(stock_display, "_US_CN_ALIASES", {})

    assert stock_display.resolve_stock_name("MNSO") == "MINISO Group Holding Limited"
    assert cache.get("MNSO") == "MINISO Group Holding Limited"

    def boom(_code):
        raise AssertionError("should not hit yfinance when cached")

    monkeypatch.setattr(stock_display, "_yfinance_name", boom)
    stock_display.resolve_stock_name.cache_clear()
    assert stock_display.resolve_stock_name("MNSO") == "MINISO Group Holding Limited"


def test_resolve_us_name_prefers_chinese_alias(monkeypatch, tmp_path):
    cache = stock_display.StockNameCache(tmp_path / "names.json")
    monkeypatch.setattr(stock_display, "_NAME_CACHE", cache)
    stock_display.resolve_stock_name.cache_clear()
    stock_display._yfinance_name.cache_clear()

    monkeypatch.setattr(stock_display, "_US_CN_ALIASES", {"MNSO": "名创优品"})
    monkeypatch.setattr(
        stock_display,
        "_yfinance_name",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("alias first")),
    )

    assert stock_display.resolve_stock_name("MNSO") == "名创优品"
    assert cache.get("MNSO") == "名创优品"


def test_display_prefers_chinese_state_over_english_resolve(monkeypatch):
    monkeypatch.setattr(
        stock_display,
        "resolve_stock_name",
        lambda ticker: "MINISO Group Holding Limited",
    )

    state = {
        "fundamentals_report": "MINISO（名创优品）主营生活方式零售",
    }
    assert stock_display.stock_display_label("MNSO", state) == "MNSO 名创优品"


def test_a_share_label_unchanged_when_rejecting_exchanges(monkeypatch):
    monkeypatch.setattr(stock_display, "resolve_stock_name", lambda ticker: None)
    state = {
        "market_report": "# 600602 云赛智联 技术面分析报告\n**标的：600602 云赛智联**",
    }
    assert stock_display.stock_display_label("600602", state) == "600602 云赛智联"


def test_rejects_prose_parentheticals_and_english_suffixes(monkeypatch):
    monkeypatch.setattr(stock_display, "resolve_stock_name", lambda ticker: None)
    state = {
        "news_report": "对MNSO这样的中国ADR（以美元计价）产生汇率影响",
        "fundamentals_report": (
            "MINISO Group Holding Limited（MNSO）\n"
            "MINISO（名创优品）是一家全球化生活方式零售商"
        ),
    }
    assert stock_display._extract_stock_name_from_state("MNSO", state) == "名创优品"
    assert stock_display.stock_display_label("MNSO", state) == "MNSO 名创优品"
    assert not stock_display._is_plausible_stock_name("以美元计价", "MNSO")
    assert not stock_display._is_plausible_stock_name("Limited", "MNSO")


def test_us_english_resolve_beats_diluted_eps_jargon(monkeypatch):
    monkeypatch.setattr(
        stock_display, "resolve_stock_name", lambda ticker: "Micron Technology"
    )
    state = {
        "fundamentals_report": "MU 稀释 每股收益同比改善，Micron Technology 库存去化",
    }
    assert stock_display._extract_stock_name_from_text(
        "MU", "MU 稀释 每股收益同比改善"
    ) is None
    assert stock_display.stock_display_label("MU", state) == "MU Micron Technology"


def test_us_english_resolve_beats_dividend_prose_as_fake_name(monkeypatch):
    """Hero regression: 'ISRG 不派发股息' must not become the display name."""
    monkeypatch.setattr(
        stock_display, "resolve_stock_name", lambda ticker: "Intuitive Surgical"
    )
    prose = "ISRG 不派发股息，但通过积极的股票回购来回报股东。"
    state = {"fundamentals_report": prose, "company_of_interest": "ISRG"}
    assert stock_display._extract_stock_name_from_text("ISRG", prose) is None
    assert not stock_display._is_plausible_stock_name("不派发股息", "ISRG")
    assert stock_display.stock_display_label("ISRG", state) == "ISRG Intuitive Surgical"


def test_resolve_skips_polluted_jargon_cache(monkeypatch, tmp_path):
    cache = stock_display.StockNameCache(tmp_path / "names.json")
    cache.set("601899", "静态")
    cache.set("MU", "稀释")
    monkeypatch.setattr(stock_display, "_NAME_CACHE", cache)
    stock_display.resolve_stock_name.cache_clear()
    stock_display._tencent_name.cache_clear()
    stock_display._yfinance_name.cache_clear()

    monkeypatch.setattr(stock_display, "_tencent_name", lambda code: "紫金矿业")
    monkeypatch.setattr(stock_display, "_mootdx_name_if_cached", lambda code: None)
    monkeypatch.setattr(stock_display, "_yfinance_name", lambda code: "Micron Technology")
    monkeypatch.setattr(stock_display, "_US_CN_ALIASES", {})

    assert stock_display.resolve_stock_name("601899") == "紫金矿业"
    assert cache.get("601899") == "紫金矿业"
    assert stock_display.resolve_stock_name("MU") == "Micron Technology"
    assert cache.get("MU") == "Micron Technology"
