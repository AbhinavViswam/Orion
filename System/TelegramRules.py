"""
The rules of Orion's Telegram bot, with no network and no Qt in them (the same rules as JARVIS's remote on the
phone, jarvis-android/.../remote/RemoteRules.kt): who is listened to, what is too old to act on, what a "yes"
means, and how Telegram's answers are read.

The safety property: only the one paired Telegram account is ever obeyed, and only in its private chat with the
bot. Telegram vouches for the sender's id, so a message can't be made to look like the owner's.
"""
from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Optional

# A command older than this is not run: the laptop was asleep or offline when it was sent, and "shut down" or
# "send her the file" arriving hours late is worse than not arriving.
STALE_S = 5 * 60
# Telegram's own limit is 4096 characters a message.
MAX_MESSAGE = 4000
# Wrong guesses at the pairing code before it is withdrawn.
MAX_WRONG_CODES = 5
# Messages listed by LateSummary; the rest are counted.
LATE_SHOWN = 10
# Big enough to read, small enough to fetch quickly.
PHOTO_EDGE = 1280

# The commands Telegram lists when "/" is typed in the chat, in that order.
MENU = [
    ("status", "Whether Orion is running, and how"),
    ("mute", "Turn the laptop's microphone off"),
    ("unmute", "Turn the laptop's microphone on"),
    ("stop", "Stop Orion talking on the laptop"),
    ("screenshot", "What's on the laptop's screen now"),
    ("calls", "Messages the call secretary took"),
    ("new", "Start a new conversation here"),
    ("undo", "Reverse the last thing Orion did"),
    ("reconnect", "A fresh voice connection, if Orion has gone quiet"),
    ("help", "What you can do here"),
]
BUILTINS = {"/start"} | {"/" + c for c, _ in MENU}

HELP = ("Write to me as you'd talk to me, and I'll do it on the laptop.\n"
        "/status — whether I'm running\n"
        "/mute, /unmute — the laptop's microphone; /stop — stop me talking\n"
        "/screenshot — the laptop's screen, as a photo\n"
        "/calls — messages the call secretary took\n"
        "/new — start a new conversation here\n"
        "/undo — reverse the last thing I did, where it can be\n"
        "/reconnect — a fresh voice connection, if I've gone quiet\n"
        "Send a photo or a file to ask me about it; write your question as its caption.")


@dataclass
class Incoming:
    """One message sent to the bot, as much of it as Orion reads."""
    update_id: int
    from_id: int
    from_name: str
    private: bool                  # a one-to-one chat with the bot: groups and channels are never listened to
    bot: bool
    text: Optional[str]            # None for a sticker or a voice note; a photo's or file's words are its caption
    date_s: int                    # when Telegram received it, in seconds since 1970
    photo: Optional[str] = None    # Telegram's file id of the size to fetch
    document: Optional[dict] = None  # {"id", "name", "size", "mime"}


@dataclass
class Tap:
    """A tap on a button under one of the bot's messages."""
    query_id: str
    from_id: int
    private: bool
    message_id: int
    data: str                      # what the button carries: never shown, never trusted beyond what it's checked against


@dataclass
class Updates:
    """What Telegram answered to "anything new?". kind: ok | slow | bad_token | elsewhere | failed."""
    kind: str
    messages: list = field(default_factory=list)
    taps: list = field(default_factory=list)
    last_id: Optional[int] = None  # the highest update id seen, even of updates that weren't messages
    seconds: int = 0               # slow: ask again after this long


@dataclass
class Verdict:
    """What to do with one message.

    kind: ignore | wrong_code | pair | stale | not_text | builtin | command | photo | document."""
    kind: str
    text: str = ""                 # builtin: the command; command / photo / document: the words
    minutes: int = 0               # stale
    file: Optional[dict] = None    # photo: {"id"}; document: {"id", "name", "size", "mime"}


