# Bot Operations Skill

## Monitoring
- Record run start/end, scanned symbol count, exchange failures, rate-limit events, emitted signals, and Telegram delivery counts.
- Alert on repeated scanner failures, unavailable exchanges, missing market data, and Telegram delivery failure.
- Distinguish a successful GitHub Actions job from a successful market scan or successful Telegram delivery.
- Do not log secrets, full authorization headers, or tokens.

## State safety
- Preserve signal history except when an explicit, versioned reset is requested.
- Keep state writes atomic and validate JSON before replacing persistent state.
- Avoid concurrent workflows writing the same state file without serialization.
- Report active signal count separately from all historical records.
- A signal log analysis is not an independent historical-candle backtest.

## Incident response
1. Capture the failing run ID and relevant sanitized logs.
2. Identify whether the failure is data, exchange, state, code, or Telegram delivery.
3. Reproduce with a test where possible.
4. Make the smallest change that addresses the evidence.
5. Verify the workflow and report any runtime checks that remain unverified.
