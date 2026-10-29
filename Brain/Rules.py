"""
Brain/Rules.py: learning from corrections.

When you correct how Ultron does something ("from now on, call me boss",
"never read my emails out loud", "stop saying sir"), the correction is kept as
a standing RULE and put into every conversation, so you say it once instead of
every time. The same design as JARVIS on the phone (memory/LearnedRules.kt and
tools/builtin/LearnRuleTool.kt).

Rules live in long-term memory under the "rules" category, so Settings ›
Memory shows them and Forget removes them. They are not silent: Ultron says
what it will do from now on, so a misheard rule is caught at once, and the
`undo` tool takes one back.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

CATEGORY = "rules"
MAX_IN_PROMPT = 10          # newest first, within PROMPT_CHARS
PROMPT_CHARS = 1_000
MAX_STORED = 25             # more than this: the oldest goes (and the result says so)
MAX_RULE_LENGTH = 200

_STOP = {"a", "an", "the", "to", "of", "in", "on", "at", "for", "and", "or", "it", "is", "be", "me",
         "my", "i", "you", "your", "that", "this", "with", "when", "please", "do", "don't", "dont",
         "not", "no", "never", "always"}
_RULE_WORDS = {"rule", "rules", "forget", "remove", "delete", "stop", "anymore", "about", "instead"}


# ── text ────────────────────────────────────────────────────────────────────

def _Words(s: str) -> list[str]:
    return [w for w in re.split(r"[^\w']+", (s or "").lower()) if w]


def _Stem(w: str) -> str:
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("ed"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _Meaningful(s: str) -> set[str]:
    out = set()
    for w in _Words(s):
        w = _Stem(w[:-2] if w.endswith("'s") else w)
        if len(w) > 1 and w not in _STOP and w not in _RULE_WORDS:
            out.add(w)
    return out


def Clean(rule: str) -> str:
    """One line, capitalised, ending with a full stop, capped in length."""
    r = re.sub(r"\s+", " ", rule or "").strip().strip("\"'“”").strip()
    if not r:
        return ""
    if len(r) > MAX_RULE_LENGTH:
        r = r[:MAX_RULE_LENGTH].rstrip() + "…"
    r = r[0].upper() + r[1:]
    return r if r[-1] in ".!?…" else r + "."


def KeyFor(rule: str) -> str:
    """A rule's key, from its words: "never_read_emails_aloud"."""
    words = [w for w in _Words(rule) if w not in _STOP] or _Words(rule)
    key = "_".join(re.sub(r"[^\w]", "", w) for w in words[:6]).strip("_")
    return key[:60] or "rule"


# ── stored rules ────────────────────────────────────────────────────────────

def All(memory: dict | None = None) -> list[dict]:
    """Every rule, newest first: [{'key', 'value', 'at'}]."""
    if memory is None:
        from Brain.Memory import LoadMemory
        memory = LoadMemory()
    out = []
    for key, entry in (memory.get(CATEGORY) or {}).items():
        if isinstance(entry, dict):
            value, at = str(entry.get("value", "")), float(entry.get("at", 0) or 0)
            if not at:
                try:
                    at = datetime.fromisoformat(str(entry.get("updated", ""))[:10]).timestamp()
                except ValueError:
                    at = 0.0
        else:
            value, at = str(entry), 0.0
        if value.strip():
            out.append({"key": key, "value": value, "at": at})
    return sorted(out, key=lambda r: r["at"], reverse=True)


def Closest(rules: list[dict], text: str) -> dict | None:
    """The stored rule `text` is about: the one sharing the most meaningful
    words, if it shares at least half of the shorter one's."""
    want = _Meaningful(text)
    if not want:
        return None
    best, best_score = None, 0.0
    for r in rules:
        have = _Meaningful(r["value"] + " " + r["key"].replace("_", " "))
        if not have:
            continue
        score = len(want & have) / max(1, min(len(want), len(have)))
        if score >= 0.5 and score > best_score:
            best, best_score = r, score
    return best


def PromptBlock(memory: dict | None = None) -> str:
    """The [YOUR RULES] block of the system prompt; empty when there are none."""
    rules = All(memory)
    if not rules:
        return ""
    lines, used = [], 0
    for r in rules[:MAX_IN_PROMPT]:
        line = f"- {r['value']}"
        if used + len(line) > PROMPT_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    return ("[YOUR RULES — the user taught you these. Always follow them, unless what the user "
            "asks right now says otherwise]\n" + "\n".join(lines) + "\n\n")


def _Put(memory: dict, key: str, value: str, at: float | None = None) -> None:
    memory.setdefault(CATEGORY, {})[key] = {
        "value": value, "updated": datetime.now().strftime("%Y-%m-%d"), "at": at or time.time()}


