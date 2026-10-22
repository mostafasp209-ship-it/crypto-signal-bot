---
name: data-flow-investigation
description: Trace a signal or runtime defect through the crypto signal bot using verifiable evidence before proposing changes.
---

# Data-flow investigation

Use this skill when a signal looks incorrect, exchange data is inconsistent, or a runtime failure is difficult to reproduce.

## Procedure

1. State the observable symptom and exact question before guessing a cause.
2. Trace the relevant path through exchange fetch, candle normalization, indicators, scoring, risk checks, persistence, and notification.
3. Prefer repository code/tests, redacted runtime examples, and official API documentation. Label observed facts separately from hypotheses.
4. Check symbol formats, quote currency, timeframe, timestamps/timezones, candle closure, missing values, and exchange-specific response shapes.
5. Test boundary cases: empty/stale responses, malformed numbers, duplicate symbols, rate limits, and timezone boundaries.
6. Reproduce the smallest case with deterministic fixtures; add a regression test before changing behavior when practical.
7. Make the smallest safe fix. Do not suppress errors or reset production state to hide a symptom.
8. Run affected tests and the full regression suite; state what remains unverified.

## Trading safety

- Never use future candles or look-ahead data.
- Do not infer profitability from code inspection or the live outcome log alone.
- Do not change entry thresholds, sizing, or live risk limits as a debugging shortcut.
- Do not share private runtime data with external tools unnecessarily.
- Inspect only systems and API behavior the operator is authorized to inspect. Do not bypass access controls; prefer public API contracts and observable behavior.

## Required investigation note

Summarize symptom, evidence and file references, reproduction status, root cause or open hypotheses, changes, tests, and residual risk.
