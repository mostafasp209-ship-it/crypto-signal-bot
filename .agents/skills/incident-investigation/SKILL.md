# Incident Investigation Skill

## Rule
No speculative fixes before a root-cause investigation. Follow the evidence through the smallest reproducible failure.

## Procedure
1. State the observed symptom and expected behavior.
2. Capture the failing workflow/run ID, timestamp, and sanitized logs.
3. Classify the likely boundary: scheduler, exchange/API, candle data, strategy logic, state file, Telegram delivery, or deployment.
4. Trace the path from input to output and verify each assumption against code or logs.
5. Create at least two plausible hypotheses when the cause is unclear; test the cheapest discriminating check first.
6. Reproduce with a focused unit test or controlled dry run where possible.
7. Make the smallest fix that addresses the confirmed cause and add a regression test.
8. Verify the fix with the test and, where possible, the same failing scenario.
9. Report what remains unverified. Stop and reassess after three unsuccessful fix attempts.

## Safety
- Do not print or commit secrets.
- Do not reset state or delete signal history to mask a symptom.
- Do not change strategy thresholds or risk controls as an operational workaround without evidence and explicit review.
- A successful workflow is not proof the scheduled bot ran correctly, the exchange accepted orders, or Telegram delivered messages.

## Report format
Symptom; timeline; evidence; confirmed root cause or unresolved hypotheses; fix; regression test; verification; residual risk.
