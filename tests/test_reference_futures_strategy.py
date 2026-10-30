import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
import bot

SETUP = "REFERENCE_RSI_STOCH_ATR_RR15"

def make_frame(side):
    n=150
    close=np.full(n,100.0); op=np.full(n,99.8 if side=="BUY" else 100.2)
    high=np.full(n,101.0); low=np.full(n,99.0); vol=np.full(n,100.0)
    if side=="BUY":
        op[-1],close[-1],low[-1]=99.8,100.4,98.8
    else:
        op[-1],close[-1],high[-1]=100.2,99.6,101.2
    vol[-1]=200.0
    return pd.DataFrame({"timestamp":np.arange(n,dtype=np.int64)*14_400_000,
        "open":op,"high":high,"low":low,"close":close,"volume":vol})

def patch_indicators(n,side):
    rv=np.full(n,50.0); av=np.full(n,1.0); k=np.full(n,50.0); d=np.full(n,50.0)
    if side=="BUY":
        rv[-2:]=[29.0,31.0]; k[-2:]=[20.0,25.0]; d[-2:]=[22.0,24.0]
    else:
        rv[-2:]=[71.0,69.0]; k[-2:]=[80.0,75.0]; d[-2:]=[78.0,76.0]
    return (patch("bot.rsi",return_value=pd.Series(rv)),
            patch("bot.atr",return_value=pd.Series(av)),
            patch("bot.stochastic",return_value=(pd.Series(k),pd.Series(d))))

