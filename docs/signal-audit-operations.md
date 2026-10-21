# Signal outcome audit operations

The bot writes its signal history to the runtime file `signals.json` (see `DATA_FILE` in `bot.py`). GitHub Actions checks out repository files; it does **not** have access to the bot host's local filesystem. Therefore the audit workflow is manual and may report a skip when the runtime log is not present in the checkout.

## Run an audit where the runtime log exists

```bash
python analyze_signals.py --input /path/to/signals.json --out outcome-report.json
```

The input log is read only. Keep `signals.json` on the bot host unless you have deliberately reviewed the privacy and retention implications of exporting it. Do not commit live user data, Telegram IDs, credentials, or runtime state just to make CI pass.

## What the report means

- It summarizes recorded closed outcomes, PnL, score/setup groups, and consistency anomalies.
- It is not an independent historical-candle backtest and does not establish causation or profitability.
- Small groups are noisy. Do not tune live thresholds from this report alone.
- If the workflow skips, it means the runtime log was not supplied to the GitHub checkout; it does not mean there were no signals.
