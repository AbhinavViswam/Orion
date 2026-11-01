"""
Orion and JARVIS together (Phase 4 of the bridge). News from the phone arrives as `phone_event` tasks through
JARVIS Link (System/PhoneBridge.py); each kind has its own switch in Settings › Plugins › JARVIS Link (phone).

  D1 phone_listening / phone_idle   both hear, the one you name answers. "Hey JARVIS…" at the desk: Orion lets
                                    that turn pass (its reply is dropped, the words stay as context) until
                                    phone_idle or 30 s. No microphone is ever held.
  D2 phone_call                     ringing or in a call: Orion stops talking and mutes; unmutes after, if it was
                                    the one that muted.
  D3 memory_offer / memory_request  one shared memory: every kind, merged automatically both ways on connect and
                                    on every change; no duplicates, the newer timestamp wins, deletions carry over,
                                    and each entry says which assistant learned it (source orion | jarvis).
  D4 briefing_handoff / _done       one morning briefing: whichever gives it tells the other.
  D5 phone_state                    the phone's battery and unread count in Orion's top bar.
  D6 activity                       a short shared log (50 lines: time, device, one line, no transcripts).
  D7 home                           the phone left the home Wi-Fi and nobody is at the laptop: Orion mutes.
"""
from __future__ import annotations

import json
import threading
import time
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from Brain.Settings import CONFIG_DIR

PHONE_TURN_S = 30
ACTIVITY_FILE = CONFIG_DIR / "Activity.json"
ACTIVITY_KEEP = 50
SYNC_FILE = CONFIG_DIR / "PhoneTogether.json"     # briefing dates, memory-sync state (git-ignored, Config/*.json)
CATEGORIES = ("identity", "preferences", "projects", "relationships", "wishes", "notes", "rules")
AWAY_FOR_HOME_S = 300
NAMESPACE = "phone_link"
SWITCHES = {"phone_listening": "one_voice", "phone_idle": "one_voice", "phone_call": "call_quiet",
            "briefing_handoff": "one_briefing", "briefing_done": "one_briefing", "phone_state": "phone_state",
            "activity": "activity", "home": "home", "memory_offer": "memory", "memory_request": "memory",
            "privacy": "privacy"}

LOW_BATTERY = 15
GOODNIGHT = re.compile(r"\bgood\s*night\b|\bgoodnite\b|\bnight night\b", re.I)

_lock = threading.Lock()


def Speakable(items: list[dict]) -> tuple[list, int]:
    """Memory items as (key, value) pairs to say out loud, and how many were left out: the private kinds while
    private mode is on are only counted, never named."""
    try:
        from Brain import Privacy
        p = Privacy.Get()
        hidden_kinds = set(p.get("kinds") or []) if p.get("on") else set()
    except Exception:
        hidden_kinds = set()
    pairs, hidden = [], 0
    for i in items:
        if i.get("category") in hidden_kinds:
            hidden += 1
        elif i.get("key") and i.get("value"):
            pairs.append((str(i["key"]).replace("_", " "), str(i["value"])[:80]))
    return pairs, hidden


def On(key: str) -> bool:
    try:
        from Brain.Settings import GetPluginEnabled, GetPluginSetting
        return GetPluginEnabled("phone_link") and bool(GetPluginSetting(NAMESPACE, key, True))
    except Exception:
        return False