class FuturesStrategyTests(unittest.TestCase):
    def test_new_entries_never_fall_back_to_legacy_spot_venues(self):
        class Manager:
            def get_exchange(self, name):
                if name == "binanceusdm":
                    return type("Exchange", (), {"markets": {"BTC/USDT:USDT": {}}})()
                return type("Exchange", (), {"markets": {"BTC/USDT": {}}})()

        self.assertEqual(bot.candidate_exchange_names(Manager(), "binanceusdm", "BTC/USDT:USDT"),
                         ["binanceusdm"])
        self.assertEqual(bot.candidate_exchange_names(Manager(), "binanceus", "BTC/USDT:USDT"), [])
    def test_long_and_short_gates(self):
        for side,key in (("BUY","signal_long"),("SELL","signal_short")):
            data=make_frame(side); p=patch_indicators(len(data),side)
            with p[0],p[1],p[2]:
                result=bot.calculate_reference_strategy_gates(data)
            self.assertTrue(result[key])
            self.assertGreater(result["volume_ratio"],1.12)

    def test_next_open_entry_and_rr_15_for_both_directions(self):
        for side in ("BUY","SELL"):
            data=make_frame(side); p=patch_indicators(len(data),side)
            with p[0],p[1],p[2]:
                sig=bot.build_signal("BTC/USDT:USDT","binanceusdm",data,next_open=100.0)
            self.assertIsNotNone(sig); self.assertEqual(sig["side"],side)
            self.assertAlmostEqual(sig["rr"],1.5)
            if side=="BUY":
                self.assertGreater(sig["entry"],100)
                self.assertAlmostEqual(sig["tp1"],sig["entry"]+1.5*(sig["entry"]-sig["sl"]))
            else:
                self.assertLess(sig["entry"],100)
                self.assertAlmostEqual(sig["tp1"],sig["entry"]-1.5*(sig["sl"]-sig["entry"]))

    def test_rsi_recovery_requires_a_real_cross(self):
        data = make_frame("BUY")
        p = patch_indicators(len(data), "BUY")
        with p[0], p[1], p[2]:
            rv = np.full(len(data), 50.0)
            rv[-2:] = [31.0, 32.0]
            with patch("bot.rsi", return_value=pd.Series(rv)):
                result = bot.calculate_reference_strategy_gates(data)
        self.assertFalse(result["signal_long"])

    def test_stochastic_requires_a_real_cross(self):
        data = make_frame("BUY")
        p = patch_indicators(len(data), "BUY")
        with p[0], p[1], p[2]:
            k = np.full(len(data), 50.0)
            d = np.full(len(data), 50.0)
            k[-2:] = [25.0, 24.0]
            d[-2:] = [22.0, 23.0]
            with patch("bot.stochastic", return_value=(pd.Series(k), pd.Series(d))):
                result = bot.calculate_reference_strategy_gates(data)
        self.assertFalse(result["signal_long"])

    def test_volume_filter_rejects_weak_volume(self):
        data = make_frame("BUY")
        data.loc[len(data)-1, "volume"] = 100.0
        p = patch_indicators(len(data), "BUY")
        with p[0], p[1], p[2]:
            result = bot.calculate_reference_strategy_gates(data)
        self.assertFalse(result["signal_long"])

    def test_no_next_open_means_no_signal(self):
        data=make_frame("BUY"); p=patch_indicators(len(data),"BUY")
        with p[0],p[1],p[2]:
            self.assertIsNone(bot.build_signal("BTC/USDT:USDT","binanceusdm",data,next_open=None))

    def test_short_tp_closes_full_position_and_books_fees(self):
        sig={"symbol":"BTC/USDT:USDT","side":"SELL","status":"ACTIVE","entry":100.0,"sl":102.0,
            "tp1":97.0,"tp2":97.0,"tp3":97.0,"components":{"setup":SETUP,"slippage_rate":0.0002,"fee_rate":0.0005},
            "initial_sl":102.0,"initial_risk_per_unit":2.0,"position_quantity":1.0,"remaining_fraction":1.0,
            "realized_pnl":0.0,"fees_paid":0.0,"tp1_hit":False,"tp2_hit":False,"tp3_hit":False}
        self.assertTrue(bot.manage_signal(sig,97.0))
        self.assertEqual(sig["status"],"CLOSED"); self.assertEqual(sig["closed_reason"],"TP1")
        self.assertEqual(sig["remaining_fraction"],0.0)
        self.assertGreater(sig["realized_pnl"],0); self.assertGreater(sig["fees_paid"],0)

    def test_short_stop_closes_position(self):
        sig={"symbol":"BTC/USDT:USDT","side":"SELL","status":"ACTIVE","entry":100.0,"sl":102.0,
            "tp1":97.0,"tp2":97.0,"tp3":97.0,"components":{"setup":SETUP,"slippage_rate":0.0002,"fee_rate":0.0005},
            "initial_sl":102.0,"initial_risk_per_unit":2.0,"position_quantity":1.0,"remaining_fraction":1.0,
            "realized_pnl":0.0,"fees_paid":0.0,"tp1_hit":False,"tp2_hit":False,"tp3_hit":False}
        self.assertTrue(bot.manage_signal(sig,102.5))
        self.assertEqual(sig["status"],"CLOSED"); self.assertEqual(sig["closed_reason"],"SL")


    def test_closed_candle_range_detects_target_even_if_ticker_reversed(self):
        sig={"symbol":"BTC/USDT:USDT","side":"BUY","status":"ACTIVE","entry":100.0,"sl":98.0,
            "tp1":103.0,"tp2":103.0,"tp3":103.0,"components":{"setup":SETUP,"slippage_rate":0.0002,"fee_rate":0.0005},
            "initial_sl":98.0,"initial_risk_per_unit":2.0,"position_quantity":1.0,"remaining_fraction":1.0,
            "realized_pnl":0.0,"fees_paid":0.0,"tp1_hit":False,"tp2_hit":False,"tp3_hit":False}
        self.assertTrue(bot.manage_signal(sig,100.0,candle_high=103.5,candle_low=99.5))
        self.assertEqual(sig["status"],"CLOSED")
        self.assertEqual(sig["closed_reason"],"TP1")

    def test_stop_wins_when_stop_and_target_touch_same_candle(self):
        sig={"symbol":"BTC/USDT:USDT","side":"BUY","status":"ACTIVE","entry":100.0,"sl":98.0,
            "tp1":103.0,"tp2":103.0,"tp3":103.0,"components":{"setup":SETUP,"slippage_rate":0.0002,"fee_rate":0.0005},
            "initial_sl":98.0,"initial_risk_per_unit":2.0,"position_quantity":1.0,"remaining_fraction":1.0,
            "realized_pnl":0.0,"fees_paid":0.0,"tp1_hit":False,"tp2_hit":False,"tp3_hit":False}
        self.assertTrue(bot.manage_signal(sig,100.0,candle_high=104.0,candle_low=97.0))
        self.assertEqual(sig["status"],"CLOSED")
        self.assertEqual(sig["closed_reason"],"SL")


    def test_stop_gap_uses_adverse_candle_open_not_stale_stop(self):
        sig={"symbol":"BTC/USDT:USDT","side":"BUY","status":"ACTIVE","entry":100.0,"sl":98.0,
            "tp1":103.0,"tp2":103.0,"tp3":103.0,"components":{"setup":SETUP,"slippage_rate":0.0002,"fee_rate":0.0005},
            "initial_sl":98.0,"initial_risk_per_unit":2.0,"position_quantity":1.0,"remaining_fraction":1.0,
            "realized_pnl":0.0,"fees_paid":0.0,"tp1_hit":False,"tp2_hit":False,"tp3_hit":False}
        self.assertTrue(bot.manage_signal(sig,96.0,candle_open=95.0,candle_high=97.0,candle_low=94.0))
        self.assertEqual(sig["closed_reason"],"SL")
        self.assertLess(sig["last_exit_price"],95.0)


    def test_reference_short_stop_uses_candle_high_not_stale_ticker(self):
        sig={"symbol":"BTC/USDT:USDT","side":"SELL","status":"ACTIVE","entry":100.0,"sl":102.0,
            "tp1":97.0,"tp2":97.0,"tp3":97.0,"components":{"setup":SETUP,"slippage_rate":0.0002,"fee_rate":0.0005},
            "initial_sl":102.0,"initial_risk_per_unit":2.0,"position_quantity":1.0,"remaining_fraction":1.0,
            "realized_pnl":0.0,"fees_paid":0.0,"tp1_hit":False,"tp2_hit":False,"tp3_hit":False}
        self.assertTrue(bot.manage_signal(sig,100.0,candle_open=100.0,candle_high=102.5,candle_low=99.0))
        self.assertEqual(sig["status"],"CLOSED")
        self.assertEqual(sig["closed_reason"],"SL")

    def test_reference_long_target_uses_candle_high_not_stale_ticker(self):
        sig={"symbol":"BTC/USDT:USDT","side":"BUY","status":"ACTIVE","entry":100.0,"sl":98.0,
            "tp1":103.0,"tp2":103.0,"tp3":103.0,"components":{"setup":SETUP,"slippage_rate":0.0002,"fee_rate":0.0005},
            "initial_sl":98.0,"initial_risk_per_unit":2.0,"position_quantity":1.0,"remaining_fraction":1.0,
            "realized_pnl":0.0,"fees_paid":0.0,"tp1_hit":False,"tp2_hit":False,"tp3_hit":False}
        self.assertTrue(bot.manage_signal(sig,100.0,candle_open=100.0,candle_high=103.5,candle_low=99.5))
        self.assertEqual(sig["status"],"CLOSED")
        self.assertEqual(sig["closed_reason"],"TP1")


    def test_breakeven_stop_wins_after_tp1_in_same_candle(self):
        sig={"symbol":"BTC/USDT:USDT","side":"BUY","status":"ACTIVE","entry":100.0,"sl":98.0,
            "tp1":103.0,"tp2":106.0,"tp3":109.0,"components":{"setup":"LEGACY_MULTI_TARGET","slippage_rate":0.0002,"fee_rate":0.0005},
            "initial_sl":98.0,"initial_risk_per_unit":2.0,"position_quantity":1.0,"remaining_fraction":1.0,
            "realized_pnl":0.0,"fees_paid":0.0,"tp1_hit":False,"tp2_hit":False,"tp3_hit":False}
        self.assertTrue(bot.manage_signal(sig,100.0,candle_open=100.0,candle_high=104.0,candle_low=99.5))
        self.assertEqual(sig["status"],"CLOSED")
        self.assertEqual(sig["closed_reason"],"SL")
        self.assertEqual(sig["remaining_fraction"],0.0)
        self.assertTrue(sig["tp1_hit"])

    def test_signal_formatter_is_persian_and_strategy_specific(self):
        sig={"symbol":"BTC/USDT:USDT","side":"BUY","exchange":"binanceusdm","entry":100.0,"sl":98.0,
            "tp1":103.0,"rr":1.5,"risk_pct":2.0,"actual_risk_usd":10.0,"risk_target_usd":10.0,
            "position_quantity":1.0,"components":{"setup":SETUP,"rsi":31.0,"stoch_k":25.0,"stoch_d":24.0,
            "volume_ratio":1.4,"atr14":1.0},"fundamentals":{},"news":{"headlines":[]}}
        msg=bot.format_signal(sig)
        self.assertIn("سیگنال جدید خرید",msg)
        self.assertIn("بازگشت RSI + کراس استوکاستیک",msg)
        self.assertIn("1.50R",msg)
        self.assertIn("سفارش واقعی ثبت نمی‌کند",msg)

if __name__=="__main__":
    unittest.main(verbosity=2)
