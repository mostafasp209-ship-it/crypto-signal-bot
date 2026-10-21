# Release Readiness Skill

## Checklist
- [ ] Scope and expected behavior are documented.
- [ ] Compile check and relevant regression tests pass.
- [ ] Final diff contains no unrelated changes or secrets.
- [ ] Signal history and persisted state remain backward-compatible, unless a migration is explicitly documented.
- [ ] Entry, exit, sizing, stop-loss, and take-profit behavior changes are clearly called out.
- [ ] API failure, rate-limit, missing-data, and Telegram failure paths are considered.
- [ ] Workflow permissions are minimal and no credentials are exposed in logs.
- [ ] Any scheduled workflow's inputs actually exist in its runner; do not assume runtime state files are committed.
- [ ] PR describes tests, limitations, rollback, and whether live behavior changes.
- [ ] After merge, inspect the workflow run and separately verify runtime delivery if applicable.

## Trading-specific gate
A strategy change must be tested on identical data against a baseline, account for fees/slippage, and include chronological out-of-sample validation. Do not infer profitability from CI, code review, or signal-log summaries alone.

## Release result
Return READY, NOT READY, or READY WITH EXPLICIT LIMITATIONS. List blocking issues first.
