"""Companion can ask the desktop to start a Robinhood login."""
import inspect


def test_robinhood_login_route_is_a_control_post():
    import monitor
    assert "/api/robinhood/login/start" in monitor._CONTROL_POST_PATHS


def test_companion_start_uses_saved_creds_and_queues_login():
    from gui import MarketAdvisorGUI
    src = inspect.getsource(MarketAdvisorGUI._on_monitor_rh_login_req)
    assert "_saved_rh_credentials" in src
    assert '_begin_rh_login' in src
    assert "companion" in src
