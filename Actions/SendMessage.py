import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from Brain import Outcome
from System.LazyImport import LazyModule, installed

# Imported on first USE, not at launch — see core/lazyimport.py. PAUSE is 0.06
# here rather than computer_control's 0.05, as it always was: typing into
# somebody else's chat window wants a little more room than a hotkey does.
_PYAUTOGUI = installed("pyautogui")
_PYPERCLIP = installed("pyperclip")


def ConfigurePyautogui(module) -> None:
    module.FAILSAFE = True
    module.PAUSE    = 0.06


pyautogui = LazyModule("pyautogui", ConfigurePyautogui)
pyperclip = LazyModule("pyperclip")

def BaseDir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

def _GetOs() -> str:
    try:
        cfg = json.loads(
            (BaseDir() / "Config" / "ApiKeys.json").read_text(encoding="utf-8")
        )
        return cfg.get("os_system", "windows").lower()
    except Exception:
        return "windows"


def RequirePyautogui():
    if not _PYAUTOGUI:
        raise RuntimeError("PyAutoGUI not installed. Run: pip install pyautogui")


def PasteText(text: str) -> None:
    RequirePyautogui()

    os_name = _GetOs()
    paste_hotkey = ("command", "v") if os_name == "mac" else ("ctrl", "v")

    if _PYPERCLIP:
        try:
            saved = pyperclip.paste()
        except Exception:
            saved = None
        pyperclip.copy(text)
        time.sleep(0.15)
        pyautogui.hotkey(*paste_hotkey)
        time.sleep(0.1)
        if saved is not None:        # give the user's clipboard back
            time.sleep(0.2)
            try:
                pyperclip.copy(saved)
            except Exception:
                pass
    else:
        pyautogui.write(text, interval=0.03)


def ClearAndPaste(text: str) -> None:
    RequirePyautogui()
    os_name = _GetOs()
    select_all = ("command", "a") if os_name == "mac" else ("ctrl", "a")
    pyautogui.hotkey(*select_all)
    time.sleep(0.1)
    pyautogui.press("delete")
    time.sleep(0.1)
    PasteText(text)

