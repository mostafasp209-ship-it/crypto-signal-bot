# Engineering Review Skill

## Purpose
Review planned and completed changes as a cautious staff engineer. This skill adapts the review-and-test workflow idea from the open-source gstack project to this Python crypto signal bot; it does not require installing gstack or its browser tooling.

## Before implementation
- Identify the user-visible problem and measurable success criteria.
- Trace data flow from exchange candles through indicators, signal creation, state persistence, and Telegram output.
- List state transitions, failure paths, API boundaries, and concurrency risks.
- Map each critical behavior to existing tests and name missing tests.
- Prefer the smallest safe design; explicitly identify what is out of scope.

## After implementation
- Review the complete diff, not just the intended lines.
- Check failure handling, idempotency, duplicate signals, retries, and partial failures.
- Check for secrets, unsafe logging, data loss, and unintended changes to trading risk.
- Verify test assertions prove behavior rather than merely execute code.
- Check whether a workflow can write conflicting state or depends on files unavailable in CI.
- Separate confirmed defects from speculative improvements.

## Release gate
Do not recommend merge until compile checks and relevant tests pass, or explicitly document why they cannot run. Treat a passing CI job as evidence of code checks only, not evidence of profitable trading or successful production delivery.

## Output
1. Findings ordered by severity with file/line evidence.
2. Must-fix items versus optional improvements.
3. Tests run and their exact results.
4. Residual risks and rollout/rollback notes.
