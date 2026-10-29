"""
System/FastCommands.py: everyday commands answered in milliseconds, with no
model at all ("volume up", "mute", "take a screenshot", "open Spotify", "what
time is it"). The same idea as JARVIS on the phone (brain/local/LocalIntents.kt).

Deliberately strict. A match is a tool call made without asking, so the WHOLE
phrase must be accounted for: every word is either what the command is about
(a number, an app's name) or one of the few polite words allowed around it.
"Turn the volume up" matches; "turn the volume up when the song ends" does not,
and nor does anything negated. Whatever does not match goes to the model, which
is slower but understands. A wrong action is worse than a slow one.

A match names a real tool and its real arguments, so it runs through the same
dispatcher as Gemini's calls: undo, confirmations and usage counting included.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

MAX_WORDS = 9

_NEGATION = re.compile(r"\b(don'?t|do not|never|not|no|stop|cancel|without|isn'?t|wasn'?t|can'?t)\b")

# Words a command may be wrapped in without changing what it means.
_LEADING = ("hey", "ok", "okay", "please", "can you", "could you", "would you", "will you",
            "can u", "pls", "just", "go ahead and", "i want you to", "i need you to")
_TRAILING = ("please", "for me", "now", "right now", "thanks", "thank you", "pls", "quickly")

_NUMBER_WORDS = {
    "zero": 0, "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
    "one hundred": 100, "a hundred": 100, "full": 100, "max": 100, "maximum": 100,
    "half": 50,
}


@dataclass
class Match:
    """A command understood: the tool to call (None for one answered here,
    like the time), its arguments, and how to say the outcome."""
    tool: Optional[str]
    args: dict = field(default_factory=dict)
    reply: Callable[[str], str] = lambda result: "Done."
    label: str = ""                 # what it was, for the log


# ── normalising ─────────────────────────────────────────────────────────────

def Normalize(heard: str, names: tuple[str, ...] = ()) -> str:
    t = (heard or "").lower().strip()
    t = t.replace("’", "'").replace("%", " percent")
    t = re.sub(r"[^\w\s'+]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    # The assistant's own name, said first or last ("Ultron, mute").
    for n in names:
        n = (n or "").lower().strip()
        if n:
            t = re.sub(rf"^(hey |ok |okay )?{re.escape(n)}\b ?", "", t)
            t = re.sub(rf" ?\b{re.escape(n)}$", "", t)
    changed = True
    while changed and t:
        changed = False
        for w in _LEADING:
            if t == w:
                return ""
            if t.startswith(w + " "):
                t, changed = t[len(w) + 1:], True
        for w in _TRAILING:
            if t.endswith(" " + w):
                t, changed = t[: -len(w) - 1], True
    return t.strip()


def _Percent(text: str) -> Optional[int]:
    m = re.search(r"\b(\d{1,3})\b", text)
    if m:
        v = int(m.group(1))
        return v if 0 <= v <= 100 else None
    for words in sorted(_NUMBER_WORDS, key=len, reverse=True):
        if re.search(rf"\b{words}\b", text):
            return _NUMBER_WORDS[words]
    return None


def _Strip(text: str, words: tuple[str, ...]) -> str:
    out = text
    for w in sorted(words, key=len, reverse=True):
        out = re.sub(rf"\b{re.escape(w)}\b", " ", out)
    return re.sub(r"\s+", " ", out).strip()


# ── replies ─────────────────────────────────────────────────────────────────

def _Problem(result: str) -> bool:
    try:
        from Brain.Outcome import IsProblem
        return IsProblem(result)
    except Exception:
        return result.startswith(("status=failed", "status=ask", "status=unverified"))


def _Say(ok_text: str) -> Callable[[str], str]:
    def Reply(result: str) -> str:
        if result.startswith("status=unverified"):
            return ok_text.rstrip(".") + ", though I couldn't check it."
        if result and "status=ask" in result:
            return "It's waiting for you to confirm on screen."
        if result and _Problem(result):
            m = re.search(r'reason="([^"]+)"', result)
            return f"That didn't work: {m.group(1)}." if m else "That didn't work."
        return ok_text
    return Reply


def _Settings(action: str, ok_text: str, value=None) -> Match:
    args = {"action": action}
    if value is not None:
        args["value"] = str(value)
    return Match("computer_settings", args, _Say(ok_text), action.replace("_", " "))


# ── the commands ────────────────────────────────────────────────────────────

_TIME_ASKS = {"what time is it", "what's the time", "whats the time", "what is the time", "time",
              "the time", "tell me the time", "what time is it now", "current time",
              "what's the time now", "do you know the time", "time please"}
_DATE_ASKS = {"what's the date", "whats the date", "what is the date", "what's today's date",
              "what is today's date", "today's date", "date", "what day is it", "what day is today",
              "what's the day", "which day is it", "what date is it", "what's the date today",
              "what is the date today"}


def _Time(t: str) -> Optional[Match]:
    if t in _TIME_ASKS:
        now = datetime.now()
        h = now.strftime("%I:%M %p").lstrip("0")
        return Match(None, {}, lambda _r, h=h: f"It's {h}.", "time")
    if t in _DATE_ASKS:
        now = datetime.now()
        d = f"{now.strftime('%A')}, {now.day} {now.strftime('%B %Y')}"
        return Match(None, {}, lambda _r, d=d: f"It's {d}.", "date")
    return None


_VOLUME_WORDS = ("turn", "the", "volume", "sound", "audio", "it", "a", "bit", "little", "up", "down",
                 "set", "to", "make", "increase", "decrease", "raise", "lower", "reduce", "more",
                 "percent", "level", "at", "put", "change", "louder", "quieter", "softer")


def _Volume(t: str) -> Optional[Match]:
    if t in ("mute", "mute it", "mute the sound", "mute the volume", "mute audio", "mute the audio",
             "mute sound", "mute volume", "mute the computer", "mute the pc", "silence",
             "unmute", "unmute it", "unmute the sound", "unmute the volume", "unmute audio",
             "toggle mute"):
        return _Settings("mute", "Unmuted." if t.startswith("unmute") else "Muted.")
    words = set(t.split())
    if not words & {"volume", "sound", "audio", "louder", "quieter", "softer"}:
        return None
    if _Strip(t, _VOLUME_WORDS + tuple(_NUMBER_WORDS)).strip(" 0123456789"):
        return None                                   # something else is in the sentence
    pct = _Percent(t)
    _up = {"up", "increase", "raise", "louder", "more"}
    _down = {"down", "decrease", "lower", "reduce", "quieter", "softer"}
    if pct is not None:
        if words & (_up | _down):
            return None                               # "up by 10"? the model can work that out
        return _Settings("volume_set", f"Volume {pct}%.", pct)
    if words & {"up", "increase", "raise", "louder", "more"}:
        return _Settings("volume_up", "Volume up.")
    if words & {"down", "decrease", "lower", "reduce", "quieter", "softer"}:
        return _Settings("volume_down", "Volume down.")
    return None


_BRIGHT_WORDS = ("turn", "the", "screen", "brightness", "display", "it", "a", "bit", "little", "up",
                 "down", "set", "to", "make", "increase", "decrease", "raise", "lower", "reduce",
                 "more", "brighter", "dimmer", "dim", "percent", "level", "at", "put", "change")


def _Brightness(t: str) -> Optional[Match]:
    words = set(t.split())
    if not words & {"brightness", "brighter", "dimmer"} and t not in ("dim the screen", "dim the display"):
        return None
    if _Strip(t, _BRIGHT_WORDS + tuple(_NUMBER_WORDS)).strip(" 0123456789"):
        return None
    if words & {"up", "increase", "raise", "brighter", "more"}:
        return _Settings("brightness_up", "Brighter.")
    if words & {"down", "decrease", "lower", "reduce", "dimmer", "dim"}:
        return _Settings("brightness_down", "Dimmer.")
    return None


# Exact phrases for one-shot actions. Kept as whole phrases on purpose: these
# act on whatever is in front of the user, so near misses go to the model.
_PHRASES: dict[str, tuple[str, str]] = {}


def _Add(action: str, reply: str, *phrases: str) -> None:
    for p in phrases:
        _PHRASES[p] = (action, reply)


_Add("screenshot", "Screenshot taken.", "screenshot", "take a screenshot", "take screenshot",
     "take a screen shot", "screen shot", "capture the screen", "capture my screen",
     "grab a screenshot", "take a picture of the screen")
_Add("lock_screen", "Locking.", "lock", "lock the screen", "lock screen", "lock my screen",
     "lock the computer", "lock my computer", "lock the pc", "lock my pc", "lock my laptop",
     "lock the laptop")
_Add("show_desktop", "Desktop.", "show desktop", "show the desktop", "show my desktop",
     "go to desktop", "go to the desktop", "minimize everything", "minimise everything",
     "minimize all", "minimise all")
_Add("minimize", "Minimised.", "minimize", "minimise", "minimize this", "minimise this",
     "minimize the window", "minimise the window", "minimize this window", "minimise this window")
_Add("maximize", "Maximised.", "maximize", "maximise", "maximize this", "maximise this",
     "maximize the window", "maximise the window", "maximize this window", "maximise this window")
_Add("close_window", "Closed.", "close this window", "close the window", "close window")
_Add("full_screen", "Full screen.", "full screen", "fullscreen", "go full screen", "make it full screen",
     "make this full screen")
_Add("snap_left", "Snapped left.", "snap left", "snap it left", "snap this left", "snap to the left")
_Add("snap_right", "Snapped right.", "snap right", "snap it right", "snap this right", "snap to the right")
_Add("switch_window", "Switched.", "switch window", "switch windows", "next window", "alt tab")
_Add("task_manager", "Task Manager.", "open task manager", "task manager", "show task manager")
_Add("open_settings", "Settings.", "open settings", "open windows settings", "open system settings",
     "open the settings app")
_Add("file_explorer", "File Explorer.", "open file explorer", "file explorer", "open explorer",
     "open my files", "open files", "open the file explorer")
_Add("new_tab", "New tab.", "new tab", "open a new tab", "open new tab")
_Add("close_tab", "Tab closed.", "close tab", "close this tab", "close the tab")
_Add("next_tab", "Next tab.", "next tab", "go to the next tab", "switch tab")
_Add("prev_tab", "Previous tab.", "previous tab", "last tab", "go to the previous tab")
_Add("go_back", "Back.", "go back", "back")
_Add("go_forward", "Forward.", "go forward", "forward")
_Add("refresh_page", "Refreshed.", "refresh", "refresh the page", "refresh this page", "reload",
     "reload the page", "reload this page", "refresh page", "reload page")
_Add("zoom_in", "Zoomed in.", "zoom in")
_Add("zoom_out", "Zoomed out.", "zoom out")
_Add("zoom_reset", "Zoom reset.", "reset zoom", "reset the zoom", "normal zoom")
_Add("scroll_down", "", "scroll down", "scroll down a bit")
_Add("scroll_up", "", "scroll up", "scroll up a bit")
_Add("scroll_top", "", "scroll to the top", "go to the top", "top of the page")
_Add("scroll_bottom", "", "scroll to the bottom", "go to the bottom", "bottom of the page")
_Add("play_pause", "", "pause", "play", "resume", "pause the video", "play the video",
     "pause the music", "play the music", "resume the music", "resume the video", "pause it",
     "play it", "pause video", "pause music", "play pause")
_Add("sleep_display", "Screen off.", "turn off the screen", "turn off the display", "screen off",
     "turn the screen off", "turn the display off", "sleep the display")


def _Phrase(t: str) -> Optional[Match]:
    hit = _PHRASES.get(t)
    if hit is None:
        return None
    action, reply = hit
    return _Settings(action, reply or "Done.")


# open_app: "open spotify", "launch visual studio code", "start notepad".
_OPEN_VERBS = ("open", "launch", "start", "run", "open up", "fire up", "bring up", "load")
# A name containing any of these is not just an app: a website, a file, a
# second step. Those need the model (browser_control, file tools...).
_NOT_AN_APP = {"and", "then", "with", "in", "on", "to", "from", "for", "about", "the website",
               "website", "site", "page", "tab", "file", "folder", "document", "link", "url", "com",
               "dot", "www", "http", "https", "search", "google", "youtube", "video", "song", "music",
               "playlist", "my", "email", "mail", "chat", "message", "new", "this", "that", "it",
               "timer", "alarm", "reminder", "game", "window", "door", "app store"}


def _OpenApp(t: str) -> Optional[Match]:
    for verb in sorted(_OPEN_VERBS, key=len, reverse=True):
        if t.startswith(verb + " "):
            name = t[len(verb) + 1:].strip()
            break
    else:
        return None
    name = re.sub(r"^(the|a|an) ", "", name)
    name = re.sub(r" (app|application|program)$", "", name).strip()
    words = name.split()
    if not words or len(words) > 4:
        return None
    if any(w in _NOT_AN_APP for w in words) or any(p in name for p in ("the website", "app store", ".")):
        return None
    pretty = " ".join(w if w.isupper() else w.capitalize() for w in words)
    return Match("open_app", {"app_name": pretty}, _Say(f"Opening {pretty}."), f"open {pretty}")


_MATCHERS = (_Time, _Volume, _Brightness, _Phrase, _OpenApp)


def _Rule(heard: str, names: tuple[str, ...]) -> Optional[Match]:
    """A correction kept as a rule (Brain/Rules.py): "from now on, call me
    boss", "stop calling me boss". Checked before the negation test, because
    "never" and "don't" are common in rules."""
    from Brain import Rules
    raw = (heard or "").strip()
    for n in names:
        n = (n or "").strip()
        if n:
            raw = re.sub(rf"^(hey |ok |okay )?{re.escape(n)}\b[,.!]? *", "", raw, flags=re.IGNORECASE)
    rule = Rules.FromSpeech(raw)
    if rule:
        def Kept(result: str) -> str:
            if result.startswith("Kept"):
                return "Got it. I'll do that from now on."
            if result.startswith("Already"):
                return "I already do that."
            return "I couldn't keep that as a rule."
        args = {"rule": rule}
        if rule.lower().startswith("call me "):
            # A new name replaces any rule about what to call the user.
            args["replaces"] = rule.rstrip(".").lower()
        return Match("learn_rule", args, Kept, "new rule")
    which = Rules.DroppedInSpeech(raw)
    if which and which.startswith("call me "):
        # "Stop calling me sir": sir may be the built-in default rather than a
        # rule, so it becomes a rule of its own (replacing "call me sir" if
        # there was one) instead of "I don't have a rule like that".
        title = which[len("call me "):].strip()
        return Match("learn_rule", {"rule": f"Do not call the user {title}.", "replaces": which},
                     lambda r: "Okay, I won't call you that any more." if r.startswith(("Kept", "Already"))
                     else "I couldn't keep that as a rule.", "drop rule")
    if which:
        return Match("learn_rule", {"replaces": which},
                     lambda r: "Okay, I won't do that any more." if r.startswith("Removed")
                     else "I don't have a rule like that.", "drop rule")
    return None


def Find(heard: str, names: tuple[str, ...] = ("ultron",)) -> Optional[Match]:
    """The command in `heard`, or None when it should go to the model."""
    rule = _Rule(heard, names)
    if rule is not None:
        return rule
    t = Normalize(heard, names)
    if not t or len(t.split()) > MAX_WORDS:
        return None
    if _NEGATION.search(t):
        return None
    for m in _MATCHERS:
        hit = m(t)
        if hit is not None:
            return hit
    return None
