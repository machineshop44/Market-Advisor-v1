"""Fee estimates, rotate caps, analytics summaries, min profit-over-fees gates."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import analytics  # noqa: E402
import scoring  # noqa: E402


class FeeAnalyticsTests(unittest.TestCase):
    def test_round_trip_fees_by_profile(self):
        rh = scoring.estimate_round_trip_fee_pct("ROBINHOOD", "AAPL", "stock")
        rh_c = scoring.estimate_round_trip_fee_pct("ROBINHOOD", "BTC", "cryptocurrency")
        cb = scoring.estimate_round_trip_fee_pct("COINBASE", "BTC", "cryptocurrency")
        self.assertLess(rh, cb)
        self.assertLess(rh, rh_c)
        self.assertAlmostEqual(rh_c, 0.019, places=4)  # 0.95% × 2
        self.assertAlmostEqual(cb, 0.024, places=4)  # Intro 1 taker 1.2% × 2
        self.assertAlmostEqual(
            scoring.estimate_fee_dollars(1000, "COINBASE", "BTC", "cryptocurrency"),
            1000 * cb,
            places=4,
        )

    def test_fee_profiles_present_per_broker(self):
        expected = {
            "ROBINHOOD_STOCK",
            "ROBINHOOD_CRYPTO",
            "COINBASE",
            "ETRADE_STOCK",
        }
        self.assertEqual(expected, set(scoring.FEE_PROFILES.keys()))
        self.assertEqual(expected, set(scoring._FEE_ONE_WAY_PCT.keys()))
        # Equity vs crypto distinct on RH
        self.assertNotEqual(
            scoring.FEE_PROFILES["ROBINHOOD_STOCK"],
            scoring.FEE_PROFILES["ROBINHOOD_CRYPTO"],
        )
        self.assertEqual(
            scoring.fee_profile_key("ROBINHOOD", "AAPL", "stock"),
            "ROBINHOOD_STOCK",
        )
        self.assertEqual(
            scoring.fee_profile_key("ROBINHOOD", "BTC", "cryptocurrency"),
            "ROBINHOOD_CRYPTO",
        )
        self.assertEqual(scoring.fee_profile_key("COINBASE", "ETH", ""), "COINBASE")
        self.assertEqual(scoring.fee_profile_key("ETRADE", "SPY", "stock"), "ETRADE_STOCK")

    def test_exit_threshold_ge_fee_rt_plus_min_profit(self):
        self.assertAlmostEqual(scoring.MIN_PROFIT_OVER_FEES_PCT, 0.01)
        cases = [
            ("ROBINHOOD", "AAPL", "stock"),
            ("ROBINHOOD", "BTC", "cryptocurrency"),
            ("COINBASE", "ETH", "cryptocurrency"),
            ("ETRADE", "SPY", "stock"),
            ("E*TRADE", "MSFT", "Equity"),
        ]
        for broker, ticker, asset in cases:
            floor = scoring.min_profit_exit_roi_pct(broker, ticker, asset)
            fees = scoring.resolve_exit_fees(broker, ticker, asset)
            for key in ("time_green_roi", "time_30m_target", "time_60m_target", "ttp_arm"):
                self.assertGreaterEqual(
                    float(fees[key]),
                    floor - 1e-12,
                    msg=f"{broker}/{ticker} {key}={fees[key]} < floor {floor}",
                )
            # Posture scale-down cannot breach the floor
            fees_soft = scoring.resolve_exit_fees(
                broker, ticker, asset, exit_roi_scale=0.5, ttp_arm_scale=0.5
            )
            for key in ("time_green_roi", "time_30m_target", "time_60m_target", "ttp_arm"):
                self.assertGreaterEqual(
                    float(fees_soft[key]),
                    floor - 1e-12,
                    msg=f"scaled {broker}/{ticker} {key} breached floor",
                )

    def test_time_green_rejects_thin_profit_over_fees(self):
        """0.2% over estimated RT fees must NOT trigger a time-green sell."""
        scoring._portfolio_memory["ROBINHOOD"] = {
            "AAPL": {
                "highest": 100.2,
                "buy_time": time.time() - 60 * 60,
                "last_eval": time.time(),
            }
        }
        rt = scoring.estimate_round_trip_fee_pct("ROBINHOOD", "AAPL", "stock")
        thin = 1.0 + rt + 0.002  # only +20 bps over fees
        with mock.patch("scoring.fetch_current_price", return_value=100.0 * thin):
            with mock.patch("scoring._atr_pct", return_value=None):
                action = scoring.evaluate_holding(
                    "AAPL",
                    avg_cost=100.0,
                    broker_id="ROBINHOOD",
                    asset_type="stock",
                    live_price=100.0 * thin,
                )
        self.assertTrue(str(action).startswith("HOLD"), msg=action)
        self.assertNotIn("Time-Green", action)
        self.assertNotIn("Time-Stop", action)

    def test_equity_does_not_jump_ship_at_fee_floor_green(self):
        """~+1.2% after an hour must HOLD / trail — not flat Time-Green / Time-Stop."""
        scoring._portfolio_memory["ROBINHOOD"] = {
            "AAPL": {
                "highest": 101.2,
                "buy_time": time.time() - 60 * 60,
                "last_eval": time.time(),
            }
        }
        with mock.patch("scoring.save_state"):
            with mock.patch("scoring._atr_pct", return_value=None):
                action = scoring.evaluate_holding(
                    "AAPL",
                    avg_cost=100.0,
                    broker_id="ROBINHOOD",
                    asset_type="stock",
                    live_price=101.2,
                )
        self.assertNotIn("Time-Green", action)
        self.assertNotIn("Time-Stop", action)
        # Either still building toward arm, or TTP-armed holding for a larger peak
        self.assertTrue(
            action.startswith("HOLD"),
            msg=action,
        )

    def test_ttp_trail_preferred_over_flat_one_pct(self):
        """Once peaked well above arm, pullback sells via TTP — not a tiny flat TP."""
        scoring._portfolio_memory["ROBINHOOD"] = {
            "AAPL": {
                "highest": 104.0,  # +4% peak
                "buy_time": time.time() - 20 * 60,
                "last_eval": time.time(),
            }
        }
        # 1.0% trail from 104 → trigger at 102.96; price 102.8 should TTP-sell
        with mock.patch("scoring.save_state"):
            with mock.patch("scoring._atr_pct", return_value=None):
                action = scoring.evaluate_holding(
                    "AAPL",
                    avg_cost=100.0,
                    broker_id="ROBINHOOD",
                    asset_type="stock",
                    live_price=102.8,
                )
        self.assertIn("TTP Triggered", action)
        self.assertNotIn("Time-Green", action)
        self.assertNotIn("Time-Stop", action)

    def test_flat_banks_stay_above_ttp_arm(self):
        """Time banks cannot sit at/under arm after resolve (no pre-arm jump-ship)."""
        for broker, ticker, asset in (
            ("ROBINHOOD", "AAPL", "stock"),
            ("ROBINHOOD", "BTC", "cryptocurrency"),
            ("COINBASE", "ETH", "cryptocurrency"),
            ("ETRADE", "SPY", "stock"),
        ):
            fees = scoring.resolve_exit_fees(broker, ticker, asset)
            arm = float(fees["ttp_arm"])
            for key in ("time_green_roi", "time_30m_target", "time_60m_target"):
                self.assertGreaterEqual(
                    float(fees[key]),
                    arm * scoring.FLAT_TIME_BANK_ARM_MULT - 1e-12,
                    msg=f"{broker}/{ticker} {key}",
                )
        # Safer/Balanced disable flat banks; Aggressive keeps the escape hatch
        self.assertFalse(
            scoring.RISK_POSTURE_PROFILES["safer"]["allow_flat_time_banks"]
        )
        self.assertFalse(
            scoring.RISK_POSTURE_PROFILES["balanced"]["allow_flat_time_banks"]
        )
        self.assertTrue(
            scoring.RISK_POSTURE_PROFILES["aggressive"]["allow_flat_time_banks"]
        )

    def test_still_climbing_suppresses_flat_time_green(self):
        """Near local high: no Time-Green even when Aggressive flat banks are enabled."""
        # Artificial rails: green below arm so flat path is reachable before TTP.
        low_arm_fees = {
            "ttp_arm": 0.050,
            "ttp_trail": 0.010,
            "hard_stop": -0.035,
            "time_30m_target": 0.040,
            "time_60m_target": 0.035,
            "time_green_min": 30,
            "time_green_roi": 0.020,
            "stale_minutes": 180,
            "stale_roi": -0.015,
        }
        scoring._portfolio_memory["ROBINHOOD"] = {
            "AAPL": {
                "highest": 102.0,  # at highs
                "buy_time": time.time() - 90 * 60,
                "last_eval": time.time(),
            }
        }
        with mock.patch("scoring.save_state"):
            with mock.patch("scoring.resolve_exit_fees", return_value=dict(low_arm_fees)):
                with mock.patch(
                    "scoring._get_trend_data",
                    return_value=(True, True, 55.0, True),
                ):
                    action = scoring.evaluate_holding(
                        "AAPL",
                        avg_cost=100.0,
                        broker_id="ROBINHOOD",
                        asset_type="stock",
                        live_price=102.0,
                        allow_flat_time_banks=True,
                    )
        self.assertTrue(action.startswith("HOLD"), msg=action)
        self.assertNotIn("Time-Green", action)
        self.assertNotIn("Time-Stop", action)

    def test_safer_default_no_flat_green_on_long_hold(self):
        """Safer/Balanced default: long green hold never flat-banks — ride toward TTP."""
        scoring._portfolio_memory["ROBINHOOD"] = {
            "AAPL": {
                "highest": 101.5,  # below +2% arm
                "buy_time": time.time() - 120 * 60,
                "last_eval": time.time(),
            }
        }
        with mock.patch("scoring.save_state"):
            with mock.patch("scoring._atr_pct", return_value=None):
                action = scoring.evaluate_holding(
                    "AAPL",
                    avg_cost=100.0,
                    broker_id="ROBINHOOD",
                    asset_type="stock",
                    live_price=101.5,
                    allow_flat_time_banks=False,
                )
        self.assertTrue(action.startswith("HOLD"), msg=action)
        self.assertNotIn("Time-Green", action)
        self.assertNotIn("Time-Stop", action)

    def test_flat_time_green_after_local_turn_when_allowed(self):
        """Aggressive escape: after peak pullback (turn), high-bar Time-Green may fire."""
        low_arm_fees = {
            "ttp_arm": 0.050,
            "ttp_trail": 0.010,
            "hard_stop": -0.035,
            "time_30m_target": 0.040,
            "time_60m_target": 0.035,
            "time_green_min": 30,
            "time_green_roi": 0.020,
            "stale_minutes": 180,
            "stale_roi": -0.015,
        }
        # Peak +4%, now +2.1% — off the high by > trail, EMA not up
        scoring._portfolio_memory["ROBINHOOD"] = {
            "AAPL": {
                "highest": 104.0,
                "buy_time": time.time() - 90 * 60,
                "last_eval": time.time(),
            }
        }
        with mock.patch("scoring.save_state"):
            with mock.patch("scoring.resolve_exit_fees", return_value=dict(low_arm_fees)):
                with mock.patch(
                    "scoring._get_trend_data",
                    return_value=(False, False, 45.0, True),
                ):
                    action = scoring.evaluate_holding(
                        "AAPL",
                        avg_cost=100.0,
                        broker_id="ROBINHOOD",
                        asset_type="stock",
                        live_price=102.1,
                        allow_flat_time_banks=True,
                    )
        self.assertIn("Time-Green", action)

    def test_crypto_also_rides_near_peak(self):
        """Crypto: still climbing near peak → HOLD, not flat Time-Green."""
        low_arm_fees = {
            "ttp_arm": 0.060,
            "ttp_trail": 0.012,
            "hard_stop": -0.040,
            "time_30m_target": 0.055,
            "time_60m_target": 0.050,
            "time_green_min": 30,
            "time_green_roi": 0.030,
            "stale_minutes": 120,
            "stale_roi": -0.012,
        }
        scoring._portfolio_memory["ROBINHOOD"] = {
            "BTC": {
                "highest": 103.0,
                "buy_time": time.time() - 100 * 60,
                "last_eval": time.time(),
            }
        }
        with mock.patch("scoring.save_state"):
            with mock.patch("scoring.resolve_exit_fees", return_value=dict(low_arm_fees)):
                action = scoring.evaluate_holding(
                    "BTC",
                    avg_cost=100.0,
                    broker_id="ROBINHOOD",
                    asset_type="cryptocurrency",
                    live_price=103.0,
                    allow_flat_time_banks=True,
                )
        self.assertTrue(action.startswith("HOLD"), msg=action)
        self.assertNotIn("Time-Green", action)

    def test_fee_gate_rejects_small_edge(self):
        scoring._rotate_day_counts.clear()
        scoring._portfolio_memory["ROBINHOOD"] = {
            "AAPL": {
                "highest": 100.0,
                "buy_time": time.time() - 200 * 60,
                "last_eval": time.time(),
            }
        }
        scoring._portfolio_memory["COINBASE"] = {
            "ETH": {
                "highest": 2000.0,
                "buy_time": time.time() - 120 * 60,
                "last_eval": time.time(),
            }
        }
        # Hold score 50, cand 58 → delta 8 = aggressive min gap; edge 1.2%
        # CB RT 2.4% * 1.25 = 3.0% + MIN 1.0% = 4.0% → reject (edge 1.2% << need)
        fund = scoring.pick_rotation_funding(
            "SOL",
            58,
            True,
            [
                {
                    "ticker": "ETH",
                    "price": 2000.0,
                    "avg_cost": 2000.0,
                    "value": 400,
                    "is_crypto": True,
                    "shares": 0.2,
                }
            ],
            posture="aggressive",
            broker_id="COINBASE",
            score_fn=lambda t, is_crypto=False: 50.0,
            skip_regime_check=True,
        )
        self.assertIsNone(fund)
        self.assertIn("eligible", scoring.last_rotation_reject_reason().lower() or "eligible")

    def test_rotate_rejects_edge_only_20bps_over_fees(self):
        """Regression: edge = recycle_fee + 0.2% must not rotate (need +1.0%)."""
        scoring._rotate_day_counts.clear()
        scoring._portfolio_memory["COINBASE"] = {
            "ETH": {
                "highest": 2000.0,
                "buy_time": time.time() - 120 * 60,
                "last_eval": time.time(),
            }
        }
        # Force score_to_edge so edge lands at recycle_fee + 0.2%
        params = scoring.opportunity_swap_params("aggressive")
        rt = scoring.estimate_round_trip_fee_pct("COINBASE", "ETH", "cryptocurrency")
        recycle = rt * 1.25
        need_thin = recycle + 0.002
        # gap_pts * edge_per_pt = need_thin → with gap=8, set edge_per_pt = need_thin/8
        thin_per_pt = need_thin / 8.0
        with mock.patch.object(
            scoring,
            "opportunity_swap_params",
            return_value={**params, "score_to_edge_pct": thin_per_pt},
        ):
            fund = scoring.pick_rotation_funding(
                "SOL",
                58,
                True,
                [
                    {
                        "ticker": "ETH",
                        "price": 2000.0,
                        "avg_cost": 2000.0,
                        "value": 400,
                        "is_crypto": True,
                        "shares": 0.2,
                    }
                ],
                posture="aggressive",
                broker_id="COINBASE",
                score_fn=lambda t, is_crypto=False: 50.0,
                skip_regime_check=True,
            )
        self.assertIsNone(fund)

    def test_rh_crypto_one_way_matches_small_account_tier(self):
        # Andrew screenshot: <$50K smart exchange = 0.95%; MM rebate also ~0.95%/$100
        self.assertAlmostEqual(
            scoring._FEE_ONE_WAY_PCT["ROBINHOOD_CRYPTO"], 0.0095, places=4
        )
        self.assertAlmostEqual(
            scoring.min_profit_exit_roi_pct("ROBINHOOD", "BTC", "cryptocurrency"),
            0.019 + scoring.MIN_PROFIT_OVER_FEES_PCT,
            places=4,
        )

    def test_cb_intro1_taker_one_way_and_exit_floor(self):
        # Andrew screenshot: Intro 1 spot taker 1.20% (30d vol ~$267) — not Advanced 0.60%
        self.assertAlmostEqual(scoring.MIN_PROFIT_OVER_FEES_PCT, 0.01)
        self.assertAlmostEqual(
            scoring._FEE_ONE_WAY_PCT["COINBASE"], 0.0120, places=4
        )
        self.assertAlmostEqual(
            scoring.estimate_round_trip_fee_pct("COINBASE", "ETH", "cryptocurrency"),
            0.024,
            places=4,
        )
        self.assertAlmostEqual(
            scoring.min_profit_exit_roi_pct("COINBASE", "ETH", "cryptocurrency"),
            0.024 + scoring.MIN_PROFIT_OVER_FEES_PCT,  # 3.4%
            places=4,
        )
        fees = scoring.resolve_exit_fees("COINBASE", "ETH", "cryptocurrency")
        for key in ("ttp_arm", "time_green_roi", "time_30m_target", "time_60m_target"):
            self.assertGreaterEqual(float(fees[key]), 0.034 - 1e-12, msg=key)

    def test_etrade_equity_one_way_and_exit_floor(self):
        # Andrew E*TRADE schedule: $0 commission stocks/ETFs; Est. keeps 10 bps friction.
        # No ETRADE_CRYPTO — autotrader is equity/ETF-only (schedule crypto 0.50% unused).
        from etrade_broker import ETradeAdapter

        et = ETradeAdapter()
        self.assertTrue(et.supports_equities)
        self.assertFalse(et.supports_crypto)
        self.assertNotIn("ETRADE_CRYPTO", scoring.FEE_PROFILES)
        self.assertNotIn("ETRADE_CRYPTO", scoring._FEE_ONE_WAY_PCT)
        self.assertAlmostEqual(
            scoring._FEE_ONE_WAY_PCT["ETRADE_STOCK"], 0.0010, places=4
        )
        self.assertAlmostEqual(
            scoring.estimate_round_trip_fee_pct("ETRADE", "SPY", "stock"),
            0.0020,
            places=4,
        )
        self.assertAlmostEqual(
            scoring.estimate_round_trip_fee_pct("E*TRADE", "MSFT", "Equity"),
            0.0020,
            places=4,
        )
        floor = 0.0020 + scoring.MIN_PROFIT_OVER_FEES_PCT  # 1.2%
        self.assertAlmostEqual(
            scoring.min_profit_exit_roi_pct("ETRADE", "SPY", "stock"),
            floor,
            places=4,
        )
        fees = scoring.resolve_exit_fees("ETRADE", "SPY", "stock")
        for key in ("ttp_arm", "time_green_roi", "time_30m_target", "time_60m_target"):
            self.assertGreaterEqual(float(fees[key]), floor - 1e-12, msg=key)
        # Even if asset_type looks crypto, ET still maps to ETRADE_STOCK rails
        self.assertEqual(
            scoring.fee_profile_key("ETRADE", "BTC", "cryptocurrency"),
            "ETRADE_STOCK",
        )

    def test_resolve_journal_fee_key_legacy_robinhood_crypto(self):
        self.assertEqual(
            scoring.resolve_journal_fee_key("ROBINHOOD", "Robinhood", "ETH", "Crypto"),
            "ROBINHOOD_CRYPTO",
        )
        self.assertEqual(
            scoring.resolve_journal_fee_key("ROBINHOOD", "Robinhood", "AAPL", "Stock"),
            "ROBINHOOD_STOCK",
        )
        self.assertEqual(
            scoring.resolve_journal_fee_key("COINBASE", "Coinbase", "SOL", ""),
            "COINBASE",
        )

    def test_effective_min_dollars_small_book_crypto(self):
        self.assertAlmostEqual(
            scoring.effective_min_dollars("ROBINHOOD", 207.0, True, 5.0),
            scoring.SMALL_BOOK_CRYPTO_MIN_DOLLARS,
        )
        self.assertAlmostEqual(
            scoring.effective_min_dollars("ROBINHOOD", 800.0, True, 5.0),
            5.0,
        )
        self.assertAlmostEqual(
            scoring.effective_min_dollars("ROBINHOOD", 207.0, False, 5.0),
            5.0,
        )

    def test_daily_rotate_cap(self):
        scoring._rotate_day_counts.clear()
        key = scoring._rotate_day_key("ROBINHOOD")
        scoring._rotate_day_counts[key] = 3
        ok, why = scoring.rotation_allowed_today("ROBINHOOD", posture="aggressive")
        self.assertFalse(ok)
        self.assertIn("cap", why)

    def test_summarize_fills_pairs_pnl(self):
        rows = [
            {
                "timestamp": "2026-08-05T10:00:00",
                "broker": "Robinhood",
                "side": "BUY",
                "ticker": "AAPL",
                "price": 100.0,
                "qty": 2,
                "dollars": 200.0,
                "status": "Filled",
                "confirmed": True,
                "fee_est": 0.2,
                "reason": "",
            },
            {
                "timestamp": "2026-08-05T11:00:00",
                "broker": "Robinhood",
                "side": "SELL",
                "ticker": "AAPL",
                "price": 110.0,
                "qty": 2,
                "dollars": 220.0,
                "status": "Filled",
                "confirmed": True,
                "fee_est": 0.2,
                "reason": "ROTATE",
            },
        ]
        s = analytics.summarize_fills(rows)
        self.assertEqual(s["buys"], 1)
        self.assertEqual(s["sells"], 1)
        self.assertEqual(s["rotates"], 1)
        self.assertAlmostEqual(s["realized_pnl"], 20.0, places=2)
        self.assertAlmostEqual(s["fee_est"], 0.4, places=2)

    def test_fee_confidence_profile_is_low(self):
        rows = [
            {
                "timestamp": "2026-08-05T10:00:00",
                "broker": "Robinhood",
                "side": "BUY",
                "ticker": "AAPL",
                "price": 100.0,
                "qty": 1,
                "dollars": 100.0,
                "status": "Filled",
                "confirmed": True,
                "fee_est": 0.1,
            }
        ]
        conf = analytics.summarize_fee_confidence(rows)
        self.assertEqual(conf["level"], "low")
        self.assertIn("Est", conf["label"])
        self.assertEqual(conf["broker_fee_n"], 0)
        self.assertEqual(conf["estimate_n"], 1)

    def test_fee_confidence_broker_field_is_high(self):
        rows = [
            {
                "timestamp": "2026-08-05T10:00:00",
                "broker": "Robinhood",
                "side": "BUY",
                "ticker": "AAPL",
                "price": 100.0,
                "qty": 1,
                "dollars": 100.0,
                "status": "Filled",
                "confirmed": True,
                "fee_paid": 0.05,
            },
            {
                "timestamp": "2026-08-05T11:00:00",
                "broker": "Robinhood",
                "side": "SELL",
                "ticker": "AAPL",
                "price": 101.0,
                "qty": 1,
                "dollars": 101.0,
                "status": "Filled",
                "confirmed": True,
                "commission": 0.05,
            },
        ]
        conf = analytics.summarize_fee_confidence(rows)
        self.assertEqual(conf["level"], "high")
        self.assertEqual(conf["broker_fee_n"], 2)

    def test_extract_fee_from_order_payloads(self):
        self.assertAlmostEqual(
            analytics.extract_fee_dollars_from_order({"fees": "0.02"}), 0.02, places=4
        )
        self.assertAlmostEqual(
            analytics.extract_fee_dollars_from_order({"total_fees": 1.25}), 1.25, places=4
        )
        self.assertAlmostEqual(
            analytics.extract_fee_dollars_from_order({"estimatedCommission": 0.0}), 0.0, places=4
        )
        self.assertIsNone(analytics.extract_fee_dollars_from_order({"state": "filled"}))
        nested = {"order": {"total_fees": {"value": "0.50"}}}
        self.assertAlmostEqual(
            analytics.extract_fee_dollars_from_order(nested), 0.50, places=4
        )

    def test_summarize_fills_prefers_broker_fee(self):
        rows = [
            {
                "timestamp": "2026-08-05T10:00:00",
                "broker": "Robinhood",
                "side": "BUY",
                "ticker": "AAPL",
                "price": 100.0,
                "qty": 1,
                "dollars": 100.0,
                "status": "Filled",
                "confirmed": True,
                "fee_est": 9.99,
                "fee_paid": 0.03,
            }
        ]
        s = analytics.summarize_fills(rows)
        self.assertAlmostEqual(s["fee_est"], 0.03, places=4)

    def test_fee_drag_coach_small_account(self):
        tip = analytics.fee_drag_coach(
            {"turnover": 400.0, "fee_drag_pct": 1.2},
            small_turnover=5000.0,
            high_drag_pct=0.45,
        )
        self.assertIsNotNone(tip)
        self.assertIn("fee drag", tip.lower())
        self.assertIsNone(
            analytics.fee_drag_coach(
                {"turnover": 50_000.0, "fee_drag_pct": 0.1},
                small_turnover=5000.0,
                high_drag_pct=0.45,
            )
        )

    def test_reports_hero_leads_with_pnl(self):
        text = analytics.format_reports_hero(
            {
                "realized_pnl": 12.5,
                "fee_est": 1.0,
                "fee_drag_pct": 0.5,
                "net_after_fees": 11.5,
                "buys": 2,
                "sells": 1,
                "rotates": 0,
                "win_rate": 1.0,
                "avg_hold_min": 30,
                "turnover": 200,
            },
            window_label="All time",
        )
        self.assertTrue(text.startswith("Net≈"))
        self.assertIn("All time", text)
        self.assertIn("3 trades", text)
        self.assertIn("Realized", text)
        self.assertIn("Buys 2", text)


if __name__ == "__main__":
    unittest.main()
