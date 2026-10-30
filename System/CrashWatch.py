"""
System/CrashWatch.py: catch Orion's crashes that no tool reported, and offer a fix.

Ported from Brahma's crash monitor (main.py, `_terminal_crash_monitor`), and it
feeds into self-repair (System/SelfRepair.py) instead of Brahma's auto-heal.

Self-repair already hears about a tool that crashes. This covers the rest:

    a background thread dying      threading.excepthook
    a button or panel crashing     sys.excepthook (Qt sends slot errors here)
    a stray asyncio task failing   the event loop's exception handler
    the voice session breaking     Main.py reports it (not network blips)
    Orion dying outright           faulthandler writes Config/Crashes/Fatal.log;
                                   the next start finds it

Each crash is kept as the "last failure", so "fix yourself" works on it. When it
is in Orion's own code, the brain is told to say so and offer a repair. Nothing is changed unless you say yes,
like the rest of self-repair. A crash that ended Orion last time is offered once
at the next start. The same error is only mentioned once every 10 minutes.

Crashes that are not in Orion's code (a library, the network) are logged, not
announced: there is nothing Orion could repair.
"""
from __future__ import annotations

import asyncio
import faulthandler
import json
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
CRASH_DIR = ROOT / "Config" / "Crashes"
LAST_FILE = CRASH_DIR / "LastCrash.json"
FATAL_FILE = CRASH_DIR / "Fatal.log"
REPEAT_SECONDS = 600

# Who gets told. Set by Main.py once there is a voice to say it with; until then
# crashes wait here (at most a few).
_announce: Optional[Callable[[str, str], None]] = None
_pending: list[tuple[str, str]] = []
_said: dict[str, float] = {}
_lock = threading.Lock()
_busy = threading.local()        # a crash while reporting a crash is not reported again
_fatal_handle = None
_installed = False


def Signature(tb: str) -> str:
    """The same bug crashing twice has the same file, line and error."""
    try:
        from System import SelfRepair
        p = SelfRepair.Locate(tb)
        if p is not None:
            return f"{p.rel}:{p.line}:{p.error.split(':')[0]}"
    except Exception:
        pass
    lines = [l.strip() for l in (tb or "").strip().splitlines() if l.strip()]
    return lines[-1][:120] if lines else ""


def Words(tb: str, earlier: bool = False) -> tuple[str, str]:
    """(the spoken line, the note for the model), or ('', '') when it is not Orion's own bug."""
    try:
        from System import SelfRepair
        place = SelfRepair.Locate(tb)
    except Exception:
        place = None
    if place is None:
        return ("", "")
    name = Path(place.rel).name
    when = "Last time, I crashed" if earlier else "Something in me just crashed"
    spoken = f"{when}, in {name}. Say fix yourself and I'll draft a repair."
    note = (f"(Orion's own code crashed: {place.rel} line {place.line}, {place.error}. If they agree, "
            "call self_repair with action 'fix'; the crash is already kept, so leave 'error' empty.)")
    return (spoken, note)


def Report(tb: str, where: str = "", earlier: bool = False, now: Optional[float] = None) -> bool:
    """Keep a crash for self-repair and mention it if it is repairable and new.
    True when it was (or will be) announced. Never raises."""
    if getattr(_busy, "on", False) or not (tb or "").strip():
        return False
    _busy.on = True
    try:
        now = time.monotonic() if now is None else now
        try:
            from System import SelfRepair
            SelfRepair.NoteFailure(where or "crash", tb)
        except Exception:
            pass
        _Keep(tb, where)
        print(f"[CrashWatch] {where or 'crash'}: {Signature(tb)}")
        spoken, note = Words(tb, earlier)
        if not spoken:
            return False
        sig = Signature(tb)
        with _lock:
            if now - _said.get(sig, -1e9) < REPEAT_SECONDS:
                return False
            _said[sig] = now
            target = _announce
            if target is None:
                _pending[:] = (_pending + [(spoken, note)])[-3:]
                return True
        try:
            target(spoken, note)
        except Exception as e:
            print(f"[CrashWatch] could not announce: {e}")
        return True
    finally:
        _busy.on = False


