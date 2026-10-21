# Optional n8n automation for Crypto Signal Bot

This repository's Python process owns the runtime `signals.json` file. An n8n instance cannot read that file just because it exists in the bot's working directory. Connect the systems deliberately; do not commit the live log or credentials to Git.

## Recommended first automation

Use n8n for **notification and operations**, not as a second trading engine:

1. The bot host runs `python analyze_signals.py --input signals.json --out outcome-report.json` where the real runtime log is available.
2. A small, authenticated integration sends only a minimal summary to an n8n webhook: generation timestamp, record counts, win/loss counts, net realized PnL, and anomaly count. Avoid sending full signal records or user metadata unless necessary.
3. n8n validates the payload and sends an operational Telegram message using an n8n-managed Telegram credential.
4. Send a separate failure notification if report generation fails; do not report a missing log as zero trades.
5. Keep the webhook private where possible. Require authentication, rotate credentials, rate-limit requests, and avoid putting tokens in workflow JSON or source control.

## Safe rollout

- Start with a manual test payload and a Telegram test chat.
- Verify missing, malformed, stale, and duplicate payload behavior.
- Verify that credentials are stored in n8n's credential store, not in workflow exports.
- Keep the automation read-only: it must not place orders, change bot settings, or reset signal history.
- Enable a schedule only after the bot host and its persistent log location are confirmed.
- Keep an audit trail of the report timestamp and delivery result, but avoid storing full private payloads in execution history.

## Why this repository does not import a large workflow collection

The upstream [n8n workflow collection](https://github.com/Zie619/n8n-workflows) is a useful discovery catalogue, not a ready-made integration for this bot. Its workflows can depend on different credentials, services, node versions, and assumptions. Import only a specific workflow after reviewing every node, credential, trigger, and outbound destination.

## Manual report generation

```bash
python analyze_signals.py --input /path/to/signals.json --out outcome-report.json
```

The report describes recorded outcomes; it is not an independent historical-candle backtest and does not establish profitability.
