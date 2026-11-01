# bot.py — assembled from chunks (emergency restore + safety patches 2026-11-01)
from pathlib import Path
_dir = Path(__file__).resolve().parent
_code = "".join(
    (_dir / f"bot_chunk_{i}.txt").read_text(encoding="utf-8")
    for i in range(5)
)
exec(compile(_code, str(_dir / "bot.py"), "exec"), globals())
