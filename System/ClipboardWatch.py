"""
System/ClipboardWatch.py: notice useful things you copy and offer help.

Ported from Brahma's core/clipboard_sentry.py. It watches for three things:

    an error / stack trace   "You copied an error. Want me to look at it?"
                             If the error is in Orion's own code, it offers
                             self-repair instead.
    a YouTube link           "Want a summary of that video?"
    a GitHub repo link       "Want me to look at that repo?"

What is different from Brahma, on purpose:
  * The offer is a short instruction to the brain (Main.SayAside), which
    says it in its own words. Brahma also asked Gemini for a "witty comment"
    every time code was copied; that is gone.
  * What you copied never leaves this computer on its own. The model is only
    told WHAT KIND of thing was copied, in a short note. If you say yes, it
    reads the clipboard itself with computer_control.
  * Plain code, JSON and SQL are not announced. A developer copies those all
    day, and a voice commenting every time gets switched off.
  * Password managers mark what they copy as private; that is skipped. So is
    anything copied from Orion's own window.

Classifying is plain text matching, done here; nothing is sent to classify.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import threading
import time
from typing import Callable, Optional

POLL_SECONDS = 1.0
MAX_CHARS = 20_000                    # a longer clipboard is a document, not a paste
GAP_SECONDS = 60                      # at most one offer a minute, whatever was copied
COOLDOWN = {"error": 180, "youtube": 300, "github": 300}

_YOUTUBE = re.compile(r"(?:https?://)?(?:www\.|m\.)?(?:youtube\.com/(?:watch\?\S*v=|shorts/)|youtu\.be/)[\w-]{6,}")
_GITHUB = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[\w.-]+/[\w.-]+")
_ERROR_LINE = re.compile(r"^\s*(?:[\w.]+\.)?\w*(?:Error|Exception|Exit)\b[^\n]*", re.M)
_STACK_LINE = re.compile(r'^\s*(?:File "[^"]+", line \d+|at \S+.*[(:]\S*\d+|at \w[\w.$<>]*\()', re.M)


# ── what was copied ─────────────────────────────────────────────────────────

def Classify(text: str) -> Optional[str]:
    """'error', 'youtube', 'github', or None for anything else."""
    t = (text or "").strip()
    if len(t) < 12 or len(t) > MAX_CHARS:
        return None
    if "Traceback (most recent call last):" in t:
        return "error"
    if "\n" in t and _ERROR_LINE.search(t) and _STACK_LINE.search(t):
        return "error"                # JavaScript, Java, C# and the like
    one_line = "\n" not in t and " " not in t
    if one_line and _YOUTUBE.search(t):
        return "youtube"
    if one_line and _GITHUB.search(t):
        return "github"
    return None


def Offer(kind: str, text: str) -> tuple[str, str]:
    """(what to tell the user, what the brain should do if they say yes)."""
    read_first = "If they say yes or ask about it, read it first with computer_control action 'copy'."
    if kind == "error":
        try:
            from System import SelfRepair
            place = SelfRepair.Locate(text)
        except Exception:
            place = None
        if place is not None:
            name = os.path.basename(place.rel)
            return (f"That error is from my own code, in {name}. Say fix yourself and I'll draft a repair.",
                    f"The user copied an error from Orion's own code ({place.rel} line {place.line}). "
                    "If they say yes or ask you to fix it, call self_repair with action 'fix' and the "
                    "copied error (read it with computer_control action 'copy').")
        return ("You copied an error. Want me to look at it?",
                f"The user copied an error message. {read_first}")
    if kind == "youtube":
        return ("You copied a YouTube link. Want a summary of it?",
                f"The user copied a YouTube link. {read_first}")
    if kind == "github":
        return ("You copied a GitHub repo. Want me to look at it?",
                f"The user copied a GitHub repository link. {read_first}")
    return ("", "")


# ── reading the clipboard ───────────────────────────────────────────────────

def _User32():
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        return ctypes.windll.user32
    except Exception:
        return None


def _Sequence() -> Optional[int]:
    """Windows bumps this number on every copy, so the clipboard is only read when it changed."""
    u = _User32()
    try:
        return int(u.GetClipboardSequenceNumber()) if u else None
    except Exception:
        return None


def _Private() -> bool:
    """Password managers tag what they copy with this format; it must not be looked at."""
    u = _User32()
    if not u:
        return False
    try:
        fmt = u.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing")
        return bool(fmt and u.IsClipboardFormatAvailable(fmt))
    except Exception:
        return False


def _FromOrion() -> bool:
    """Copied from Orion's own window (a reply, a log line): not news."""
    u = _User32()
    if not u:
        return False
    try:
        import ctypes
        pid = ctypes.c_ulong(0)
        u.GetWindowThreadProcessId(u.GetForegroundWindow(), ctypes.byref(pid))
        return pid.value == os.getpid()
    except Exception:
        return False


def _Paste() -> str:
    try:
        import pyperclip
        return pyperclip.paste() or ""
    except Exception:
        return ""


# ── the watcher ─────────────────────────────────────────────────────────────

class ClipboardWatch:
    """Calls `on_found(kind, text, spoken, note)` for something worth an offer.
    `enabled()` is asked on every copy, so the Settings switch works at once."""

    def __init__(self, on_found: Callable[[str, str, str, str], None],
                 enabled: Callable[[], bool] = lambda: True):
        self.on_found = on_found
        self.enabled = enabled
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_at: dict[str, float] = {}
        self._last_any = 0.0
        self._seen: list[str] = []          # hashes of what was already offered

    def Start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._Loop, daemon=True, name="clipboard-watch")
        self._thread.start()

    def Stop(self) -> None:
        self._stop.set()

    def Consider(self, text: str, now: Optional[float] = None) -> Optional[str]:
        """Decide about one new clipboard text; returns the kind if it was offered."""
        now = time.monotonic() if now is None else now
        kind = Classify(text)
        if not kind:
            return None
        digest = hashlib.sha1(text.strip().encode("utf-8", "replace")).hexdigest()
        if digest in self._seen:
            return None
        if now - self._last_any < GAP_SECONDS and self._last_any:
            return None
        if now - self._last_at.get(kind, -1e9) < COOLDOWN.get(kind, 300):
            return None
        spoken, note = Offer(kind, text)
        if not spoken:
            return None
        self._seen = (self._seen + [digest])[-50:]
        self._last_at[kind] = now
        self._last_any = now
        try:
            self.on_found(kind, text, spoken, note)
        except Exception as e:
            print(f"[ClipboardWatch] {e}")
        return kind

    def _Loop(self) -> None:
        seq = _Sequence()
        last = _Paste().strip() if seq is None else ""
        while not self._stop.wait(POLL_SECONDS):
            try:
                if seq is not None:
                    now_seq = _Sequence()
                    if now_seq == seq:
                        continue
                    seq = now_seq
                if not self.enabled():
                    continue
                if _Private():
                    continue
                text = _Paste().strip()
                if not text or text == last:
                    continue
                last = text
                if _FromOrion():
                    continue
                self.Consider(text)
            except Exception as e:
                print(f"[ClipboardWatch] {e}")
