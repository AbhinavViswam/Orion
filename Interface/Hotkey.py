"""
Push-to-talk — hold a key, speak, release.

Why this exists
---------------
An open mic is not always what you want: in a meeting, in a noisy room, or
when you simply do not want it listening, a key you hold is faster and never
mishears. It is also the natural way to use the
assistant while another window has focus.

How it works, and what it costs
-------------------------------
Zero new dependencies, best available mechanism per platform:

* **Windows** — `GetAsyncKeyState` polled from one small thread. This is
  deliberately *not* `RegisterHotKey`, which only reports a press: push-to-talk
  needs the release too, and it needs to work while another application has
  focus. Polling two virtual-key codes 30 times a second is a rounding error of
  CPU and needs no message loop.
* **macOS / Linux** — no portable way to read global key state without pulling
  in a new package or asking for accessibility permissions, so the chord is
  bound as an application shortcut instead: it works whenever the assistant's
  window has focus. `scope` reports which of the two you got, so the UI can say
  so honestly rather than pretending.

The class never raises. If the platform hook cannot be installed it simply
reports `scope == "window"` and the Qt shortcut carries it.
"""

from __future__ import annotations

import platform
import threading
import time
from typing import Callable

_OS = platform.system()

# The hold-to-speak key: Right Ctrl on its own. The watcher reads keys without
# blocking them, so the focused app still sees the key too. Ctrl alone does
# nothing in almost every app, unlike Ctrl+Space (autocomplete, input language).
DEFAULT_CHORD = ("rctrl",)

# Windows virtual-key codes for the names we accept.
_VK = {
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "rctrl": 0xA3,
    "space": 0x20, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "capslock": 0x14, "insert": 0x2D,
}

# Keys that never count as "another key pressed": mouse buttons and the Ctrl
# codes themselves (Windows reports Right Ctrl as 0x11 and 0xA3 both).
_IGNORE_VK = {0x01, 0x02, 0x04, 0x05, 0x06, 0x11, 0xA2, 0xA3}

# Qt key sequence text for the same chord, used by the windowed fallback.
_QT_NAME = {"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt", "space": "Space",
            "rctrl": "Right Ctrl", "f8": "F8", "f9": "F9", "f10": "F10",
            "capslock": "CapsLock", "insert": "Ins"}

_POLL_HZ = 30.0
# A key has to be down this long before we call it speech. It stops a stray
# brush of the chord from opening the microphone.
_DEBOUNCE_S = 0.06
# A lone modifier waits longer, so Right Ctrl+C has time to show it is a
# shortcut before the microphone opens. Still well under a breath.
_MODIFIER_DEBOUNCE_S = 0.2
_MODIFIERS = {"ctrl", "rctrl", "shift", "alt"}


def ChordLabel(chord=DEFAULT_CHORD) -> str:
    """Human-readable name of the chord, for the UI and the logs."""
    return "+".join(_QT_NAME.get(k, k.title()) for k in chord)


def QtSequence(chord=DEFAULT_CHORD) -> str:
    """The same chord as a QKeySequence string."""
    return "+".join(_QT_NAME.get(k, k.title()) for k in chord)


class PushToTalk:
    """Calls `on_change(held: bool)` whenever the chord is pressed or released.

    Start it once; it is safe to start and stop repeatedly, and safe to stop a
    detector that never started.
    """

    def __init__(self, on_change: Callable[[bool], None], chord=DEFAULT_CHORD):
        self._on_change = on_change
        self._chord = tuple(chord)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._held = False
        self._scope = "window"

    # ── state ───────────────────────────────────────────────────────────────

    @property
    def Held(self) -> bool:
        return self._held

    @property
    def scope(self) -> str:
        """'global' once a system-wide hook is running, else 'window'."""
        return self._scope

    @property
    def label(self) -> str:
        return ChordLabel(self._chord)

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> str:
        """Begin watching. Returns the scope actually achieved."""
        self.stop()
        self._stop.clear()
        if _OS == "Windows" and self.CanPoll():
            self._scope = "global"
            self._thread = threading.Thread(
                target=self.PollLoop, name="push-to-talk", daemon=True)
            self._thread.start()
        else:
            self._scope = "window"
        return self._scope

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None and t.is_alive():
            t.join(timeout=1.0)
        self._SetHeld(False)

    # ── the windowed fallback drives this directly ──────────────────────────

    def SetHeld(self, held: bool) -> None:
        """Feed a press/release from a Qt shortcut (non-Windows, or no hook)."""
        self._SetHeld(bool(held))

    # ── internals ───────────────────────────────────────────────────────────

    def CanPoll(self) -> bool:
        try:
            import ctypes
            ctypes.windll.user32.GetAsyncKeyState  # noqa: B018 — presence check
            return all(k in _VK for k in self._chord)
        except Exception:
            return False

    def _SetHeld(self, held: bool) -> None:
        if held == self._held:
            return
        self._held = held
        try:
            self._on_change(held)
        except Exception:
            pass          # a listener fault must never kill the watcher

    def PollLoop(self) -> None:
        import ctypes
        user32 = ctypes.windll.user32
        codes = [_VK[k] for k in self._chord]
        ignore = _IGNORE_VK | set(codes)
        others = [vk for vk in range(0x08, 0xFF) if vk not in ignore]
        period = 1.0 / _POLL_HZ
        wait = (_MODIFIER_DEBOUNCE_S if set(self._chord) <= _MODIFIERS
                else _DEBOUNCE_S)
        down_since = 0.0
        # A press that turned out to be a shortcut (Right Ctrl+C...). Ignored
        # until the key comes back up.
        spoiled = False

        while not self._stop.is_set():
            try:
                # The high bit of the return value is "currently down".
                down = all(user32.GetAsyncKeyState(c) & 0x8000 for c in codes)
                # Before speech starts, any other key down means a shortcut.
                # Once talking, other keys are fine (you might type a word).
                if down and not self._held and not spoiled:
                    spoiled = any(user32.GetAsyncKeyState(vk) & 0x8000
                                  for vk in others)
            except Exception:
                break     # driver or session teardown — fall back to windowed
            now = time.monotonic()
            if down and not spoiled:
                if down_since == 0.0:
                    down_since = now
                elif now - down_since >= wait:
                    self._SetHeld(True)
            elif not down:
                down_since = 0.0
                spoiled = False
                self._SetHeld(False)
            self._stop.wait(period)

        self._SetHeld(False)
        self._scope = "window"
