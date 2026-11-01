"""
Telegram's Bot API, the calls Orion needs: who the bot is, anything new, say this (with buttons), change that,
a tap seen, "typing…", show this picture or file, fetch that photo, the "/" menu.

The bot's token is part of every address, so NO ADDRESS AND NO EXCEPTION'S MESSAGE IS EVER LOGGED FROM HERE: a
failure is reported as a value (None / False), never as text that could carry the token into the console or log.
Standard library only, so the reminder script (a separate process) can send an alert too.
"""
from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from System import TelegramRules as R

HOST = "https://api.telegram.org"
POLL_S = 50                        # Telegram holds "anything new?" open this long
MAX_DOWNLOAD = 15 * 1024 * 1024    # what the desk runner takes; the Bot API itself gives files up to 20 MB


def _Call(token: str, method: str, fields: Optional[dict] = None, files: Optional[dict] = None,
          timeout: float = 20) -> Optional[tuple[int, bytes]]:
    """(HTTP status, body), or None when Telegram couldn't be reached at all. `files`: {field: (name, mime, bytes)}."""
    url = f"{HOST}/bot{token}/{method}"
    headers = {}
    data = None
    if files:
        boundary = "orion" + secrets.token_hex(12)
        parts = []
        for k, v in (fields or {}).items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
                         + str(v).encode("utf-8") + b"\r\n")
        for k, (name, mime, blob) in files.items():
            safe = str(name).replace('"', "'").replace("\r", " ").replace("\n", " ")
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{safe}"\r\n'
                         f"Content-Type: {mime}\r\n\r\n".encode("utf-8") + blob + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        data = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif fields is not None:
        data = urllib.parse.urlencode({k: str(v) for k, v in fields.items()}).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        except Exception:
            return e.code, b""
    except Exception:
        return None


def BotName(token: str) -> Optional[str]:
    """The bot's @name, or None when the token is refused or Telegram can't be reached."""
    r = _Call(token, "getMe")
    return R.ParseBotName(r[1]) if r else None


def GetUpdates(token: str, offset: int, poll_s: int = POLL_S) -> R.Updates:
    """Messages and taps after `offset`; waits up to `poll_s` seconds for one."""
    r = _Call(token, "getUpdates", {"offset": offset, "timeout": poll_s,
                                    "allowed_updates": '["message","callback_query"]'}, timeout=poll_s + 20)
    if r is None:
        return R.Updates("failed")
    return R.ParseUpdates(r[0], r[1])


def Send(token: str, chat: int, text: str, buttons: Optional[str] = None) -> Optional[int]:
    """Plain text, as written: no formatting is read into it, so nothing in an answer can break the message.
    Returns the message's id, or None when it wasn't sent."""
    fields = {"chat_id": chat, "text": text}
    if buttons:
        fields["reply_markup"] = buttons
    r = _Call(token, "sendMessage", fields)
    return R.ParseSentId(r[1]) if r and r[0] == 200 else None


def Edit(token: str, chat: int, message_id: int, text: str) -> bool:
    """Rewrites one of the bot's messages; its buttons go with the old words."""
    r = _Call(token, "editMessageText", {"chat_id": chat, "message_id": message_id, "text": text})
    return bool(r and r[0] == 200)


def AnswerTap(token: str, query_id: str, text: Optional[str] = None) -> None:
    """Ends the spinner on a tapped button, with `text` shown briefly at the top of the chat."""
    fields = {"callback_query_id": query_id}
    if text:
        fields["text"] = text
    _Call(token, "answerCallbackQuery", fields)


def Typing(token: str, chat: int) -> None:
    """"typing…" under the bot's name for about five seconds."""
    _Call(token, "sendChatAction", {"chat_id": chat, "action": "typing"}, timeout=10)


def SendPhoto(token: str, chat: int, image: bytes, caption: str = "", name: str = "screen.png",
              mime: str = "image/png") -> bool:
    r = _Call(token, "sendPhoto", {"chat_id": chat, "caption": caption[:1000]},
              files={"photo": (name, mime, image)}, timeout=60)
    return bool(r and r[0] == 200)


def SendDocument(token: str, chat: int, name: str, mime: str, blob: bytes, caption: str = "") -> bool:
    r = _Call(token, "sendDocument", {"chat_id": chat, "caption": caption[:1000]},
              files={"document": (name, mime, blob)}, timeout=120)
    return bool(r and r[0] == 200)


def SetCommands(token: str) -> bool:
    """The list Telegram shows when "/" is typed in the chat."""
    r = _Call(token, "setMyCommands", {"commands": R.MenuJson()})
    return bool(r and r[0] == 200)


def Download(token: str, file_id: str) -> Optional[bytes]:
    """A file sent to the bot, by Telegram's id for it. None when it can't be fetched or is over MAX_DOWNLOAD.
    Its address carries the token too, so it is never logged."""
    r = _Call(token, "getFile", {"file_id": file_id})
    path = R.ParseFilePath(r[1]) if r and r[0] == 200 else None
    if not path:
        return None
    try:
        with urllib.request.urlopen(f"{HOST}/file/bot{token}/{path}", timeout=60) as resp:
            size = int(resp.headers.get("Content-Length") or 0)
            if size > MAX_DOWNLOAD:
                return None
            blob = resp.read(MAX_DOWNLOAD + 1)
            return blob if len(blob) <= MAX_DOWNLOAD else None
    except Exception:
        return None
