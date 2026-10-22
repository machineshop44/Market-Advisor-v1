"""Manual RH/CB connect must restore arm intent the way a silent reconnect does."""
import inspect


def test_rh_and_cb_connect_restore_arm():
    from gui import MarketAdvisorGUI
    rh = inspect.getsource(MarketAdvisorGUI._on_rh_connect_finished)
    cb = inspect.getsource(MarketAdvisorGUI._on_cb_connect_finished)
    assert '_maybe_restore_broker_arm("Robinhood"' in rh
    assert '_maybe_restore_broker_arm("Coinbase"' in cb
