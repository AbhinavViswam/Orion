"""
Brain/Usage.py: how much Orion is used, for Settings › Usage.

Two things are kept, the same as JARVIS on the phone:
  * tokens and turns per day, from the usage Gemini reports with its replies
  * every tool call: which tool, how long it took, whether it worked

They live in Brain/Usage.json on this machine (git-ignored). Recording must
never slow a reply or break one, so every write is guarded and quick.
"""
from __future__ import annotations

import atexit
import json
import threading
import time
from datetime import date, timedelta
from pathlib import Path

USAGE_PATH = Path(__file__).resolve().parent / "Usage.json"

KEEP_DAYS = 60          # older days and tool calls are dropped
MAX_CALLS = 5000        # tool calls kept at most

SAVE_DELAY = 5.0        # seconds: writes are batched, not one per reply

_lock = threading.Lock()
_session_tokens = 0
_cache: dict | None = None
_save_timer: threading.Timer | None = None
_dirty_path: Path | None = None     # where the pending write goes


def _Empty() -> dict:
    return {"days": {}, "tools": []}


def _Load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        data.setdefault("days", {})
        data.setdefault("tools", [])
    except Exception:
        data = _Empty()
    _cache = data
    return data


def _Save(data: dict) -> None:
    """Called under _lock. The file is rewritten a few seconds later, off the
    caller's thread — recording happens on the event loop, once per reply."""
    global _save_timer, _dirty_path
    _dirty_path = USAGE_PATH
    if _save_timer is None:
        _save_timer = threading.Timer(SAVE_DELAY, Flush)
        _save_timer.daemon = True
        _save_timer.start()


def Flush() -> None:
    """Write any pending change now (timer, exit, Reset)."""
    global _save_timer, _dirty_path
    with _lock:
        if _save_timer is not None:
            _save_timer.cancel()
            _save_timer = None
        path, _dirty_path = _dirty_path, None
        if path is not None and _cache is not None:
            _Write(_cache, path)


def _Write(data: dict, path: Path) -> None:
    try:
        cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
        data["days"] = {d: v for d, v in data["days"].items() if d >= cutoff}
        since = time.time() - KEEP_DAYS * 86400
        data["tools"] = [c for c in data["tools"] if c[0] >= since][-MAX_CALLS:]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        print(f"[Usage] could not save: {e}")


def Reset() -> None:
    """Forget the in-memory copy (tests point USAGE_PATH somewhere else)."""
    global _cache, _session_tokens
    Flush()
    with _lock:
        _cache = None
        _session_tokens = 0


# ── recording ───────────────────────────────────────────────────────────────

def AddTokens(total: int, prompt: int = 0, response: int = 0) -> None:
    """Count one reply's tokens toward today."""
    global _session_tokens
    try:
        total = int(total or 0)
    except Exception:
        return
    if total <= 0:
        return
    with _lock:
        data = _Load()
        day = data["days"].setdefault(date.today().isoformat(),
                                      {"tokens": 0, "turns": 0, "prompt": 0, "response": 0})
        day["tokens"] += total
        day["turns"] += 1
        day["prompt"] = day.get("prompt", 0) + int(prompt or 0)
        day["response"] = day.get("response", 0) + int(response or 0)
        _session_tokens += total
        _Save(data)


def RecordTool(name: str, ms: float, ok: bool) -> None:
    """Remember one tool call."""
    if not name:
        return
    with _lock:
        data = _Load()
        data["tools"].append([time.time(), str(name), int(max(0, ms)), bool(ok)])
        _Save(data)


# ── reading ─────────────────────────────────────────────────────────────────

def SessionTokens() -> int:
    return _session_tokens


def Today() -> dict:
    """{'tokens', 'turns', 'prompt', 'response'} for today (zeros when unused)."""
    with _lock:
        day = _Load()["days"].get(date.today().isoformat(), {})
    return {k: int(day.get(k, 0)) for k in ("tokens", "turns", "prompt", "response")}


def RecentDays(days: int = 14) -> list[dict]:
    """The last `days` days, oldest first, including the empty ones, so a quiet
    week never looks like a busy one."""
    with _lock:
        stored = dict(_Load()["days"])
    first = date.today() - timedelta(days=days - 1)
    out = []
    for i in range(days):
        d = (first + timedelta(days=i)).isoformat()
        v = stored.get(d, {})
        out.append({"day": d, "tokens": int(v.get("tokens", 0)), "turns": int(v.get("turns", 0))})
    return out


def UsualDay(days: int = 14) -> int:
    """Average tokens on the days with any use, not counting today."""
    used = [d["tokens"] for d in RecentDays(days)[:-1] if d["tokens"] > 0]
    return int(sum(used) / len(used)) if used else 0


def ToolStats(days: int = 7) -> list[dict]:
    """Calls, failures and average time per tool over the last `days` days,
    busiest first."""
    since = time.time() - days * 86400
    with _lock:
        calls = [c for c in _Load()["tools"] if c[0] >= since]
    by: dict[str, dict] = {}
    for _t, name, ms, ok in calls:
        s = by.setdefault(name, {"name": name, "calls": 0, "failed": 0, "ms": 0})
        s["calls"] += 1
        s["failed"] += 0 if ok else 1
        s["ms"] += ms
    out = []
    for s in by.values():
        s["avg_ms"] = int(s.pop("ms") / s["calls"])
        out.append(s)
    return sorted(out, key=lambda s: (-s["calls"], s["name"]))


atexit.register(Flush)     # nothing recorded is lost on a normal exit
