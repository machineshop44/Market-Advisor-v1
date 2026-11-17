"""1.43.3 — live tape board and contract headlines on any US name."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Src"))

import desk_radar
import news_guard as ng

NOW = 1_800_000_000.0


def _items(*titles, age_h=2.0):
    return [{"title": t, "ts": NOW - age_h * 3600} for t in titles]


def test_yahoo_screener_keeps_equities_and_prices():
    payload = {
        "finance": {
            "result": [{
                "quotes": [
                    {"symbol": "CCJ", "quoteType": "EQUITY", "regularMarketPrice": 64.51},
                    {"symbol": "BTC-USD", "quoteType": "CRYPTOCURRENCY", "regularMarketPrice": 90000},
                    {"symbol": "CEG", "quoteType": "EQUITY", "regularMarketPrice": 268.07},
                    {"symbol": "CCJ", "quoteType": "EQUITY", "regularMarketPrice": 60},
                ],
            }],
        },
    }
    rows = desk_radar.parse_yahoo_screener_quotes(payload)
    assert [r["symbol"] for r in rows] == ["CCJ", "CEG"]
    assert rows[0]["price"] == 64.51


def test_board_includes_a_name_that_is_not_on_a_stored_list():
    rows = [
        {"symbol": "CCJ", "price": 64.51, "source": "yahoo"},
        {"symbol": "SOUN", "price": 11.0, "source": "yahoo"},
    ]
    board = desk_radar.assemble_market_board(rows, max_share=80, max_total=30, max_above=8)
    syms = [r["symbol"] for r in board]
    assert syms == ["CCJ", "SOUN"]
    assert all("mover" in r["type"].lower() for r in board)


def test_expensive_tape_cannot_push_an_affordable_name_off():
    expensive = ["NVDA", "TSLA", "AMZN", "META", "AVGO", "ORCL", "CRM", "COST", "JPM", "NFLX", "AMD", "MSFT"]
    rows = [{"symbol": "CEG", "price": 268.0, "source": "yahoo"}]
    rows.extend({"symbol": n, "price": 220.0, "source": "yahoo"} for n in expensive)
    rows.append({"symbol": "SOUN", "price": 12.0, "source": "yahoo"})
    board = desk_radar.assemble_market_board(rows, max_share=80, max_total=30, max_above=8)
    syms = [r["symbol"] for r in board]
    assert "SOUN" in syms
    assert "CEG" in syms
    assert sum(1 for s in syms if s in set(expensive)) <= 8


def test_board_caps_the_tape():
    rows = [{"symbol": f"B{chr(65 + (i % 26))}{chr(65 + (i // 26))}", "price": 10.0, "source": "rh"} for i in range(40)]
    # f-string above can emit digits? BAA style. Verify alpha only via the function.
    board = desk_radar.assemble_market_board(rows, max_share=50, max_total=30, max_above=8)
    assert len(board) == 30


def test_cameco_contract_is_a_catalyst_for_any_name():
    title = "Cameco Signs Long-Term Uranium Supply Agreement with India"
    assert ng.is_major_contract_headline(title)
    ok, tag, _ = ng.headline_catalyst_eval(_items(title), now=NOW)
    assert ok and tag == "major_contract"
    rows = ng.discover_catalyst_megacaps(
        pool=["CCJ", "NVDA"],
        fetch=lambda s: _items(title) if s == "CCJ" else [],
        name_fn=lambda s: "Cameco" if s == "CCJ" else s,
        now=NOW,
        use_cache=False,
        mega_only=False,
    )
    assert [r["symbol"] for r in rows] == ["CCJ"]
    assert rows[0]["tag"] == "major_contract"


def test_share_sale_is_not_a_contract():
    title = "XYZ signs a long-term agreement to sell shares in a public offering"
    assert not ng.is_major_contract_headline(title)


def test_mega_only_still_ignores_a_contract_outside_the_list():
    title = "Cameco Signs Long-Term Uranium Supply Agreement with India"
    rows = ng.discover_catalyst_megacaps(
        pool=["CCJ"],
        fetch=lambda s: _items(title),
        name_fn=lambda s: "Cameco",
        now=NOW,
        use_cache=False,
        mega_only=True,
    )
    assert rows == []
