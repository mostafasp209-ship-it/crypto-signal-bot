"""Atomic file-backed persistence for signal state."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from config import DATA_FILE


def load_state(path: str | os.PathLike[str] = DATA_FILE) -> dict[str, Any]:
    """Load signal state, preserving the legacy empty-state fallback behavior."""
    state_path = Path(path)
    if not state_path.exists():
        return {"signals": [], "last_run": None}

    try:
        with state_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        # Fail closed: never replace a damaged history file with an empty state.
        raise RuntimeError(f"Signal state {state_path} is unreadable; refusing to reset history.") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Signal state {state_path} must contain a JSON object.")
    if "signals" in data and not isinstance(data["signals"], list):
        raise ValueError(f"Signal state {state_path} has an invalid 'signals' collection.")
    data.setdefault("signals", [])
    data.setdefault("last_run", None)
    return data


def save_state(state: dict[str, Any], path: str | os.PathLike[str] = DATA_FILE) -> None:
    """Write state atomically so interrupted runs do not leave partial JSON."""
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = state_path.with_name(state_path.name + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_path, state_path)
