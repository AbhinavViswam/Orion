"""
Telegram: use Orion from anywhere through a Telegram bot of your own, like JARVIS's Telegram remote on the phone.
The bot itself (pairing, listening, answering, confirmations, alerts) lives in System/TelegramLink.py; this file is
the settings section (token box, Pair / Unpair, switches) and a small tool so "is my Telegram bot on?" and "set up
the Telegram bot" have an answer.
"""
from System.TelegramLink import LINK, NAMESPACE, Load

SETUP = ("In Telegram, message @BotFather, send /newbot and follow the steps. Copy the token it gives you, paste it "
         "in Settings › Plugins › Telegram and press Save token. Then send the 6-digit code it shows to your new "
         "bot in Telegram.")

PLUGIN_SETTINGS = {
    "namespace": NAMESPACE,
    "title": "Telegram",
    "fields": [
        {"key": "status", "label": "Status", "type": "status", "get": LINK.Status},
        {"key": "token", "label": "Bot token", "type": "password", "save": False,
         "placeholder": "123456789:AA… from @BotFather",
         "hint": "Make a bot with @BotFather in Telegram (/newbot) and paste its token here. Give Orion its own bot, "
                 "not JARVIS's: only one can listen. The button saves a token whenever one is typed here."},
        {"key": "details", "label": "Send private answers here", "type": "toggle", "default": False,
         "hint": "While private mode is on, answers it keeps from Gemini (and screenshots) stay on the laptop "
                 "unless this is on"},
        {"key": "alert_reminders", "label": "Reminders in Telegram", "type": "toggle", "default": False,
         "hint": "When one of Orion's reminders comes due"},
        {"key": "alert_calls", "label": "Call messages in Telegram", "type": "toggle", "default": False,
         "hint": "Who called and the message the call secretary took"},
        {"key": "alert_alerts", "label": "Alerts in Telegram", "type": "toggle", "default": False,
         "hint": "Topics you asked Orion to watch, and laptop warnings like a hot CPU"},
        {"key": "alert_work", "label": "Long jobs finished", "type": "toggle", "default": False,
         "hint": "\"Orion finished the dev agent\""},
    ],
    "action": {"label": LINK.ButtonLabel, "run": LINK.Act, "busy": "Checking with Telegram…"},
}

PLUGIN = {
    "name": NAMESPACE,
    "description": (
        "Orion's Telegram bot, which lets the user message Orion from anywhere. action 'status' answers 'is my "
        "Telegram bot on / paired?'. action 'setup' when the user asks to set up, connect or pair the Telegram bot: "
        "it opens the settings and tells them the steps (you cannot paste the token or send the code for them)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {"action": {"type": "STRING", "description": "status | setup"}},
        "required": ["action"],
    },
    "tier": "extra",
    "brief": "Message Orion from anywhere through your own Telegram bot.",
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str((parameters or {}).get("action") or "status").lower().strip()
    status, _ = LINK.Status()
    if action == "setup":
        d = Load()
        opener = getattr(player, "OpenSettings", None)
        if callable(opener):
            try:
                opener("plugins")
            except Exception:
                pass
        if d.get("owner"):
            return f"The Telegram bot is already set up: {status}"
        if d.get("bot"):
            return "The bot is saved. In Settings › Plugins › Telegram press Pair, then send the code to the bot."
        return SETUP
    return status