def OpenApp(app_name: str) -> bool:
    RequirePyautogui()
    os_name = _GetOs()

    try:
        if os_name == "windows":
            pyautogui.press("win")
            time.sleep(0.5)
            PasteText(app_name)
            time.sleep(0.6)
            pyautogui.press("enter")
            time.sleep(2.5)
            return True

        elif os_name == "mac":
            result = subprocess.run(
                ["open", "-a", app_name],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            )
            if result.returncode != 0:
                result = subprocess.run(
                    ["open", "-a", f"{app_name}.app"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                )
            time.sleep(2.5)
            return result.returncode == 0

        else: 
            launched = False
            for launcher in [
                ["gtk-launch", app_name.lower()],
                [app_name.lower()],
            ]:
                try:
                    subprocess.Popen(
                        launcher,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    launched = True
                    break
                except FileNotFoundError:
                    continue
            time.sleep(2.5)
            return launched

    except Exception as e:
        print(f"[SendMessage] ⚠️ Could not open {app_name}: {e}")
        return False


def OpenBrowserUrl(url: str) -> bool:
    import webbrowser
    try:
        webbrowser.open(url)
        time.sleep(4.0) 
        return True
    except Exception as e:
        print(f"[SendMessage] ⚠️ Could not open browser: {e}")
        return False

def SearchInApp(query: str) -> None:
    RequirePyautogui()
    os_name = _GetOs()
    search_hotkey = ("command", "f") if os_name == "mac" else ("ctrl", "f")

    pyautogui.hotkey(*search_hotkey)
    time.sleep(0.5)
    ClearAndPaste(query)
    time.sleep(1.0)

def DesktopSend(app_name: str, receiver: str, message: str) -> str:
    if not OpenApp(app_name):
        return Outcome.failed(
            "send_message", f"{app_name} could not be opened, so nothing was "
            f"typed and no message was sent",
            platform=app_name, to=receiver,
        )

    time.sleep(1.0)
    SearchInApp(receiver)
    pyautogui.press("enter")
    time.sleep(0.8)

    PasteText(message)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)

    # UNVERIFIED, AND THAT IS THE HONEST ANSWER.
    #
    # Everything above is keystrokes with sleeps between them: press Win, type
    # the app name, Ctrl+F, type the contact, Enter, paste, Enter. Nothing
    # reads back which window received any of it. On a machine one second
    # slower than these sleeps assume, the whole sequence lands somewhere else
    # — and the old code still returned "Message sent to X via Y."
    #
    # There is no read-back to add here without a driver per application, and
    # `_send_whatsapp` shows what that costs to build. Until one exists for a
    # given app, the truthful thing is to say the keystrokes went out and the
    # result is unconfirmed, which is something the user can glance at their
    # screen and check.
    return Outcome.unverified(
        "send_message",
        f"the message was typed into {app_name} blind - no confirmation is "
        f"available for this app, so it is NOT known to have been sent",
        platform=app_name, to=receiver,
    )

def SendWhatsapp(receiver: str, message: str) -> str:
    """WhatsApp is typed blind, like the other desktop apps.

    This used to try `Plugins._whatsapp_core`, a verified driver that never
    existed in this project, so every send already fell through to the blind
    path below. The dead branch is gone; the send itself now waits for the
    on-screen confirm in SendMessage(), because blind keystrokes can land in
    the wrong window or conversation.
    """
    return DesktopSend("WhatsApp", receiver, message)

def SendTelegram(receiver: str, message: str) -> str:
    return DesktopSend("Telegram", receiver, message)

def SendSignal(receiver: str, message: str) -> str:
    return DesktopSend("Signal", receiver, message)


def SendDiscord(receiver: str, message: str) -> str:
    return DesktopSend("Discord", receiver, message)


def SendInstagram(receiver: str, message: str) -> str:
    RequirePyautogui()

    if not OpenBrowserUrl("https://www.instagram.com/direct/new/"):
        return Outcome.failed(
            "send_message", "Instagram could not be opened in the browser, so "
            "nothing was sent", platform="Instagram", to=receiver)

    PasteText(receiver)
    time.sleep(1.5)

    pyautogui.press("down")
    time.sleep(0.3)
    pyautogui.press("enter")   
    time.sleep(0.4)

    for _ in range(4):
        pyautogui.press("tab")
        time.sleep(0.15)
    pyautogui.press("enter")
    time.sleep(2.0)

    PasteText(message)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)

    return Outcome.unverified(
        "send_message",
        "the message was typed into Instagram in the browser blind - the page "
        "was never read back, so it is NOT known to have been sent",
        platform="Instagram", to=receiver)


def SendMessenger(receiver: str, message: str) -> str:
    RequirePyautogui()

    if not OpenBrowserUrl("https://www.messenger.com/"):
        return Outcome.failed(
            "send_message", "Messenger could not be opened in the browser, so "
            "nothing was sent", platform="Messenger", to=receiver)


    SearchInApp(receiver)
    time.sleep(0.5)
    pyautogui.press("down")
    time.sleep(0.3)
    pyautogui.press("enter")
    time.sleep(1.0)

    PasteText(message)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)

    return Outcome.unverified(
        "send_message",
        "the message was typed into Messenger in the browser blind - the page "
        "was never read back, so it is NOT known to have been sent",
        platform="Messenger", to=receiver)

_PLATFORM_MAP = [
    ({"whatsapp", "wp", "wapp"},              SendWhatsapp),
    ({"telegram", "tg"},                      SendTelegram),
    ({"instagram", "ig", "insta"},            SendInstagram),
    ({"signal"},                               SendSignal),
    ({"discord"},                              SendDiscord),
    ({"messenger", "facebook", "fb"},         SendMessenger),
]


def ResolvePlatform(platform_str: str):
    # Whole words only: a substring test sent "signal" through Instagram ("ig").
    key = platform_str.lower().strip()
    words = set(re.split(r"[^a-z0-9]+", key)) - {""}
    for keywords, handler in _PLATFORM_MAP:
        if key in keywords or words & keywords:
            return handler
    return lambda r, m: DesktopSend(platform_str.strip().title(), r, m)


