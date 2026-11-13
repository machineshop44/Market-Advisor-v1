"""1.42.78 — news catalyst mega lane: discovery + RH fractional only when tagged."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Src"))

import news_guard as ng
from scoring import buy_candidate_affordable

NOW = 1_800_000_000.0


def _items(*titles, age_h=2.0):
    return [{"title": t, "ts": NOW - age_h * 3600} for t in titles]


def test_ceo_resign_is_catalyst_eval_not_veto():
    items = _items("Nvidia CEO Jensen Huang resigns amid scandal")
    ok, tag, title = ng.headline_catalyst_eval(items, now=NOW)
    assert ok and tag == "ceo_exit"
    assert "resign" in title.lower()
    veto, _ = ng.headline_veto(items, now=NOW)
    assert not veto


def test_offering_still_vetoes_even_if_plunge_wording():
    items = _items("NVDA plunges after company prices $2 billion public offering")
    # Plunge pattern may match catalyst eval…
    ok, tag, _ = ng.headline_catalyst_eval(items, now=NOW)
    assert ok and tag == "headline_plunge"
    # …but dilution veto must still block the catalyst lane.
    veto, why = ng.headline_veto(items, now=NOW)
    assert veto and "offering" in why


def test_discover_catalyst_keeps_mega_with_ceo_headline():
    pool = ["NVDA", "AEHL", "CIFR"]
    news = {
        "NVDA": _items("NVIDIA CEO steps down after misconduct probe"),
        "AEHL": _items("AEHL soars on contract win"),
        "CIFR": _items("CIFR announces expansion"),
    }
    rows = ng.discover_catalyst_megacaps(
        pool=pool,
        fetch=lambda s: news.get(s, []),
        name_fn=lambda s: {"NVDA": "NVIDIA Corporation"}.get(s, s),
        now=NOW,
        use_cache=False,
        max_n=4,
    )
    assert len(rows) == 1
    assert rows[0]["symbol"] == "NVDA"
    assert rows[0]["tag"] == "ceo_exit"


def test_discover_catalyst_drops_mega_with_offering_veto():
    pool = ["NVDA"]
    news = {
        "NVDA": _items("NVDA plunges after prices public offering of shares"),
    }
    rows = ng.discover_catalyst_megacaps(
        pool=pool,
        fetch=lambda s: news.get(s, []),
        name_fn=lambda s: "NVIDIA Corporation",
        now=NOW,
        use_cache=False,
    )
    assert rows == []


def test_afford_fractional_allows_news_catalyst_mega():
    # Small RH book cannot buy 1 NVDA share; catalyst lane allows fractional min ticket.
    settings = {"min_trade_dollars": 5.0, "target_bp_utilization_pct": 88.0}
    ok, why = buy_candidate_affordable(
        buying_power=50.0,
        price=180.0,
        is_crypto=False,
        broker_id="ROBINHOOD",
        equity=200.0,
        settings=settings,
        prefer_whole_shares=True,
        allow_fractional=True,
    )
    assert ok, why


def test_afford_prefer_whole_still_blocks_untagged_mega():
    settings = {"min_trade_dollars": 5.0, "target_bp_utilization_pct": 88.0}
    ok, why = buy_candidate_affordable(
        buying_power=50.0,
        price=180.0,
        is_crypto=False,
        broker_id="ROBINHOOD",
        equity=200.0,
        settings=settings,
        prefer_whole_shares=True,
        allow_fractional=False,
    )
    assert not ok
    assert "whole share" in why


def test_auto_cycle_passes_news_catalyst_fractional_flag():
    import auto_cycle as ac

    cands = [
        {
            "ticker": "NVDA",
            "price": 180.0,
            "asset_type": "stock",
            "news_catalyst": True,
            "score": 95,
        },
        {
            "ticker": "AAPL",
            "price": 250.0,
            "asset_type": "stock",
            "score": 92,
        },
    ]
    affordable, dropped = ac.filter_affordable_buy_candidates(
        cands,
        buying_power=50.0,
        equity=200.0,
        broker_id="ROBINHOOD",
        settings={"min_trade_dollars": 5.0, "target_bp_utilization_pct": 88.0},
        prefer_whole_shares=True,
    )
    tickers = {str(c.get("ticker")).upper() for c in affordable}
    assert "NVDA" in tickers
    assert "AAPL" not in tickers
    assert any("AAPL" in d for d in dropped)


def test_advisor_propose_stores_catalyst_flags(tmp_path, monkeypatch):
    import advisor_queue as aq

    path = tmp_path / "advisor_queue.json"
    monkeypatch.setattr(aq, "QUEUE_FILE", str(path))
    path.write_text('{"proposals":[],"repropose_cooldown":{}}', encoding="utf-8")
    prop = aq.propose(
        broker="Robinhood",
        ticker="NVDA",
        asset_type="stock",
        price=180.0,
        dollars=40.0,
        score=95.0,
        engine="NEWS_CATALYST",
        reason="news_catalyst",
        news_catalyst=True,
        allow_fractional=True,
        catalyst_tag="ceo_exit",
        catalyst_title="NVIDIA CEO steps down",
        honor_cooldown=False,
    )
    assert prop is not None
    assert prop.get("news_catalyst") is True
    assert prop.get("allow_fractional") is True
    assert prop.get("engine") == "NEWS_CATALYST"
    assert prop.get("catalyst_tag") == "ceo_exit"
