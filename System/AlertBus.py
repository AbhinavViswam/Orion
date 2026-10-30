"""
One line per place Orion has news for the user's phone:

    AlertBus.Alert("reminder", "Reminder", "Call the dentist")
    AlertBus.Work("dev:1", "the dev agent", "working")   # … later "done"

Each kind has its own switch in Settings › Plugins › JARVIS Link (phone). Sent in the background, so the caller
never waits on JARVIS Link; nothing happens when Orion isn't paired with Link. Never raises.
"""
from __future__ import annotations

import re
import threading

NAMESPACE = "phone_link"
# what → the plugin setting that switches it off
SWITCHES = {"reminder": "send_reminders", "call": "send_calls", "monitor": "send_alerts", "alert": "send_alerts",
            "note": "send_alerts", "work": "send_work"}


def Enabled(what: str) -> bool:
    try:
        from Brain.Settings import GetPluginEnabled, GetPluginSetting
        if not GetPluginEnabled("phone_link"):
            return False
        return bool(GetPluginSetting(NAMESPACE, SWITCHES.get(what, "send_alerts"), True))
    except Exception:
        return False


def Plain(alert: str) -> str:
    """An alert written for the model ("[SYSTEM_ALERT] CPU is at 95%. Warn the user…") as words for a person:
    the tags dropped, and only each alert's first sentence kept."""
    parts = [p.strip() for p in re.split(r"\[[A-Z_]+\]", str(alert or "")) if p.strip()]
    out = []
    for p in parts:
        m = re.match(r"(.+?[.!?])(\s|$)", p, re.S)
        out.append((m.group(1) if m else p).strip())
    return " ".join(out)[:600]


def MonitorText(alert: str) -> tuple[str, str]:
    """A [MONITOR_ALERT] (topic, headline, snippet, source on their own lines) as (title, text) for the phone."""
    lines = [l.strip() for l in str(alert or "").splitlines() if l.strip()]
    topic = re.sub(r"^\[[A-Z_]+\]\s*", "", lines[0]) if lines else ""
    headline = next((l[len("Headline:"):].strip() for l in lines if l.startswith("Headline:")), "")
    source = next((l[len("Source:"):].strip() for l in lines if l.startswith("Source:")), "")
    text = headline or " ".join(lines[1:])
    if source:
        text += f" ({source})"
    return (f"Watching: {topic}" if topic else "Something you're watching")[:80], text[:600]


def AlertNow(what: str, title: str, text: str, urgent: bool = False, speak: bool = True) -> bool:
    """The same, waiting for it to go: for the reminder script, which exits straight after."""
    if not text or not Enabled(what):
        return False
    try:
        from System import PhoneBridge
        return PhoneBridge.Push(what, title, text, urgent=urgent, speak=speak)
    except Exception:
        return False


def Alert(what: str, title: str, text: str, urgent: bool = False, speak: bool = True) -> None:
    threading.Thread(target=AlertNow, args=(what, title, text, urgent, speak), daemon=True).start()


def Work(session: str, task: str, state: str) -> None:
    """Orion's long job, shown on the phone like Claude Code's: working / approval / done."""
    def Go():
        if not Enabled("work"):
            return
        try:
            from System import PhoneBridge
            PhoneBridge.Report(session, task, state)
        except Exception:
            pass
    threading.Thread(target=Go, daemon=True).start()