def SendMessage(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params       = parameters or {}
    receiver     = params.get("receiver", "").strip()
    message_text = params.get("message_text", "").strip()
    platform     = params.get("platform", "whatsapp").strip()

    if not receiver:
        return Outcome.failed("send_message", "no recipient was given")
    if not message_text:
        return Outcome.failed("send_message", "no message text was given",
                              to=receiver)
    if not _PYAUTOGUI:
        return Outcome.failed(
            "send_message",
            "PyAutoGUI is not installed, so the desktop cannot be driven - "
            "run: pip install pyautogui",
            to=receiver, platform=platform,
        )

    preview = message_text[:50] + ("…" if len(message_text) > 50 else "")
    print(f"[SendMessage] 📨 {platform} → {receiver}: {preview}")
    if player:
        player.WriteLog(f"[msg] {platform} → {receiver}")   # the message itself IS the news

    # Every path here types blind: nothing reads back which window or
    # conversation got the keystrokes, and a message cannot be unsent. So it
    # waits for the user's on-screen confirm, then sends in the background and
    # reports, as before.
    from Brain import Confirm
    if Confirm.PendingTitle():
        return Outcome.ask(
            "send_message",
            "there is already a confirmation waiting on screen - the user has "
            "to answer that one first", to=receiver, platform=platform)

    def Go() -> str:
        threading.Thread(
            target=SendAndReport,
            args=(player, platform, receiver, message_text),
            daemon=True, name="send-message",
        ).start()
        return f"Sending to {receiver} on {platform}."

    return Confirm.request(
        "send_message",
        f"Send {platform} message to {receiver}: {preview}"[:120],
        "Orion will type this message into the app without being able to "
        "check which conversation gets it.",
        Go,
    )


def SendAndReport(player, platform: str, receiver: str,
                     message_text: str) -> None:
    """Do the sending, then say what actually happened — including when it did
    not happen, and including when nobody can tell.

    Every path here reports. A tool that says `started` and then goes quiet on
    failure has only moved the false success one step later, which is the whole
    thing this rewrite exists to stop.
    """
    try:
        result = ResolvePlatform(platform)(receiver, message_text)
    except Exception as exc:
        print(f"[SendMessage] {type(exc).__name__}: {exc}")
        result = Outcome.failed("send_message", f"it failed: {exc}",
                                to=receiver, platform=platform)

    # A prefix test on a fixed vocabulary, not a search for the word "sent".
    # The old check was `"sent" in text and "not sent" not in text`, which is
    # one careless rewording away from putting a tick next to a message that
    # never went.
    verified = result.startswith(f"status={Outcome.DONE} ")

    # The full status line goes to the TERMINAL, where a developer reads it.
    # The activity log beside the assistant's own sentences is for the
    # conversation and for things that went wrong — a line reading
    # `status=done action=send_message verified=true` there is machinery
    # showing through, and it was reported as exactly that.
    print(f"[SendMessage] {'✅' if verified else '⚠️'} {result[:160]}")
    if player and Outcome.IsProblem(result):
        player.WriteLog(f"[msg] could not confirm the message to {receiver} — "
                         f"see the terminal")

    if verified:
        # It went, and the user asked for it to go. Announcing a success they
        # already expect is noise — the log and the panel carry the record.
        return

    if player and hasattr(player, "request_say"):
        try:
            player.request_say(
                "Tell the user, in one or two short sentences in their own "
                "language, exactly what this says happened — and if it says "
                "the message was not sent or not confirmed, say that plainly "
                "rather than claiming it was sent: " + result
            )
        except Exception:
            pass


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "send_message",
    "description": (
        "Sends a text message via WhatsApp, Telegram, or another messaging "
        "platform. Write 'message_text' in the USER'S OWN LANGUAGE, exactly "
        "what they asked to be said. "
        "It first puts a confirmation on screen; nothing is sent until the "
        "user confirms. Then it sends in the background. On "
        "status=started say only that you are sending it, never that it is "
        "sent. The result follows separately: status=done means it was "
        "confirmed delivered to the conversation, status=unverified means the "
        "keystrokes went out and NOBODY can confirm it arrived - say exactly "
        "that - and status=failed means it did not go. Never tell the user a "
        "message was sent unless the result said status=done."
    ),
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "receiver": {
                "type": "STRING",
                "description": "Recipient contact name"
            },
            "message_text": {
                "type": "STRING",
                "description": "The message to send"
            },
            "platform": {
                "type": "STRING",
                "description": "Platform: WhatsApp, Telegram, etc."
            }
        },
        "required": [
            "receiver",
            "message_text",
            "platform"
        ]
    },
    "handler": SendMessage,
}