def _Keep(tb: str, where: str) -> None:
    try:
        CRASH_DIR.mkdir(parents=True, exist_ok=True)
        LAST_FILE.write_text(json.dumps({"where": where, "traceback": tb, "at": time.time()},
                                        indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def SetAnnouncer(fn: Optional[Callable[[str, str], None]]) -> None:
    """Main.py: how to tell the user. Crashes that happened before this are told now."""
    global _announce
    with _lock:
        _announce = fn
        waiting, _pending[:] = list(_pending), []
    if fn:
        for spoken, note in waiting:
            try:
                fn(spoken, note)
            except Exception as e:
                print(f"[CrashWatch] could not announce: {e}")


# ── the hooks ───────────────────────────────────────────────────────────────

def _Text(exc_type, exc, tb) -> str:
    return "".join(traceback.format_exception(exc_type, exc, tb))


def Install() -> None:
    """Call once, as early as possible. Keeps the hooks that were there before."""
    global _installed, _fatal_handle
    if _installed:
        return
    _installed = True
    _PreviousFatal()

    old_sys = sys.excepthook

    def SysHook(exc_type, exc, tb):
        if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            Report(_Text(exc_type, exc, tb), "the window")
        try:
            old_sys(exc_type, exc, tb)
        except Exception:
            pass
    sys.excepthook = SysHook

    old_thread = threading.excepthook

    def ThreadHook(args):
        if args.exc_type is not None and not issubclass(args.exc_type, (KeyboardInterrupt, SystemExit)):
            name = getattr(args.thread, "name", "") or "a background job"
            Report(_Text(args.exc_type, args.exc_value, args.exc_traceback), f"thread {name}")
        try:
            old_thread(args)
        except Exception:
            pass
    threading.excepthook = ThreadHook

    # A hard crash (no Python exception to catch) still leaves a trace here.
    try:
        CRASH_DIR.mkdir(parents=True, exist_ok=True)
        _fatal_handle = open(FATAL_FILE, "w", encoding="utf-8")
        faulthandler.enable(file=_fatal_handle, all_threads=True)
        import atexit
        atexit.register(MarkEnded)
    except Exception as e:
        print(f"[CrashWatch] fatal log unavailable: {e}")


def WatchLoop(loop: asyncio.AbstractEventLoop) -> None:
    """Catch tasks that fail with nobody awaiting them."""
    def Handler(lp, context):
        exc = context.get("exception")
        if exc is not None and not isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            Report(_Text(type(exc), exc, exc.__traceback__), "a background task")
        lp.default_exception_handler(context)
    try:
        loop.set_exception_handler(Handler)
    except Exception:
        pass


def FatalAsTraceback(text: str) -> str:
    """faulthandler lists the crashing thread newest call FIRST; a traceback is
    newest LAST. Turn the crashing thread's part into a traceback so self-repair
    blames the right frame. '' when there is no crashing thread in it."""
    lines = (text or "").splitlines()
    header = next((l.strip() for l in lines if l.strip()), "")
    frames, inside = [], False
    for l in lines:
        s = l.strip()
        if s.startswith(("Current thread", "Thread ")) and "most recent call first" in s:
            if inside:
                break
            inside = s.startswith("Current thread")
            continue
        if inside:
            if not s:
                break
            if s.startswith("File "):
                frames.append("  " + s)
    if not frames:
        return ""
    return "Traceback (most recent call last):\n" + "\n".join(reversed(frames)) + "\n" + header


def _PreviousFatal() -> None:
    """Orion died hard last time: faulthandler left a trace. Offer it once."""
    try:
        text = FATAL_FILE.read_text(encoding="utf-8", errors="replace") if FATAL_FILE.exists() else ""
    except Exception:
        text = ""
    if not text.strip():
        return
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        FATAL_FILE.replace(CRASH_DIR / f"Fatal-{stamp}.log")
    except Exception:
        pass
    tb = FatalAsTraceback(text)
    if tb:
        Report(tb, "Orion stopping", earlier=True)
    else:
        print(f"[CrashWatch] Orion stopped unexpectedly last time; see {CRASH_DIR}")


def MarkEnded() -> None:
    """A clean exit. On Windows faulthandler also writes down errors that were
    caught and survived, so the log is removed here: only a real hard crash
    (where this never runs) leaves one for the next start."""
    try:
        faulthandler.disable()
        if _fatal_handle:
            _fatal_handle.close()
        FATAL_FILE.unlink(missing_ok=True)
    except Exception:
        pass
