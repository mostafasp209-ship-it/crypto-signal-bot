import unittest
import numpy as np
import pandas as pd
from research.rtm_ichimoku_futures_v1 import resample_ohlcv, build_features, trade_sim


class MultiTimeframeTests(unittest.TestCase):
    def test_resample_uses_closed_right_edge(self):
        ts=pd.date_range("2025-01-01 00:15",periods=8,freq="15min",tz="UTC")
        d=pd.DataFrame({"timestamp":ts,"open":range(8),"high":np.arange(8)+1,"low":range(8),"close":range(8),"volume":1})
        h=resample_ohlcv(d,"1h")
        self.assertEqual(h.iloc[0].timestamp, pd.Timestamp("2025-01-01 01:00",tz="UTC"))
        self.assertEqual(h.iloc[0].close,3)

    def test_features_have_no_future_timestamp_join(self):
        n=1200; ts=pd.date_range("2025-01-01",periods=n,freq="15min",tz="UTC")
        close=100+np.sin(np.arange(n)/15)+np.arange(n)*.01
        d=pd.DataFrame({"timestamp":ts,"open":close-.1,"high":close+.4,"low":close-.4,"close":close,"volume":100+np.arange(n)%17})
        f=build_features(d)
        for c in ["1h_time","4h_time"]:
            valid=f[c].notna()
            self.assertTrue((f.loc[valid,c]<=f.loc[valid,"timestamp"]).all())

    def test_simulator_respects_stop_first_when_stop_and_target_same_bar(self):
        n=500; ts=pd.date_range("2025-01-01",periods=n,freq="15min",tz="UTC")
        close=np.full(n,100.0); high=np.full(n,100.5); low=np.full(n,99.5); op=np.full(n,100.0)
        side=np.zeros(n); side[401]=1
        high[402]=104; low[402]=96; op[402]=100; close[402]=100
        # Force ATR via a simple, internally consistent wide range.
        high[400:]=101; low[400:]=99
        high[402]=104; low[402]=96
        d=pd.DataFrame({"timestamp":ts,"open":op,"high":high,"low":low,"close":close,"volume":100.0,"atr":np.full(n,1.0),"side":side})
        trades=trade_sim(d,400,n,fee=0,slip=0)
        self.assertTrue(trades)
        self.assertEqual(trades[0]["exit_reason"],"SL")


if __name__=="__main__": unittest.main()
