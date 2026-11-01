# bot.py — decompress safety-patched source (2026-11-01 post-mortem fixes)
import zlib
import base64
from pathlib import Path

_dir = Path(__file__).resolve().parent
_b64 = "".join((_dir / f"bot_z_{i}.b64").read_text(encoding="ascii") for i in range(3))
_code = zlib.decompress(base64.b64decode(_b64)).decode("utf-8")
exec(compile(_code, str(_dir / "bot.py"), "exec"), globals())
