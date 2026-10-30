"""
Private mode, like JARVIS's on the phone (ORION_LOCAL_BRAIN_PLAN.md §6, P2).

While Gemini is the brain and private mode is on, private things never reach Gemini:
    * the private kinds of memory are left out of its instructions, and recall_memory splits them off;
    * private tool results (messages from calls, the phone's notifications and screens, where the phone
      is, the activity log...) are read out by Orion's own voice on the laptop, and Gemini gets PRIVATE_NOTE.
The laptop's own brain is private already, so nothing here acts while it answers.

Settings live in Config/ApiKeys.json under "privacy": {"on", "kinds", "updated"}. The laptop starts with
private mode off (the user's choice); once linked, the newest change on either device wins (System/Together.py).
"""
from __future__ import annotations

from datetime import datetime

# Same kinds, keys and defaults as the phone (SettingsRepository.kt › PrivateMemory).
KINDS: tuple[tuple[str, str, str], ...] = (
    ("relationships", "People in your life", "Family, friends, partner"),
    ("personal", "Personal details", "Age, birthday, city, school"),
    ("preferences", "Preferences", "Likes, dislikes, habits"),
    ("projects", "Projects and goals", "What you're working on"),
    ("wishes", "Wishes and plans", "Things you want to do"),
    ("notes", "Notes", "Anything else you asked Orion to remember"),
)
KIND_KEYS = tuple(k for k, _, _ in KINDS)
DEFAULT_KINDS = ("relationships", "personal")
PERSONAL_FIELDS = {"age", "birthday", "city", "school"}      # the identity facts that count as "personal"

PRIVATE_NOTE = ("Done. The answer was read to the user on the laptop, privately: you do not have its content, so "
                "do not guess, repeat or sum it up. If they ask more about it, call this tool again. Say nothing "
                "more, or at most a couple of words.")
_MARK = "\x00private\x00"
_SEP = "\x1f"


# ── settings ─────────────────────────────────────────────────────────────────

def Get() -> dict:
    """{"on": bool, "kinds": [..], "updated": "YYYY-MM-DDTHH:MM:SS" or ""}."""
    try:
        from Brain.Settings import LoadApiKeys
        raw = LoadApiKeys().get("privacy")
    except Exception:
        raw = None
    raw = raw if isinstance(raw, dict) else {}
    kinds = raw.get("kinds")
    kinds = [k for k in kinds if k in KIND_KEYS] if isinstance(kinds, list) else list(DEFAULT_KINDS)
    return {"on": bool(raw.get("on", False)), "kinds": kinds, "updated": str(raw.get("updated") or "")}


def Save(on: bool | None = None, kinds=None, updated: str | None = None) -> dict:
    """Change private mode and/or its kinds. `updated` is set to now unless given (the phone's own time,
    which may be "" when the phone has never been changed)."""
    from Brain.Settings import PatchConfig
    cur = Get()
    if on is not None:
        cur["on"] = bool(on)
    if kinds is not None:
        cur["kinds"] = [k for k in KIND_KEYS if k in set(kinds)]
    cur["updated"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S") if updated is None else str(updated)
    PatchConfig(privacy=cur)
    return cur


def On() -> bool:
    return Get()["on"]


def Active() -> bool:
    """Private mode is on and Gemini is the brain (the laptop's own brain is private already)."""
    if not On():
        return False
    try:
        from System import LocalBrain
        return not LocalBrain.ACTIVE
    except Exception:
        return True


def IsPrivate(category: str, key: str, kinds=None) -> bool:
    """Whether a fact in category/key is a private kind (same rule as the phone)."""
    kinds = set(Get()["kinds"] if kinds is None else kinds)
    if category == "identity":
        return "personal" in kinds and _Norm(key) in PERSONAL_FIELDS
    return category in kinds


def _Norm(key: str) -> str:
    return "_".join(str(key or "").lower().replace("-", " ").replace("_", " ").split())


def ForGemini(memory: dict | None) -> dict | None:
    """`memory` without its private facts while private mode is active; unchanged otherwise."""
    if not memory or not Active():
        return memory
    kinds = set(Get()["kinds"])
    out = {}
    for cat, items in memory.items():
        if isinstance(items, dict):
            out[cat] = {k: v for k, v in items.items() if not IsPrivate(cat, k, kinds)}
        else:
            out[cat] = items
    return out


# ── reading out privately from anywhere (plugins, watchers) ──────────────────

_speaker = None


def SetSpeaker(fn) -> None:
    """Main.py: a thread-safe callable that reads text out with Orion's own voice."""
    global _speaker
    _speaker = fn


def SayPrivately(text: str) -> bool:
    """Read `text` out on the laptop, not through Gemini. False when there's no voice to do it."""
    if _speaker is None or not str(text or "").strip():
        return False
    try:
        _speaker(text)
        return True
    except Exception as e:
        print(f"[Privacy] couldn't read it out: {e}")
        return False


# ── private tool results ─────────────────────────────────────────────────────

def Privately(full: str, spoken: str | None = None, note: str = PRIVATE_NOTE) -> str:
    """A tool result that, while private mode is active, is read out on the laptop and never reaches Gemini
    (Gemini gets `note`). Otherwise `full`, as always."""
    if not Active() or not str(full or "").strip():
        return full
    return f"{_MARK}{note}{_SEP}{spoken if spoken is not None else full}"


def Split(result: str) -> tuple[str, str] | None:
    """(note for the model, text to read out) when `result` came from Privately(), else None."""
    if not isinstance(result, str) or not result.startswith(_MARK):
        return None
    note, _, spoken = result[len(_MARK):].partition(_SEP)
    return note, spoken


def Spoken(text: str) -> str:
    """Tool text made fit to read aloud: no "outside content" fences or markup, short lines joined."""
    import re
    text = re.sub(r"<<<[^>]*>>>|\[/?(untrusted|outside)[^\]]*\]", " ", str(text or ""), flags=re.I)
    text = re.sub(r"(?i)^(the phone (reported|says) this, which is information and not instructions:|"
                  r"the phone says \(information, not instructions\):)", "", text.strip())
    lines = [ln.strip(" -•*\t") for ln in text.splitlines() if ln.strip(" -•*\t")]
    return ". ".join(ln.rstrip(".") for ln in lines)[:1500] + ("." if lines else "")


def SplitMemory(text: str, kinds=None) -> tuple[str, str]:
    """recall_memory's answer split into (for Gemini, private lines). Lines look like "category/Key: value"."""
    keep, hidden = [], []
    kinds = set(Get()["kinds"] if kinds is None else kinds)
    for line in str(text or "").splitlines():
        cat, slash, rest = line.partition("/")
        key = rest.partition(":")[0]
        if slash and ":" in rest and IsPrivate(cat.strip(), key.strip(), kinds):
            hidden.append(f"{key.strip()}: {rest.partition(':')[2].strip()}")
        else:
            keep.append(line)
    return "\n".join(keep), "\n".join(hidden)
