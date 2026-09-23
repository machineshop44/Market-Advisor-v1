"""1.42.33 — overnight peel bookkeeping + buy hours retry + PDT/WO quieting."""
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_sold_qty_and_partial_peel():
    import auto_cycle as ac

    st = "Sell-All partial peel market Filled (2)"
    assert ac.sold_qty_from_sell_status(st, 2.99) == 2.0
    assert ac.sell_status_is_partial_peel(st, 2.99, 2.0) is True
    assert ac.sell_status_is_partial_peel("Sell-All Filled (3)", 3.0, 3.0) is False


def test_buy_working_left_working():
    import auto_cycle as ac

    assert ac.buy_order_is_working_unfilled(
        "Buy submitted pending fill (PENDING; left working)"
    )
    assert not ac.buy_order_is_working_unfilled("Fail: boom")


def test_working_orders_expire():
    import working_orders as wo

    wo._orders.clear()
    wo._loaded = True
    wo.register(
        broker="E*TRADE", order_id="x1", side="BUY", ticker="AMC",
        dollars=40.0, status="pending",
    )
    # Force old ts
    for v in wo._orders.values():
        v["ts"] = time.time() - 8000
    n = wo.expire_stale(ttl_sec=7200)
    assert n >= 1
    assert wo.open_notional("E*TRADE") == 0.0


def test_advisor_park_limit_unfilled_and_product_id():
    from activity_log_util import advisor_miss_park_spec

    park = advisor_miss_park_spec(
        "LCID: Skipped: Limit unfilled (queued) — cancelled"
    )
    assert park and park[1] == "limit_unfilled"
    park2 = advisor_miss_park_spec(
        'Fail: 400 Client Error: Bad Request {"error":"INVALID_ARGUMENT",'
        '"error_details":"Invalid product_id"}'
    )
    assert park2 and park2[1] == "broker_route"


def test_rh_equity_left_working_not_cancel():
    """Equity confirm timeout must not cancel (crypto still may)."""
    import broker as br

    cls = getattr(br, "RobinhoodAdapter", None) or getattr(br, "RobinhoodBroker", None)
    assert cls is not None

    class FakeRH(cls):
        def __init__(self):
            pass

        def cancel_order(self, oid, is_crypto=False):
            raise AssertionError("equity path must not cancel")

    rh = FakeRH()
    st, spent, oid = rh._rh_cancel_unfilled("abc", "queued", is_crypto=False)
    assert "left working" in st.lower()
    assert oid == "abc"
    assert float(spent or 0) == 0.0
