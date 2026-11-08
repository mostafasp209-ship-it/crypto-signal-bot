import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
import bot
import strategy

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
    return (patch("strategy.rsi",return_value=pd.Series(rv)),
            patch("strategy.atr",return_value=pd.Series(av)),
            patch("strategy.stochastic",return_value=(pd.Series(k),pd.Series(d))))

class FuturesStrategyTests(unittest.TestCase):
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
            target_fill = sig["tp1"] * (1-bot.SLIPPAGE_RATE) if side == "BUY" else sig["tp1"] * (1+bot.SLIPPAGE_RATE)
            expected_net_rr = (abs(target_fill-sig["entry"]) - (sig["entry"]+sig["tp1"])*bot.FEE_RATE) / abs(sig["entry"]-sig["sl"])
            self.assertAlmostEqual(sig["net_rr"], expected_net_rr)
            self.assertGreaterEqual(sig["net_rr"], bot.MIN_RR)
            if side=="BUY":
                self.assertGreater(sig["entry"],100)
                self.assertAlmostEqual(sig["tp1"],sig["entry"]+1.5*(sig["entry"]-sig["sl"]))
            else:
                self.assertLess(sig["entry"],100)
                self.assertAlmostEqual(sig["tp1"],sig["entry"]-1.5*(sig["sl"]-sig["entry"]))

    def test_initial_stop_risk_above_two_point_five_percent_is_rejected(self):
        data = make_frame("BUY")
        data.loc[data.index[-5], "low"] = 95.0
        p = patch_indicators(len(data), "BUY")
        with p[0], p[1], p[2]:
            self.assertIsNone(bot.build_signal("BTC/USDT:USDT", "binanceusdm", data, next_open=100.0))

    def test_entry_price_below_safety_floor_is_rejected(self):
        data = make_frame("BUY")
        p = patch_indicators(len(data), "BUY")
        with p[0], p[1], p[2]:
            self.assertIsNone(bot.build_signal("BTC/USDT:USDT", "bitget", data, next_open=0.005))

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

if __name__=="__main__":
    unittest.main(verbosity=2)