def GetVerdict(m: Incoming, paired_id: Optional[int], code: Optional[str], now_s: int) -> Verdict:
    if not m.private or m.bot:
        return Verdict("ignore")
    text = (m.text or "").strip()
    if code is not None and text == code:
        return Verdict("pair")
    if paired_id is None or m.from_id != paired_id:
        # "/start" is what Telegram sends when someone first opens the bot: not a guess at the code.
        if code is not None and text and not text.startswith("/"):
            return Verdict("wrong_code")
        return Verdict("ignore")
    if now_s - m.date_s > STALE_S:
        return Verdict("stale", minutes=(now_s - m.date_s) // 60)
    if m.photo:
        return Verdict("photo", text=text, file={"id": m.photo})
    if m.document:
        return Verdict("document", text=text, file=dict(m.document))
    if not text:
        return Verdict("not_text")
    # "/status@my_bot" is how Telegram writes a command picked from the menu.
    command = text.split(" ", 1)[0].split("@", 1)[0].lower()
    if command in BUILTINS:
        return Verdict("builtin", text=command)
    return Verdict("command", text=text)


def HowLong(minutes: int) -> str:
    if minutes < 60:
        return "1 minute" if minutes == 1 else f"{minutes} minutes"
    if minutes < 48 * 60:
        h = minutes // 60
        return "1 hour" if h == 1 else f"{h} hours"
    return f"{minutes // (24 * 60)} days"


def LateSummary(late: list) -> str:
    """The one reply to everything that reached the laptop too late to act on: `late` is (words or None, minutes
    late) for each message, oldest first."""
    if len(late) == 1:
        return (f"That message reached the laptop {HowLong(late[0][1])} late, so I haven't acted on it. "
                f"Send it again if you still want it.")

    def Line(text):
        if text is None or not str(text).strip():
            return "• (not text)"
        t = str(text).strip().replace("\n", " ")
        return "• " + (t[:59] + "…" if len(t) > 60 else t)

    shown = "\n".join(Line(t) for t, _ in late[:LATE_SHOWN])
    more = f"\n…and {len(late) - LATE_SHOWN} more" if len(late) > LATE_SHOWN else ""
    return (f"{len(late)} messages reached the laptop late (the oldest {HowLong(max(m for _, m in late))}), "
            f"so I haven't acted on any of them:\n{shown}{more}\n\nSend again whatever you still want.")


_YES = {"yes", "y", "ok", "okay", "confirm", "do it", "go ahead", "yes please", "sure"}
_NO = {"no", "n", "cancel", "stop", "don't", "dont", "no thanks"}


def YesNo(text: str) -> Optional[bool]:
    """The answer to a waiting confirmation: True, False, or None when it is neither."""
    t = str(text or "").strip().lower().rstrip(".!")
    if t in _YES:
        return True
    if t in _NO:
        return False
    return None


def ConfirmButtons(pending_id: int) -> str:
    """The Yes and No buttons under a confirmation. Each carries the confirmation's id, so a tap answers that one
    confirmation and no other."""
    return json.dumps({"inline_keyboard": [[
        {"text": "Yes, do it", "callback_data": f"yes:{pending_id}"},
        {"text": "No", "callback_data": f"no:{pending_id}"},
    ]]})


def TapAnswer(data: str) -> Optional[tuple[int, bool]]:
    """The confirmation a tap answers and how: (id, yes), or None when it isn't one of ConfirmButtons."""
    head, _, tail = str(data or "").partition(":")
    if head not in ("yes", "no"):
        return None
    try:
        return int(tail), head == "yes"
    except ValueError:
        return None


def Chunks(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """`text` in pieces Telegram will take, broken at a line or a space where there is one."""
    out, rest = [], str(text or "").strip()
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n")
        if cut <= limit // 2:
            cut = window.rfind(" ")
        if cut <= limit // 2:
            cut = limit
        out.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        out.append(rest)
    return out


def NewCode() -> str:
    """A six-digit pairing code."""
    return f"{secrets.randbelow(1_000_000):06d}"


_TOKEN = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,}$")


def LooksLikeToken(text: str) -> bool:
    """What a bot token looks like: the bot's number, a colon, and a long secret."""
    return bool(_TOKEN.match(str(text or "").strip()))


def MenuJson() -> str:
    return json.dumps([{"command": c, "description": d} for c, d in MENU])


# ── reading Telegram's answers ───────────────────────────────────────────────

def _Json(body) -> Optional[dict]:
    try:
        root = json.loads(body)
        return root if isinstance(root, dict) else None
    except Exception:
        return None


def BestPhoto(sizes: list) -> Optional[str]:
    """Telegram keeps each photo at several sizes. The largest no bigger than PHOTO_EDGE on its long side, or the
    smallest when every size is bigger. `sizes` is [(file_id, width, height)]."""
    by_area = sorted(sizes, key=lambda s: s[1] * s[2])
    fit = [s for s in by_area if max(s[1], s[2]) <= PHOTO_EDGE]
    pick = fit[-1] if fit else (by_area[0] if by_area else None)
    return pick[0] if pick else None


def ParseUpdates(status: int, body) -> Updates:
    root = _Json(body)
    if root is None or root.get("ok") is not True:
        code = (root or {}).get("error_code") or status
        if code in (401, 404):
            return Updates("bad_token")
        if code == 409:
            return Updates("elsewhere")
        if code == 429:
            retry = ((root or {}).get("parameters") or {}).get("retry_after") or 5
            return Updates("slow", seconds=int(retry))
        return Updates("failed")
    results = root.get("result")
    if not isinstance(results, list):
        return Updates("failed")
    out = Updates("ok")
    for update in results:
        if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
            continue
        uid = update["update_id"]
        if out.last_id is None or uid > out.last_id:
            out.last_id = uid
        q = update.get("callback_query")
        if isinstance(q, dict):
            tap = _ParseTap(q)
            if tap:
                out.taps.append(tap)
            continue
        m = update.get("message")
        if not isinstance(m, dict):
            continue
        frm, chat = m.get("from") or {}, m.get("chat") or {}
        if not isinstance(frm.get("id"), int):
            continue
        name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x).strip() \
            or str(frm.get("username") or "")
        photo = None
        if isinstance(m.get("photo"), list):
            photo = BestPhoto([(s.get("file_id"), int(s.get("width") or 0), int(s.get("height") or 0))
                               for s in m["photo"] if isinstance(s, dict) and s.get("file_id")])
        doc = None
        d = m.get("document")
        if isinstance(d, dict) and d.get("file_id"):
            doc = {"id": d["file_id"], "name": str(d.get("file_name") or "file"),
                   "size": int(d.get("file_size") or 0), "mime": str(d.get("mime_type") or "")}
        out.messages.append(Incoming(
            update_id=uid, from_id=frm["id"], from_name=name[:60],
            private=chat.get("type") == "private", bot=frm.get("is_bot") is True,
            text=m.get("text") if m.get("text") is not None else m.get("caption"),
            date_s=int(m.get("date") or 0), photo=photo, document=doc))
    return out


def _ParseTap(q: dict) -> Optional[Tap]:
    m = q.get("message") or {}
    frm = q.get("from") or {}
    if not (q.get("id") and isinstance(frm.get("id"), int) and isinstance(m.get("message_id"), int)
            and q.get("data") is not None):
        return None
    return Tap(query_id=str(q["id"]), from_id=frm["id"], private=(m.get("chat") or {}).get("type") == "private",
               message_id=m["message_id"], data=str(q["data"]))


def ParseResult(body) -> Optional[dict]:
    """The "result" of a call that worked, or None."""
    root = _Json(body)
    if root is None or root.get("ok") is not True:
        return None
    r = root.get("result")
    return r if isinstance(r, dict) else {}


def ParseBotName(body) -> Optional[str]:
    r = ParseResult(body)
    return str(r["username"]) if r and r.get("username") else None


def ParseSentId(body) -> Optional[int]:
    r = ParseResult(body)
    return r.get("message_id") if r and isinstance(r.get("message_id"), int) else None


def ParseFilePath(body) -> Optional[str]:
    r = ParseResult(body)
    return str(r["file_path"]) if r and r.get("file_path") else None