def _Load(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def _Save(path: Path, data) -> None:
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(path)


def State() -> dict:
    d = _Load(SYNC_FILE, {})
    return d if isinstance(d, dict) else {}


def SetState(**kw) -> None:
    d = State()
    d.update(kw)
    _Save(SYNC_FILE, d)


def Today() -> str:
    return date.today().isoformat()


# ── D6: the shared activity log ─────────────────────────────────────────────

def Activity() -> list[dict]:
    d = _Load(ACTIVITY_FILE, [])
    return d if isinstance(d, list) else []


def AddActivity(device: str, line: str, at: Optional[float] = None) -> None:
    line = " ".join(str(line or "").split())[:200]
    if not line:
        return
    log = Activity() + [{"at": int(at or time.time()), "device": device, "line": line}]
    _Save(ACTIVITY_FILE, log[-ACTIVITY_KEEP:])


def ActivityWords(device: str = "") -> str:
    rows = [r for r in Activity() if not device or r.get("device") == device]
    if not rows:
        return "Nothing in the shared activity log yet."
    names = {"phone": "phone", "laptop": "laptop"}
    lines = [f"{time.strftime('%a %H:%M', time.localtime(r.get('at', 0)))} · {names.get(r.get('device'), '?')} · "
             f"{r.get('line', '')}" for r in rows[-20:]]
    return "Activity, oldest first:\n" + "\n".join(lines)


# ── D3: one shared memory ───────────────────────────────────────────────────
# Kept the same on both sides, automatically (the user's choice, 2026-10-30): every kind, no duplicates (same kind
# and same key, ignoring case and spaces), the newer value wins by its timestamp, deletions carry over (with their
# own timestamp, so they don't come back). Each entry also says which assistant learned its current value:
# "source" orion | jarvis (missing = orion, Orion's own). Orion's memory keeps only a date per entry, so the exact
# times are kept in PhoneTogether.json ("stamps").

TOMBSTONE_DAYS = 30


def NormKey(key: str) -> str:
    return re.sub(r"[\s_\-]+", "_", str(key or "").strip().lower())


def _Same(a: str, b: str) -> bool:
    return " ".join(str(a or "").split()).lower() == " ".join(str(b or "").split()).lower()


def _Time(t: str) -> str:
    t = str(t or "")
    return t + "T00:00:00" if len(t) == 10 else t


def Now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def Source(entry) -> str:
    return "jarvis" if isinstance(entry, dict) and entry.get("source") == "jarvis" else "orion"


def MemoryItems(memory: Optional[dict] = None) -> list[dict]:
    """Every entry Orion shares, with its best-known time and where it came from."""
    from Brain.Memory import EntryValue, LoadMemory
    memory = memory if memory is not None else LoadMemory()
    stamps = State().get("stamps", {})
    out = []
    for cat in CATEGORIES:
        for key, entry in (memory.get(cat) or {}).items():
            value = EntryValue(entry)
            if value:
                day = str(entry.get("updated") or "") if isinstance(entry, dict) else ""
                out.append({"category": cat, "key": str(key), "value": value, "source": Source(entry),
                            "updated": stamps.get(f"{cat}/{NormKey(key)}") or _Time(day)})
    return out


def Tombstones() -> list[dict]:
    """Orion's own deletions of the last 30 days, so the phone drops them too (even if it was away)."""
    cut = (datetime.now() - timedelta(days=TOMBSTONE_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    out = []
    for k, when in State().get("tombstones", {}).items():
        if when >= cut:
            cat, _, key = k.partition("/")
            out.append({"category": cat, "key": key, "value": "", "updated": when, "deleted": True, "source": "orion"})
    return out


LAST_LEARNED: list[dict] = []      # the last Merge's new values from the phone (for the talk, System/Talk.py)
LAST_CONFLICTS: list[dict] = []    # ...and where both changed the same thing within a day, differently
CONFLICT_S = 24 * 3600


def _Close(a: str, b: str) -> bool:
    try:
        return abs((datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds()) < CONFLICT_S
    except Exception:
        return False


def Merge(items: list[dict]) -> int:
    """What the phone sent, merged into Orion's memory. Returns how many entries changed."""
    from Brain.Memory import EntryValue, LoadMemory, SaveMemory
    memory = LoadMemory()
    st = State()
    stamps, tombs, from_phone = st.get("stamps", {}), st.get("tombstones", {}), st.get("from_phone", {})
    changed = 0
    learned, conflicts = [], []
    for it in items:
        cat = str(it.get("category"))
        if cat not in CATEGORIES or not it.get("key"):
            continue
        k = f"{cat}/{NormKey(it['key'])}"
        theirs = _Time(it.get("updated"))
        section = memory.setdefault(cat, {})
        have_key = next((key for key in section if NormKey(key) == NormKey(it["key"])), None)
        have = section.get(have_key) if have_key is not None else None
        mine = stamps.get(k) or _Time(have.get("updated") if isinstance(have, dict) else "")
        if it.get("deleted"):
            if have_key is not None and mine <= theirs:
                del section[have_key]
                changed += 1
            stamps.pop(k, None)
            from_phone[k] = None
            continue
        if tombs.get(k) and tombs[k] >= theirs:
            continue                               # Orion deleted it after the phone last changed it
        value = str(it.get("value") or "")
        src = "orion" if it.get("source") == "orion" else "jarvis"
        if have_key is not None and _Same(EntryValue(have), value):
            from_phone[k] = EntryValue(have)       # the same thing: no duplicate, its source stays
            continue
        if have_key is not None and theirs <= mine:
            continue                               # Orion's is newer (a tie keeps Orion's)
        # Orion set it itself (an exact time is known) less than a day before JARVIS changed it: ask the user.
        if have_key is not None and src == "jarvis" and Source(have) == "orion" and stamps.get(k) \
                and _Close(stamps[k], theirs):
            conflicts.append({"category": cat, "key": str(it["key"]), "mine": EntryValue(have), "theirs": value})
        else:
            learned.append({"category": cat, "key": str(it["key"]), "value": value})
        entry = {"value": value, "updated": theirs[:10] or datetime.now().strftime("%Y-%m-%d")}
        if src == "jarvis":
            entry["source"] = "jarvis"
        section[have_key if have_key is not None else str(it["key"])] = entry
        stamps[k] = theirs or Now()
        tombs.pop(k, None)
        from_phone[k] = value
        changed += 1
    if changed:
        SaveMemory(memory)
    SetState(stamps=stamps, tombstones=tombs, from_phone=from_phone)
    LAST_LEARNED[:] = learned
    LAST_CONFLICTS[:] = conflicts
    return changed


class Together:
    """Handles the phone's news for one running Orion (`live` is Main.OrionLive; tests pass a stand-in)."""

    def __init__(self, live, send: Optional[Callable[..., bool]] = None):
        self.live = live
        self.send = send or self._Send            # an event for the phone: send(type, **fields)
        self.phone_turn_until = 0.0
        self.call_muted = False
        self.home_muted = False
        self.phone_state: dict = {}
        self._offered: dict[str, str] = {}
        self.talk = None                          # System/Talk.py: talking out loud with JARVIS
        self.left_home_at = 0.0

    def Start(self) -> None:
        """Every half minute: offer memory items Orion learned (after the first sync)."""
        def Loop():
            while True:
                time.sleep(30)
                try:
                    self.OfferChanges()
                except Exception as e:
                    print(f"[Together] memory offer failed: {e}")
        threading.Thread(target=Loop, name="Together", daemon=True).start()

    @staticmethod
    def _Send(kind: str, **fields) -> bool:
        from System import PhoneBridge
        try:
            return bool(PhoneBridge.Sealed("event", type=kind, **fields).get("ok"))
        except Exception:
            return False

    def SendLater(self, kind: str, **fields) -> None:
        threading.Thread(target=lambda: self.send(kind, **fields), daemon=True).start()

    # D1
    def PhoneTurn(self) -> bool:
        return time.monotonic() < self.phone_turn_until

    def Handle(self, ev: dict) -> None:
        kind = str(ev.get("type"))
        if self.talk is not None:
            self.talk.last_phone_event = time.time()   # the phone is there: JARVIS is running
            if kind.startswith("talk_"):
                self.talk.Handle(ev)
                return
        if not On(SWITCHES.get(kind, "")):
            return
        fn = getattr(self, "On_" + kind, None)
        if fn:
            try:
                fn(ev)
            except Exception as e:
                print(f"[Together] {kind} failed: {e}")

    def On_phone_listening(self, ev) -> None:
        self.phone_turn_until = time.monotonic() + PHONE_TURN_S
        if getattr(self.live, "_is_speaking", False):
            self.live.interrupt()

    def On_phone_idle(self, ev) -> None:
        self.phone_turn_until = 0.0

    # D2
    def On_phone_call(self, ev) -> None:
        ui = self.live.ui
        state = str(ev.get("state"))
        if state in ("ringing", "in_call"):
            if getattr(self.live, "_is_speaking", False):
                self.live.interrupt()
            if not ui.muted:
                who = str(ev.get("caller") or "").strip()
                ui.SetMicMuted(True, f"Phone call{' — ' + who if who else ''}")
                self.call_muted = True
        elif state == "idle" and self.call_muted:
            self.call_muted = False
            ui.SetMicMuted(False, "")

    # D4
    def On_briefing_done(self, ev) -> None:
        if str(ev.get("date") or "") == Today():
            SetState(phone_briefed=Today())

    def On_briefing_handoff(self, ev) -> None:
        give = getattr(self.live, "GiveBriefingNow", None)
        if callable(give) and State().get("orion_briefed") != Today():
            # Out loud first (JARVIS adds the phone's news), then the briefing; at once if they can't talk now.
            if self.talk is not None:
                self.talk.Moment("briefing", self.talk.PhoneFacts(), after=give)
            else:
                give()

    def ShouldBrief(self) -> bool:
        """At Orion's start: not if the phone gave today's briefing already."""
        return not (On("one_briefing") and State().get("phone_briefed") == Today())

    def Briefed(self) -> None:
        SetState(orion_briefed=Today())
        if On("one_briefing"):
            self.SendLater("briefing_done", date=Today())

    # D5
    def On_phone_state(self, ev) -> None:
        before = self.phone_state.get("battery")
        self.phone_state = {"battery": ev.get("battery"), "charging": bool(ev.get("charging")),
                            "unread": ev.get("unread"), "at": time.time()}
        b = ev.get("battery")
        # Low and not charging, just now: they mention it.
        if self.talk is not None and isinstance(b, int) and b <= LOW_BATTERY and not ev.get("charging") \
                and (before is None or before > LOW_BATTERY):
            self.talk.Moment("battery", self.talk.PhoneFacts())

    def PhoneRing(self) -> Optional[tuple[float, str, str]]:
        """(percent, text, tooltip) for the top bar, or None when the phone hasn't said for 15 minutes."""
        s = self.phone_state
        if not s or time.time() - s.get("at", 0) > 900 or s.get("battery") is None:
            return None
        pct = float(s["battery"])
        unread = int(s.get("unread") or 0)
        tip = f"Phone: {pct:.0f}%{', charging' if s.get('charging') else ''}" + (f", {unread} unread" if unread else "")
        return pct, f"{pct:.0f}", tip

    # D6
    def On_activity(self, ev) -> None:
        line = str(ev.get("line") or "")
        AddActivity("phone", line, ev.get("at"))
        if self.talk is not None and line.strip():
            if not self.Goodnight(line):
                self.talk.Moment("activity", {"lines": [" ".join(line.split())[:120]]})

    def Goodnight(self, text: str) -> bool:
        """"Good night" said to either, in the evening, once a day: they wish each other good night."""
        h = datetime.now().hour
        if self.talk is None or not GOODNIGHT.search(text or "") or not (h >= 20 or h < 4):
            return False
        day = (date.today() - timedelta(days=1)).isoformat() if h < 4 else Today()
        if State().get("talk_goodnight") == day:
            return False
        SetState(talk_goodnight=day)
        today = [r.get("line", "") for r in Activity() if r.get("at", 0) >= time.time() - 16 * 3600][-6:]
        self.talk.Moment("goodnight", {**self.talk.PhoneFacts(), "today": today})
        return True

    def OnTurn(self, user_text: str) -> None:
        """A finished turn at the laptop: one line in the shared log, and the same line to the phone."""
        self.Goodnight(user_text)
        if self.PhoneTurn() or not user_text.strip() or not On("activity"):
            return
        line = " ".join(user_text.split())[:100]
        AddActivity("laptop", line)
        self.SendLater("activity", line=line, at=int(time.time()))

    # D7
    def On_home(self, ev) -> None:
        from System import PhoneBridge
        ui = self.live.ui
        if not ev.get("at_home"):
            self.left_home_at = time.time()
            if not ui.muted and PhoneBridge.IdleSeconds() >= AWAY_FOR_HOME_S:
                ui.SetMicMuted(True, "You left home")
                self.home_muted = True
            return
        if self.home_muted:
            self.home_muted = False
            ui.SetMicMuted(False, "")
        # Back home: they greet each other once you're at the laptop (it waits up to 3 hours for that). Only after
        # Orion saw the phone leave: JARVIS says "at home" each time it starts too.
        if self.talk is not None and self.left_home_at:
            left, self.left_home_at = self.left_home_at, 0.0
            laptop = [r.get("line", "") for r in Activity()
                      if r.get("device") == "laptop" and r.get("at", 0) >= int(left)][-5:]
            away = int((time.time() - left) / 60) if left else 0
            self.talk.Moment("welcome", {**self.talk.PhoneFacts(), "laptop": laptop, "away_min": away})

    # D3
    def On_memory_offer(self, ev) -> None:
        n = Merge(ev.get("items") or [])
        if ev.get("first"):
            self.OfferAll()                        # the phone sent everything: send everything back
        if n:
            try:
                self.live.ui.WriteLog(f"SYS: Memory synced with your phone — {n} updated")
            except Exception:
                pass
        # Out loud: what JARVIS learned (not the whole list sent on connecting), and any disagreement.
        if self.talk is not None and not ev.get("first"):
            pairs, hidden = Speakable(LAST_LEARNED)
            if pairs or hidden:
                self.talk.Moment("memory", {"from_jarvis": pairs, "hidden": hidden})
            for c in LAST_CONFLICTS[:1]:
                if not Speakable([c])[1]:
                    self.talk.Moment("conflict", {"category": c["category"], "key": c["key"].replace("_", " "),
                                                  "mine": c["mine"], "theirs": c["theirs"]})

    def On_memory_request(self, ev) -> None:
        self.OfferAll()

    # One private mode (ORION_LOCAL_BRAIN_PLAN.md P3)
    def On_privacy(self, ev) -> None:
        """The phone's private mode. The newest change wins; a laptop never changed by hand takes the phone's."""
        from Brain import Privacy
        mine, theirs = Privacy.Get(), str(ev.get("updated") or "")
        kinds = [k for k in (ev.get("kinds") or []) if k in Privacy.KIND_KEYS]
        if mine["updated"] and not (theirs and theirs > mine["updated"]):
            if mine["updated"] > theirs:
                self.OfferPrivacy()                # the laptop's is newer: the phone follows
            return
        on = bool(ev.get("on"))
        if on == mine["on"] and sorted(kinds) == sorted(mine["kinds"]):
            Privacy.Save(updated=theirs)
            return
        Privacy.Save(on=on, kinds=kinds, updated=theirs)
        try:
            self.live.ui.WriteLog(f"SYS: Private mode {'on' if on else 'off'}, as on your phone.")
            self.live.RequestReconnect(keep_context=True, reason="private mode from your phone")
        except Exception:
            pass

    def OfferPrivacy(self, now: bool = False) -> None:
        """The laptop's private mode, to the phone. `follows`: Orion keeps the private kinds from Gemini the
        same way, so the phone can send them in the memory sync. `now`: on this thread, not in the background."""
        if not On("privacy"):
            return
        from Brain import Privacy
        p = Privacy.Get()
        fields = dict(on=p["on"], kinds=p["kinds"], updated=p["updated"], follows=True)
        if now:
            self.send("privacy", **fields)
        else:
            self.SendLater("privacy", **fields)

    def OnConnected(self) -> None:
        """Orion and the phone just found each other through Link: private mode first (so the phone knows it may
        send every kind), then both send their whole memory. Orion asks for the phone's too, since the phone may
        not notice Orion restarted (it sends its own once per connection). In this order, on one thread."""
        def Run():
            self.OfferPrivacy(now=True)
            if On("memory"):
                self.send("memory_request")
                items = MemoryItems()
                self._offered = {f"{i['category']}/{NormKey(i['key'])}": i["value"] for i in items}
                self.send("memory_offer", items=items + Tombstones(), first=True)
        self.RunLater(Run)

    @staticmethod
    def RunLater(fn) -> None:
        threading.Thread(target=fn, name="Together-connect", daemon=True).start()

    def OfferAll(self) -> None:
        items = MemoryItems()
        self._offered = {f"{i['category']}/{NormKey(i['key'])}": i["value"] for i in items}
        self.SendLater("memory_offer", items=items + Tombstones(), first=True)

    def SyncNow(self) -> str:
        """'Sync memory with JARVIS': send everything and ask for the phone's."""
        if not self.send("memory_request"):
            return "I couldn't reach the phone through JARVIS Link."
        self.OfferAll()
        return "Syncing memory with JARVIS on your phone. Both will have the same things."

    def OfferChanges(self) -> int:
        """Every half minute: what Orion learned, changed or forgot since, to the phone (not what just came from
        the phone). The first look only remembers what's there."""
        if not On("memory"):
            return 0
        st = State()
        from_phone, stamps, tombs = st.get("from_phone", {}), st.get("stamps", {}), st.get("tombstones", {})
        now = {f"{i['category']}/{NormKey(i['key'])}": i for i in MemoryItems()}
        if not self._offered:
            self._offered = {k: i["value"] for k, i in now.items()}
            return 0
        fresh = []
        for k, it in now.items():
            if self._offered.get(k) != it["value"] and from_phone.get(k) != it["value"]:
                stamps[k] = Now()
                fresh.append({**it, "updated": stamps[k], "source": "orion"})
        for k in [k for k in self._offered if k not in now]:
            if from_phone.get(k, "") is not None:  # not a deletion that came from the phone
                tombs[k] = Now()
                cat, _, key = k.partition("/")
                fresh.append({"category": cat, "key": key, "value": "", "updated": tombs[k], "deleted": True,
                              "source": "orion"})
            stamps.pop(k, None)
        self._offered = {k: i["value"] for k, i in now.items()}
        SetState(stamps=stamps, tombstones=tombs)
        if fresh:
            self.SendLater("memory_offer", items=fresh, first=False)
            pairs, hidden = Speakable([i for i in fresh if not i.get("deleted")])
            if self.talk is not None and (pairs or hidden):
                self.talk.Moment("memory", {"from_orion": pairs, "hidden": hidden})
        return len(fresh)
