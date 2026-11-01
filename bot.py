"""
CRITICAL: bot.py was corrupted during an automated safety-patch attempt.

RESTORE NOW (local machine):

  git clone https://github.com/mostafasp209-ship-it/crypto-signal-bot.git
  cd crypto-signal-bot
  git checkout bd39ca3565a0bd54c7955bf8a5e5910a1203adb6 -- bot.py
  # Then apply SAFETY_PATCHES.md (or ask Grok to re-apply after restore)
  git add bot.py
  git commit -m "Restore bot.py from bd39ca3"
  git push origin main

Good commit SHA: bd39ca3565a0bd54c7955bf8a5e5910a1203adb6
safety_filters.py is already on main and is correct.
"""

raise SystemExit(
    "bot.py is in emergency restore mode. "
    "Checkout commit bd39ca3565a0bd54c7955bf8a5e5910a1203adb6 -- bot.py and push."
)
