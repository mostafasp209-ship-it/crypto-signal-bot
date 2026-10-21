# Per-signal strategy diagnostics

Run this against a runtime copy of `signals.json` (do not commit the live file):

```bash
python analyze_signals.py --input signals.json --out outcome-report.json
python -c "import json; from strategy_diagnostics import diagnose_signals; d=json.load(open('signals.json')); print(json.dumps(diagnose_signals(d.get('signals', d)), indent=2))"
```

New signals record an `entry_snapshot` containing the entry-time regime, trend, setup, RSI, ATR, EMA extension, relative volume, support retests, resistance room in R, stop distance, reward/risk, and target/stop methods. This snapshot is stored with the signal so the later diagnosis does not accidentally use post-entry indicator values.

The diagnostic report flags evidence-linked hypotheses (e.g. long against daily downtrend, limited resistance room, extended entry, weak volume, repeatedly tested support). It does **not** claim causation. Compare repeated patterns with winners and other setups before changing rules. Old signals have no snapshot and are explicitly marked as missing evidence; they cannot be reconstructed reliably from current indicators alone.
