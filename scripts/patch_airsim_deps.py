"""
Patches msgpackrpc for tornado 5+ compatibility. Run once after `pip install`.
Also called automatically by scripts/run.py.

Two known breakages in msgpackrpc when used with tornado >= 5:
  1. loop.py       — passes IOLoop as 3rd positional arg to PeriodicCallback
                     (tornado 5 renamed that param from io_loop → jitter)
  2. transport/tcp.py — passes io_loop= kwarg to IOStream constructor
                        (tornado 5 removed that parameter entirely)
"""
import sys
from pathlib import Path


def _find_msgpackrpc() -> Path | None:
    for p in sys.path:
        candidate = Path(p) / "msgpackrpc"
        if candidate.is_dir():
            return candidate
    return None


def _patch_file(path: Path, old: str, new: str, label: str) -> None:
    if not path.exists():
        print(f"  SKIP  {label} — file not found: {path}")
        return
    content = path.read_text(encoding="utf-8")
    if old not in content:
        print(f"  OK    {label} — already patched or pattern not found")
        return
    path.write_text(content.replace(old, new), encoding="utf-8")
    print(f"  PATCH {label}")


pkg = _find_msgpackrpc()
if pkg is None:
    print("ERROR: msgpackrpc not found in sys.path. Install deps first.")
    sys.exit(1)

print(f"Found msgpackrpc at: {pkg}")

# ── Patch 1: loop.py ──────────────────────────────────────────────────────────
# Old: ioloop.PeriodicCallback(callback, callback_time, self._ioloop)
# New: ioloop.PeriodicCallback(callback, callback_time)
_patch_file(
    pkg / "loop.py",
    old="ioloop.PeriodicCallback(callback, callback_time, self._ioloop)",
    new="ioloop.PeriodicCallback(callback, callback_time)",
    label="loop.py — removed io_loop positional arg from PeriodicCallback",
)

# ── Patch 2: transport/tcp.py ─────────────────────────────────────────────────
# Old: IOStream(self._address.socket(), io_loop=self._session._loop._ioloop)
# New: IOStream(self._address.socket())
_patch_file(
    pkg / "transport" / "tcp.py",
    old="IOStream(self._address.socket(), io_loop=self._session._loop._ioloop)",
    new="IOStream(self._address.socket())",
    label="transport/tcp.py — removed io_loop from IOStream",
)

print("Done.")