def Learn(rule: str = "", replaces: str = "") -> str:
    """Keep a rule, replace one it changes, or (rule empty) forget one. The
    result is written for the model: what happened, and what to say."""
    from Brain.Memory import LoadMemory, SaveMemory
    from Brain.Undo import PushUndo

    rule = Clean(rule)
    replaces = (replaces or "").strip()
    memory = LoadMemory()
    rules = All(memory)
    if replaces:
        old = Closest(rules, replaces)
        if old is None and rule:
            old = next((r for r in rules if r["value"].lower() == rule.lower()), None)
    else:
        old = next((r for r in rules if r["value"].lower() == rule.lower()
                    or r["key"] == KeyFor(rule)), None)

    if not rule:
        if not replaces:
            return "Nothing to keep: give the rule, or what to forget in 'replaces'."
        if old is None:
            return "No rule like that is stored. The rules you follow are in [YOUR RULES]."
        del memory[CATEGORY][old["key"]]
        SaveMemory(memory)
        PushUndo(f"forgot the rule: {old['value'].rstrip('.')}", lambda o=old: _Restore([o], []))
        return f"Removed the rule: {old['value']} Tell the user in a few words."

    if old is not None and old["value"].lower() == rule.lower():
        return f"Already a rule: {old['value']} Tell the user you already do that."
    if old is not None:
        memory[CATEGORY].pop(old["key"], None)
    key = KeyFor(rule)
    _Put(memory, key, rule)
    dropped = None
    kept = All(memory)
    if len(kept) > MAX_STORED:
        dropped = kept[-1]
        memory[CATEGORY].pop(dropped["key"], None)
    SaveMemory(memory)
    PushUndo(f"learned a rule: {rule.rstrip('.')}",
             lambda k=key, o=old, d=dropped: _Restore([x for x in (o, d) if x], [k]))
    out = f"Kept as a rule from now on: {rule}"
    if old:
        out += f" (it replaces: {old['value']})"
    if dropped:
        out += f" The oldest rule was forgotten to make room: {dropped['value']}"
    return out + " Confirm in one short sentence what you will do from now on."


def _Restore(put_back: list[dict], take_out: list[str]) -> str:
    from Brain.Memory import LoadMemory, SaveMemory
    memory = LoadMemory()
    for k in take_out:
        memory.get(CATEGORY, {}).pop(k, None)
    for r in put_back:
        _Put(memory, r["key"], r["value"], r.get("at") or None)
    SaveMemory(memory)
    return "Rule taken back." if take_out else "Rule back."


# ── said out loud ───────────────────────────────────────────────────────────

# Not "next time": "next time I'm at the shop, remind me…" is a reminder.
_SPOKEN = re.compile(
    r"^(?:(?:hey )?ultron,? )?(?:ok(?:ay)?,? |and |also |please )?(?:from now on|from today on(?:wards)?|"
    r"from now onwards|going forward|always remember to|remember to always|remember never to|"
    r"remember to never)\b,? (.+)$", re.IGNORECASE)
# "From now on, remind me at 7 every day" is a command, not a rule.
_COMMAND_START = re.compile(
    r"^(?:please )?(?:remind|set|wake|call(?! me\b)|phone|ring|message|text|send|open|turn|switch|"
    r"play|pause|start|stop the|book|order|search|find|show|tell me)\b", re.IGNORECASE)
_DROP_ADDRESS = re.compile(
    r"^(?:(?:hey )?ultron,? )?(?:ok(?:ay)?,? |please )?(?:stop calling me|don'?t call me|do not call me|"
    r"stop addressing me as) ([\w '-]{1,30}?)(?: any ?more| now)?$", re.IGNORECASE)
_DROP_RULE = re.compile(
    r"^(?:(?:hey )?ultron,? )?(?:ok(?:ay)?,? |please )?(?:forget|drop|remove|delete) (?:the|that|my) "
    r"rule (?:about |that |to )?(.+)$", re.IGNORECASE)


def FromSpeech(heard: str) -> str | None:
    """'from now on, call me boss' → 'Call me boss.' Only clear openings, so
    ordinary sentences never turn into rules. None when it is not one."""
    t = re.sub(r"\s+", " ", heard or "").strip().rstrip(".!?")
    m = _SPOKEN.match(t)
    if not m:
        return None
    body = m.group(1).strip().lstrip(",").strip()
    if len(_Words(body)) < 2 or (heard or "").strip().endswith("?"):
        return None
    if _COMMAND_START.search(body):
        return None
    return Clean(body)


def DroppedInSpeech(heard: str) -> str | None:
    """'stop calling me boss' → 'call me boss' (the words to find the rule by);
    'forget the rule about emails' → 'emails'. None when it is not one."""
    t = re.sub(r"\s+", " ", heard or "").strip().rstrip(".!")
    m = _DROP_ADDRESS.match(t)
    if m:
        return f"call me {m.group(1).strip()}"
    m = _DROP_RULE.match(t)
    if m and _Words(m.group(1)):
        return m.group(1).strip()
    return None
