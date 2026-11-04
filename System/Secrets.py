"""
Secrets kept with Windows' own encryption for this user (DPAPI): the Telegram bot token and the JARVIS Link key.

Stored as "dpapi:<base64>". "plain:<base64>" is what is left when DPAPI can't be used (not Windows, or it failed):
it is only ever written with a warning printed, and read so an older save keeps working.

Standard library only: the reminder script, a separate process, imports this through System/TelegramLink.py.
"""
from __future__ import annotations

import base64
import sys
from typing import Optional


def Protect(data: bytes, what: str = "Orion") -> str:
    """data → "dpapi:…". Falls back to "plain:…" only with a warning in the log, never silently."""
    if sys.platform == "win32":
        try:
            return "dpapi:" + base64.b64encode(Dpapi(data, protect=True, what=what)).decode()
        except Exception as e:
            print(f"[Secrets] ⚠ Windows encryption (DPAPI) failed for {what}: {e.__class__.__name__}. "
                  f"Saved without it.")
    else:
        print(f"[Secrets] ⚠ no Windows encryption (DPAPI) here: {what} is saved without it.")
    return "plain:" + base64.b64encode(data).decode()


def Unprotect(stored: str) -> Optional[bytes]:
    """"dpapi:…" or "plain:…" → the bytes; None when it can't be read (another Windows user or PC, or garbage)."""
    try:
        kind, _, b64 = str(stored or "").partition(":")
        blob = base64.b64decode(b64)
        if kind == "dpapi":
            return Dpapi(blob, protect=False)
        if kind == "plain":
            return blob
    except Exception:
        pass
    return None


def Dpapi(data: bytes, protect: bool, what: str = "Orion") -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inb = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    UI_FORBIDDEN = 0x1
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(inb), what, None, None, None, UI_FORBIDDEN, ctypes.byref(out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(inb), None, None, None, None, UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)
