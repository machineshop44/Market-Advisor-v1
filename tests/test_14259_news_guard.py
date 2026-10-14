"""1.42.59 — equity entry news guard: earnings block, headline veto, shadow catalyst tag."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Src"))

import news_guard as ng
from analytics import format_round_trip_report, summarize_round_trips

NOW = 1_800_000_000.0


def _items(*titles, age_h=2.0):
    return [{"title": t, "ts": NOW - age_h * 3600} for t in titles]


def test_normalize_old_and_new_yfinance_shapes():
    raw = [
        {"title": "ACHR wins contract", "providerPublishTime": 1_700_000_000},
        {"content": {"title": "SMCI prices offering", "pubDate": "2026-10-13T12:00:00Z"}},
        {"content": {"title": ""}},
    ]
    out = ng.normalize_items(raw)
    assert [o["title"] for o in out] == ["ACHR wins contract", "SMCI prices offering"]
    assert all(o["ts"] for o in out)


def test_offering_headline_vetoes():
    veto, why = ng.headline_veto(_items("AMC announces $250 million at-the-market offering"), now=NOW)
    assert veto and "offering" in why


def test_going_concern_and_halt_veto():
    assert ng.headline_veto(_items("Auditor flags going concern doubt"), now=NOW)[0]
    assert ng.headline_veto(_items("NIO shares halted pending news"), now=NOW)[0]


def test_neutral_headline_passes():
    assert not ng.headline_veto(_items("SNAP launches new AR glasses"), now=NOW)[0]


def test_old_headline_ignored():
    assert not ng.headline_veto(_items("Company prices public offering", age_h=48), now=NOW)[0]


def test_earnings_blocks_entry():
    why, tag = ng.entry_news_check(
        "SNAP", settings={}, earnings_fn=lambda t: True, name_fn=None, fetch=lambda t: [], now=NOW,
    )
    assert "earnings" in why and tag is None


def test_feed_down_fails_open():
    assert ng.entry_news_check(
        "SNAP", settings={}, earnings_fn=lambda t: None, name_fn=None, fetch=lambda t: None, now=NOW,
    ) == (None, None)


def test_catalyst_tag_without_veto():
    why, tag = ng.entry_news_check(
        "ACHR", settings={}, earnings_fn=lambda t: False, name_fn=None,
        fetch=lambda t: _items("ACHR wins Air Force contract"), now=NOW,
    )
    assert why is None and tag is True
    why, tag = ng.entry_news_check(
        "ACHR", settings={}, earnings_fn=lambda t: False, name_fn=None, fetch=lambda t: [], now=NOW,
    )
    assert why is None and tag is False


def test_settings_disable_blocks():
    s = {"entry_earnings_block": False, "entry_news_veto": False}
    why, tag = ng.entry_news_check(
        "AMC", settings=s, earnings_fn=lambda t: True, name_fn=None,
        fetch=lambda t: _items("AMC prices public offering"), now=NOW,
    )
    assert why is None and tag is True


def test_aliases_from_company_names():
    assert ng.name_aliases("SMCI", "Super Micro Computer, Inc.") == [
        "SMCI", "Super Micro Computer", "Super Micro",
    ]
    assert ng.name_aliases("SOFI", "SoFi Technologies, Inc.") == ["SOFI", "SoFi"]
    assert ng.name_aliases("NIO", "NIO Inc.") == ["NIO"]


def test_roundup_headline_about_other_company_ignored():
    aliases = ng.name_aliases("SOFI", "SoFi Technologies, Inc.")
    roundup = _items("Adobe downgraded, VF Corp. upgraded: Wall Street's top analyst calls")
    assert not ng.headline_veto(roundup, now=NOW, aliases=aliases)[0]
    assert not ng.has_fresh_catalyst(roundup, now=NOW, aliases=aliases)
    own = _items("SoFi prices $1.5 billion public offering")
    assert ng.headline_veto(own, now=NOW, aliases=aliases)[0]


def test_snap_verb_not_company():
    aliases = ng.name_aliases("SNAP", "Snap Inc.")
    assert not ng.has_fresh_catalyst(_items("Stocks snap losing streak"), now=NOW, aliases=aliases)
    assert ng.has_fresh_catalyst(_items("Snap unveils new Spectacles"), now=NOW, aliases=aliases)


RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>SMCI prices public offering</title><pubDate>Tue, 13 Oct 2026 14:00:00 +0000</pubDate></item>
<item><title>No date headline</title></item>
</channel></rss>"""


def test_parse_rss():
    out = ng.parse_rss(RSS)
    assert out[0]["title"] == "SMCI prices public offering" and out[0]["ts"] > 0
    assert out[1]["ts"] is None
    assert ng.parse_rss("not xml") == []


def test_merge_discovery_us_only_dedup_cap():
    out = ng.merge_discovery(
        ["CIFR", "CNQ.TO", "BAC", "^GSPC"],
        [["CIFR", "AEHL"], ["BRK-B", "PFAI", "GRAB"]],
        exclude=["BAC"], max_n=3,
    )
    assert out == ["CIFR", "AEHL", "PFAI"]


def test_breakout_universe_reserves_news_slots():
    import desk_radar
    rh = [f"R{c}" for c in "ABCDEFGHIJKLMNOPQRST"]
    out = desk_radar.merge_breakout_universe(
        ["FINA"], rh, [], news=["CIFR", "AEHL", "PFAI"], max_news=2, max_total=6,
    )
    syms = [o["symbol"] for o in out]
    assert syms[:3] == ["FINA", "CIFR", "AEHL"]
    assert "PFAI" not in syms and len(syms) == 6
    assert out[1]["type"] == "News Radar"


def test_round_trip_report_groups_by_news():
    base = {"broker": "Robinhood", "ticker": "ACHR", "qty": 2, "status": "Filled",
            "confirmed": True, "fee_est": 0.0}
    rows = [
        {**base, "timestamp": "2026-10-13T10:00:00", "side": "BUY", "price": 5.0,
         "dollars": 10.0, "news_catalyst": True},
        {**base, "timestamp": "2026-10-13T11:00:00", "side": "SELL", "price": 5.2,
         "dollars": 10.4, "reason": "TTP trail"},
    ]
    s = summarize_round_trips(rows)
    assert "fresh news" in (s.get("by_news") or {})
    assert "By news catalyst" in format_round_trip_report(s)
