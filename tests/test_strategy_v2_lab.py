import unittest
import numpy as np
import pandas as pd
from research.strategy_v2_lab import add_features, generate_candidates, simulate_one, simulate_candidate, metrics, splits

def bars(n=700,step=0.2):
    i=np.arange(n); c=100+i*step+np.sin(i/5); o=c-0.1
    return pd.DataFrame({"timestamp":i.astype("int64")*14_400_000,"open":o,
      "high":np.maximum(o,c)+0.5,"low":np.minimum(o,c)-0.5,"close":c,"volume":np.full(n,1000.)})
def daily(n=400):
    i=np.arange(n); c=80+i*.8
    return pd.DataFrame({"timestamp":i.astype("int64")*86_400_000,"open":c-.2,
      "high":c+.5,"low":c-.5,"close":c,"volume":np.full(n,10000.)})

class StrategyV2Tests(unittest.TestCase):
    def test_daily_context_uses_last_closed_daily_bar(self):
        x=add_features(bars(),daily())
        self.assertTrue(np.isnan(x.loc[0,"daily_close"]))
        row=x.loc[x.timestamp==5*14_400_000].iloc[0]
        self.assertEqual(row.daily_close,daily().loc[0,"close"])
    def test_features_are_causal(self):
        src=bars(); d=daily(); a=add_features(src,d); changed=src.copy()
        changed.loc[500:,["open","high","low","close","volume"]]*=1.7
        b=add_features(changed,d)
        cols=["ema20","ema50","ema200","atr","rsi","donchian_high20","donchian_low20"]
        pd.testing.assert_frame_equal(a.loc[:499,cols],b.loc[:499,cols],check_dtype=False,check_exact=False,rtol=1e-10,atol=1e-10)
    def test_candidate_outputs_are_boolean_and_complete(self):
        x=add_features(bars(),daily()); c=generate_candidates(x)
        self.assertEqual(set(c),{"current_rsi_stoch_baseline","trend_breakout_20","trend_pullback_ema20","trend_macd_cross","range_mean_reversion"})
        for v in c.values():
            for side in ("BUY","SELL"): self.assertEqual((v[side].dtype,len(v[side])),(np.dtype(bool),len(x)))
    def test_current_baseline_reproduces_long_and_short_rsi_stoch_triggers(self):
        x=add_features(bars(),daily())
        i=len(x)-1
        x.loc[i-1,["rsi","stoch_k","stoch_d"]]=[29.,20.,22.]
        x.loc[i,["rsi","stoch_k","stoch_d","volume_ma20","volume","open"]]=[
            31.,25.,24.,1000.,1500.,float(x.loc[i,"close"])-0.1]
        sig=generate_candidates(x)["current_rsi_stoch_baseline"]
        self.assertTrue(sig["BUY"][i])
        self.assertFalse(sig["SELL"][i])

        x.loc[i-1,["rsi","stoch_k","stoch_d"]]=[71.,80.,78.]
        x.loc[i,["rsi","stoch_k","stoch_d","volume_ma20","volume","open"]]=[
            69.,75.,76.,1000.,1500.,float(x.loc[i,"close"])+0.1]
        sig=generate_candidates(x)["current_rsi_stoch_baseline"]
        self.assertTrue(sig["SELL"][i])
        self.assertFalse(sig["BUY"][i])

    def test_current_baseline_uses_132_atr_and_15r_exit(self):
        x=add_features(bars(),daily())
        i=300
        trade=simulate_one(x,i,"BUY",len(x),family="current_rsi_stoch_baseline")
        self.assertIsNotNone(trade)
        self.assertAlmostEqual(trade["target"]-trade["entry"],
            1.5*(trade["entry"]-trade["stop"]),places=7)

    def test_entry_uses_next_bar_open(self):
        x=add_features(bars(),daily()); i=300; t=simulate_one(x,i,"BUY",len(x))
        self.assertIsNotNone(t); self.assertEqual(t["entry_time"],int(x.iloc[i+1].timestamp))
        self.assertNotEqual(t["entry"],float(x.iloc[i].close))
    def test_stop_first_when_stop_and_target_hit_same_bar(self):
        x=add_features(bars(),daily()); i=300
        x.loc[i+1,"open"]=100.; x.loc[i+1,"high"]=1000.; x.loc[i+1,"low"]=-1000.
        self.assertEqual(simulate_one(x,i,"BUY",len(x))["reason"],"STOP")
    def test_short_entry_has_adverse_slippage(self):
        x=add_features(bars(step=-.05),daily()); i=300; t=simulate_one(x,i,"SELL",len(x))
        self.assertIsNotNone(t); self.assertLess(t["entry"],float(x.iloc[i+1].open))
    def test_no_overlapping_positions(self):
        x=add_features(bars(),daily()); sig={"BUY":np.zeros(len(x),bool),"SELL":np.zeros(len(x),bool)}
        sig["BUY"][[250,251,252,253,254]]=True; ts=simulate_candidate(x,sig,220,len(x))
        for a,b in zip(ts,ts[1:]): self.assertGreater(b["signal_index"],a["exit_index"])
    def test_metrics_include_long_short(self):
        m=metrics([{"net_r":1.,"side":"BUY"},{"net_r":-1.,"side":"SELL"},{"net_r":.5,"side":"BUY"}])
        self.assertEqual(m["trades"],3); self.assertAlmostEqual(m["expectancy_r"],1/6)
        self.assertEqual((m["long_trades"],m["short_trades"]),(2,1))
    def test_chronological_splits(self):
        self.assertEqual(splits(1000),{"train":(220,600),"validation":(600,800),"holdout":(800,1000)})

if __name__=="__main__": unittest.main(verbosity=2)
