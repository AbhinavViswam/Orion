"""
BackgroundMonitor — user-configured topic watching.
Checks DDG news once per day per topic; alerts ORION when a new headline appears.
No crypto, no finance, no uninvited tracking.
"""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path


# ── Blocked categories (never monitor regardless of what user says) ────────────

_BLOCKED = {
    # Brand / asset names — spelled the same in every language
    "bitcoin", "ethereum", "dogecoin", "solana", "binance",
    "nft", "blockchain", "defi", "altcoin", "memecoin", "coin", "token",
    # spellings of the "crypto" root across different languages
    "crypto", "kripto", "cripto", "krypto", "крипто", "仮想通貨", "暗号資産",
    "cryptocurrency",
}

def IsBlocked(topic: str) -> bool:
    t = topic.lower()
    return any(word in t for word in _BLOCKED)


# ── Slug / hash helpers ────────────────────────────────────────────────────────

def Slug(topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", topic.lower().strip())[:40].strip("_")

def TitleHash(title: str) -> str:
    return hashlib.md5(title.encode("utf-8", errors="ignore")).hexdigest()[:12]


# ── Memory I/O ─────────────────────────────────────────────────────────────────

def _load() -> dict:
    from Brain.Memory import LoadMemory
    data = LoadMemory().get("monitors", {})
    return data if isinstance(data, dict) else {}

def _Save(monitors: dict) -> None:
    from Brain.Memory import LoadMemory, MEMORY_PATH, _lock
    memory = LoadMemory()
    memory["monitors"] = monitors
    with _lock:
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ── Public API ─────────────────────────────────────────────────────────────────

def AddMonitor(topic: str) -> str:
    topic = topic.strip()
    if not topic:
        return "Please specify a topic to monitor."
    if IsBlocked(topic):
        return "I don't monitor crypto or financial topics."
    monitors = _load()
    slug = Slug(topic)
    if slug in monitors:
        return f"Already monitoring: {monitors[slug]['topic']}"
    monitors[slug] = {
        "topic":      topic,
        "added":      datetime.now().strftime("%Y-%m-%d"),
        "last_check": "",
        "last_hash":  "",
    }
    _Save(monitors)
    print(f"[Monitor] ➕ Added: {topic}")
    return f"Now monitoring: {topic}"


def RemoveMonitor(topic: str) -> str:
    topic = topic.strip().lower()
    monitors = _load()
    # exact slug match first
    slug = Slug(topic)
    if slug in monitors:
        label = monitors.pop(slug)["topic"]
        _Save(monitors)
        return f"Stopped monitoring: {label}"
    # partial match fallback
    for key, val in list(monitors.items()):
        if topic in val.get("topic", "").lower():
            label = monitors.pop(key)["topic"]
            _Save(monitors)
            return f"Stopped monitoring: {label}"
    return f"Not found in monitored topics: {topic}"


def ListMonitors() -> list[str]:
    return [v.get("topic", k) for k, v in _load().items()]


def CheckAll() -> list[str]:
    """
    Run all pending topic checks (once per day per topic).
    Returns a list of [MONITOR_ALERT] strings — empty if nothing new.
    """
    from Actions.WebSearch import DdgNews

    monitors = _load()
    if not monitors:
        return []

    today   = datetime.now().strftime("%Y-%m-%d")
    alerts  = []
    changed = False

    for slug, data in monitors.items():
        if data.get("last_check") == today:
            continue                     # already checked today

        topic = data.get("topic", slug)
        try:
            results = DdgNews(topic, max_results=5)
            if not results:
                monitors[slug]["last_check"] = today
                changed = True
                continue

            top   = results[0]
            title = top.get("title", "").strip()
            if not title:
                continue

            h = TitleHash(title)
            monitors[slug]["last_check"] = today
            changed = True

            if h == data.get("last_hash"):
                continue                 # same headline as last check — no alert

            monitors[slug]["last_hash"] = h

            snippet = top.get("snippet", "")[:150]
            source  = top.get("source", "")
            parts   = [f"[MONITOR_ALERT] {topic}", f"Headline: {title}"]
            if snippet:
                parts.append(snippet)
            if source:
                parts.append(f"Source: {source}")
            alerts.append("\n".join(parts))
            print(f"[Monitor] 🔔 New headline for '{topic}': {title[:60]}")

        except Exception as e:
            print(f"[Monitor] ⚠️ Check failed for '{topic}': {e}")

    if changed:
        _Save(monitors)

    return alerts
