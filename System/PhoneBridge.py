"""
Orion ⇄ JARVIS Link: how Orion reaches the user's phone.

JARVIS Link (jarvis-link/jarvis_link.py) runs on this same PC and already talks to JARVIS on the phone. Orion pairs
with Link once: Link shows a 6-digit code (Connect Orion…), the user types it into Settings › Plugins › JARVIS Link
(phone), and both sides make a key from a fresh X25519 exchange plus the code. After that every frame is sealed
with that key (AES-256-GCM), the same way the phone's are. The key is kept in Config/PhoneLink.json (git-ignored).

What goes through it now (Phase 1): Orion's card (running / muted / speaking), alerts for the phone (reminders,
call messages, monitor and system alerts) and Orion's long jobs ("Orion is working on the dev agent"). Link hands
alerts to the phone, and the phone decides whether to show them (only when the user is away from the laptop).

It must never stop Orion working: Link not running is normal, so failures are retried quietly
(30 s, doubling to 5 min) and every call catches its own errors.
"""
from __future__ import annotations

import base64
import hmac
import json
import secrets
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from Brain.Settings import CONFIG_DIR

LINK_HOST = "127.0.0.1"
LINK_PORT = 47800
STORE = CONFIG_DIR / "PhoneLink.json"
WAIT_S = 20                    # Orion's long wait for work from the phone (Link caps it at 25)
BACKOFF_S = (30, 60, 120, 240, 300)
CARD_EVERY_S = 300             # the card is sent again this often, and at once when muted / speaking change
CAN = ["web_search", "browser", "flights", "dev_agent", "files", "screen", "call_secretary", "reminders"]

_lock = threading.Lock()


class LinkDown(Exception):
    """JARVIS Link isn't running (or isn't answering)."""


class NotPaired(Exception):
    """Link doesn't know this Orion (never paired, or forgotten on Link's side)."""


# ── storage ─────────────────────────────────────────────────────────────────

def Load() -> Optional[dict]:
    try:
        d = json.loads(STORE.read_text("utf-8"))
        return d if isinstance(d, dict) and d.get("key") and d.get("link_id") else None
    except Exception:
        return None


def Save(d: Optional[dict]) -> None:
    with _lock:
        if d is None:
            STORE.unlink(missing_ok=True)
        else:
            STORE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STORE.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, indent=2), "utf-8")
            tmp.replace(STORE)


# ── frames (the same as Link's: 4-byte length, then JSON) ───────────────────

def _Send(sock: socket.socket, obj: dict) -> None:
    data = json.dumps(obj).encode("utf-8")
    sock.sendall(struct.pack(">I", len(data)) + data)


def _Recv(sock: socket.socket) -> Optional[dict]:
    def Exact(n: int) -> Optional[bytes]:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf
    head = Exact(4)
    if head is None:
        return None
    body = Exact(struct.unpack(">I", head)[0])
    return json.loads(body.decode("utf-8")) if body is not None else None


def _Call(obj: dict, timeout: float = 8) -> dict:
    try:
        with socket.create_connection((LINK_HOST, LINK_PORT), timeout=timeout) as s:
            s.settimeout(timeout)
            _Send(s, obj)
            reply = _Recv(s)
    except OSError as e:
        raise LinkDown(str(e)) from e
    if reply is None:
        raise LinkDown("no answer")
    return reply


def _Seal(key: bytes, obj: dict, aad: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    return base64.b64encode(nonce + AESGCM(key).encrypt(nonce, json.dumps(obj).encode("utf-8"), aad)).decode()


def _Open(key: bytes, box: str, aad: bytes) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(box)
    return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], aad).decode("utf-8"))


def _Key(shared: bytes, code: str) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"orion-link-v1").derive(shared + code.encode())


# ── pairing ─────────────────────────────────────────────────────────────────

PAIR_ERRORS = {
    "code": "That code didn't match. Check the one in JARVIS Link.",
    "cancelled": "That code was cancelled after too many wrong tries. Click Connect Orion… in JARVIS Link for a new one.",
    "none": "JARVIS Link isn't showing a code. Click Connect Orion… in JARVIS Link first.",
    "bad": "JARVIS Link didn't accept the request. Update JARVIS Link and try again.",
}
LINK_DOWN = "JARVIS Link isn't running. Start it from its tray icon or start.bat, then try again."


def Pair(code: str, name: str = "Orion") -> tuple[bool, str]:
    """Pairs with the 6-digit code the user typed. (ok, words for the settings page)."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != 6:
        return False, "Type the 6-digit code that JARVIS Link shows."
    try:
        hello = _Call({"t": "hello"})
        mine = X25519PrivateKey.generate()
        pub = mine.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        proof = hmac.new(code.encode(), b"orion-pair" + pub, "sha256").hexdigest()
        r = _Call({"t": "orion_pair", "pub": base64.b64encode(pub).decode(), "name": name, "proof": proof})
    except LinkDown:
        return False, LINK_DOWN
    if not r.get("ok"):
        return False, PAIR_ERRORS.get(str(r.get("error")), PAIR_ERRORS["bad"])
    try:
        key = _Key(mine.exchange(X25519PublicKey.from_public_bytes(base64.b64decode(r["pub"]))), code)
        body = _Open(key, r["box"], b"orion-reply")
    except Exception:
        return False, "Pairing didn't finish: the keys didn't agree. Try again with a new code."
    link_id = str(body.get("link_id") or hello.get("id") or "")
    Save({"link_id": link_id, "key": base64.b64encode(key).decode(), "pc_name": str(body.get("pc_name", "")),
          "paired_at": int(time.time())})
    BRIDGE.OnPaired()
    return True, f"Connected to JARVIS Link on {body.get('pc_name') or 'this PC'}."


def Unpair() -> str:
    """Disconnect: tells Link first (so it forgets Orion too), then forgets here either way."""
    try:
        Sealed("unpair")
    except Exception:
        pass
    Save(None)
    BRIDGE.OnUnpaired("Disconnected.")
    return "Disconnected from JARVIS Link."


def Sealed(op: str, timeout: float = 8, **fields) -> dict:
    """One sealed frame to Link: its reply {ok, text, data}. Raises NotPaired / LinkDown."""
    d = Load()
    if not d:
        raise NotPaired()
    key = base64.b64decode(d["key"])
    box = _Seal(key, {"op": op, "ts": int(time.time() * 1000), **fields}, b"orion:" + d["link_id"].encode())
    r = _Call({"t": "orion", "box": box}, timeout)
    if "box" not in r:
        if r.get("error") == "not paired":
            raise NotPaired()
        raise LinkDown(str(r.get("error")))
    try:
        return _Open(key, r["box"], b"orion-reply:" + d["link_id"].encode())
    except Exception as e:
        raise NotPaired() from e


# ── the running connection ──────────────────────────────────────────────────

class Bridge:
    """Keeps Orion known to Link while Orion runs: the card, the long wait, and reconnecting by itself."""

    def __init__(self):
        self.state: Callable[[], dict] = lambda: {"running": True, "muted": False, "speaking": False}
        self.connected = False
        self.note = ""                 # why it isn't connected, for the settings page
        self.phones = 0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._started = False
        self._card_sent = ({}, 0.0)
        self.on_change: Callable[[], None] = lambda: None
        self.tasks = None              # System.PhoneTasks.PhoneTasks: what to do with the phone's requests
        self.together = None           # System.Together.Together: the phone's news (Phase 4)
        self.confirm_id = ""           # Orion's confirmation shown on the phone too, if any
        self._gate = None
        self._answered_from_phone = ""
        self.no_mirror = False         # a question that came FROM the phone is never sent back to it
        self.answered_inline: set[str] = set()   # phone tasks whose answer Orion already had from phone_ask
        self.last_phone_task = ""

    def Start(self, state: Optional[Callable[[], dict]] = None) -> None:
        if state:
            self.state = state
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._Loop, name="PhoneBridge", daemon=True).start()
        threading.Thread(target=self._Watch, name="PhoneBridgeCard", daemon=True).start()

    def Stop(self) -> None:
        """Ends the threads (tests; Orion itself just exits)."""
        self._stop.set()
        self._wake.set()

    def OnPaired(self) -> None:
        self.note = ""
        self._card_sent = ({}, 0.0)
        self._wake.set()

    def OnUnpaired(self, note: str) -> None:
        self._Set(False, note)

    def _Set(self, connected: bool, note: str = "") -> None:
        changed = connected != self.connected
        self.connected, self.note = connected, note
        if changed:
            try:
                self.on_change()
            except Exception:
                pass

    def _State(self) -> dict:
        try:
            s = self.state() or {}
        except Exception:
            s = {}
        return {"running": True, "muted": bool(s.get("muted")), "speaking": bool(s.get("speaking"))}

    def SendCard(self) -> None:
        state = self._State()
        Sealed("card", card={"name": "Orion", "version": "1", "can": CAN, "state": state})
        self._card_sent = (state, time.time())

    def _Loop(self) -> None:
        fails = 0
        while not self._stop.is_set():
            if not Load():
                self._Set(False, self.note)
                self._wake.wait(5)
                self._wake.clear()
                fails = 0
                continue
            try:
                if not self.connected:
                    self.SendCard()
                    self.phones = int((Sealed("status").get("data") or {}).get("phones", 0))
                    self._Set(True)
                    if self.together is not None:
                        self.together.OnConnected()   # one shared memory: both send everything on connect
                    fails = 0
                r = Sealed("wait", timeout=WAIT_S + 15, wait_s=WAIT_S)
                for task in (r.get("data") or {}).get("tasks", []):
                    self.OnTask(task)
            except NotPaired:
                if Load():  # not our own Disconnect: Link forgot us (or its folder was reset)
                    Save(None)
                    self._Set(False, "Disconnected by JARVIS Link. Connect again with a new code.")
            except Exception as e:
                was = self.connected
                self._Set(False, LINK_DOWN if isinstance(e, LinkDown) else f"Link error: {e.__class__.__name__}")
                if was:
                    print(f"[PhoneBridge] lost JARVIS Link: {e}")
                delay = BACKOFF_S[min(fails, len(BACKOFF_S) - 1)]
                fails += 1
                self._wake.wait(delay)
                self._wake.clear()

    def _Watch(self) -> None:
        """Sends the card again when muted / speaking change (at most every 2 s), and every few minutes."""
        while not self._stop.is_set():
            time.sleep(2)
            if not self.connected:
                continue
            last, at = self._card_sent
            if self._State() != last or time.time() - at > CARD_EVERY_S:
                try:
                    self.SendCard()
                except Exception:
                    pass

    def OnTask(self, task: dict) -> None:
        if not isinstance(task, dict):
            return
        if int(task.get("hops", 1) or 1) > 2:
            print(f"[PhoneBridge] refused {str(task.get('id'))[:16]}: too many hops")
            return
        if task.get("kind") == "confirm_answer":
            self._ConfirmAnswer(task)
        elif self.tasks is not None:
            self.tasks.Handle(task)
        else:
            Answer(str(task.get("id")), "failed", "Orion isn't ready yet. Try again in a moment.")

    # ── Orion's confirmations, on the phone too (A6) ────────────────────────
    # Only while a phone request is running or nobody is at the laptop. A tap on Yes there is the user's own tap,
    # the same as clicking CONFIRM on Orion's banner; the model can never answer it.

    def WrapConfirm(self, gate) -> None:
        """Called once after Brain.Confirm.bind(): the banner's show / hide also reach the phone."""
        show, hide, log = gate._show_cb, gate._hide_cb, gate._log_cb
        self._gate = gate
        if show is None or getattr(show, "mirrored", False):
            return

        def Show(title, detail):
            show(title, detail)
            self._MirrorShow(str(title), str(detail))

        def Hide():
            hide()
            self._MirrorHide()

        def Log(msg):
            if log:
                log(msg)
            if self._answered_from_phone and str(msg).startswith(("SYS: Confirmed", "ERR:")):
                self._answered_from_phone = ""
                Push("note", "Done", str(msg).split(" — ", 1)[-1][:600], speak=False)

        Show.mirrored = True
        gate.bind(Show, Hide, Log)

    def _MirrorShow(self, title: str, detail: str) -> None:
        if self.no_mirror:
            return
        busy = getattr(self.tasks, "running", 0) > 0
        if not self.connected or not (busy or IdleSeconds() >= AWAY_S):
            return
        cid = "c-" + secrets.token_hex(6)
        self.confirm_id = cid
        threading.Thread(target=lambda: _Quiet(lambda: Sealed("confirm", id=cid, title=title, text=detail)),
                         daemon=True).start()

    def _MirrorHide(self) -> None:
        cid, self.confirm_id = self.confirm_id, ""
        if cid:
            threading.Thread(target=lambda: _Quiet(lambda: Sealed("confirm_done", id=cid)), daemon=True).start()

    def _ConfirmAnswer(self, task: dict) -> None:
        gate = self._gate
        if not gate or not self.confirm_id or task.get("id") != self.confirm_id or not gate.PendingTitle():
            return                     # an old question, or already answered at the laptop
        self._answered_from_phone = self.confirm_id if task.get("yes") else ""
        print(f"[PhoneBridge] confirmation answered on the phone: {'yes' if task.get('yes') else 'no'}")
        gate.resolve(bool(task.get("yes")))

    def Status(self) -> tuple[str, Optional[bool]]:
        """The settings page's status line: (words, connected?)."""
        d = Load()
        if not d:
            return (self.note or "Not connected"), False
        if self.connected:
            phones = f" · {self.phones} phone{'s' if self.phones != 1 else ''} paired" if self.phones else ""
            return f"Connected to JARVIS Link on {d.get('pc_name') or 'this PC'}{phones}", True
        return (self.note or "Connecting to JARVIS Link…"), False

    def Dot(self) -> Optional[bool]:
        """The top bar's link dot: None (never paired, hidden), True connected, False paired but not reachable."""
        if not Load():
            return None
        return self.connected


BRIDGE = Bridge()


AWAY_S = 60


def IdleSeconds() -> float:
    """Seconds since the keyboard or mouse was last touched (0 where that can't be read)."""
    try:
        import ctypes

        class _LastInput(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        li = _LastInput(ctypes.sizeof(_LastInput), 0)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
            return 0.0
        return (((ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF) - li.dwTime) & 0xFFFFFFFF) / 1000
    except Exception:
        return 0.0


def _Quiet(fn):
    try:
        return fn()
    except Exception:
        return None


def Push(what: str, title: str, text: str, urgent: bool = False, speak: bool = True,
         url: str = "", file: Optional[dict] = None) -> bool:
    """One alert for the phone. False if it couldn't go (not paired, Link down): never raises."""
    extra = {}
    if url:
        extra["url"] = url
    if file:
        extra["file"] = file
    try:
        return bool(Sealed("push", what=what, title=title[:80], text=text[:600], urgent=urgent, speak=speak,
                           **extra).get("ok"))
    except Exception:
        return False


def Answer(task_id: str, state: str, text: str, speak: bool = True) -> bool:
    """Orion's answer to a request from the phone. Never raises."""
    try:
        return bool(Sealed("answer", id=task_id, state=state, text=text[:8000], speak=speak).get("ok"))
    except Exception as e:
        print(f"[PhoneBridge] answer for {task_id[:16]} didn't go: {e.__class__.__name__}")
        return False


PHONE_FINAL = {"completed", "failed", "canceled"}


def AskPhone(action: str, args: Optional[dict] = None, wait_s: float = 20) -> dict:
    """Phase 3: a task for the phone. {state, text, data, id}; state 'failed' with words when it can't go."""
    if not Load():
        return {"state": "failed", "text": "Orion isn't connected to JARVIS Link, so it can't reach the phone.", "data": {}}
    try:
        r = Sealed("phone_ask", timeout=wait_s + 10, action=action, args=args or {}, wait_s=wait_s)
    except NotPaired:
        return {"state": "failed", "text": "JARVIS Link has forgotten Orion. Connect again in Settings › Plugins.", "data": {}}
    except Exception:
        return {"state": "failed", "text": LINK_DOWN, "data": {}}
    if not r.get("ok"):
        return {"state": "failed", "text": str(r.get("text") or "The phone didn't take that."), "data": {}}
    d = r.get("data") or {}
    tid = str(d.get("id", ""))
    BRIDGE.last_phone_task = tid
    if d.get("state") in PHONE_FINAL:
        BRIDGE.answered_inline.add(tid)
    return {"id": tid, "state": str(d.get("state")), "text": str(d.get("answer") or ""), "data": d.get("data") or {}}


def PhoneCancel(task_id: str) -> bool:
    return bool(_Quiet(lambda: Sealed("phone_cancel", id=task_id).get("ok")))


def PhoneConfirmAnswer(qid: str, yes: bool) -> bool:
    """Only ever called from a click on Orion's banner (never from the model)."""
    return bool(_Quiet(lambda: Sealed("phone_confirm_answer", id=qid, yes=yes).get("ok")))


def SendToPhone(title: str, text: str, url: str = "", path: str = "") -> str:
    """A5: a note, a link or a file on the phone. Words for the model to say."""
    if not Load():
        return "Orion isn't connected to JARVIS Link, so it can't reach the phone. Connect it in Settings › Plugins."
    file = None
    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            return f"There's no file at {path}."
        try:
            r = Sealed("offer", path=str(p.resolve()))
        except Exception:
            return LINK_DOWN
        if not r.get("ok"):
            return f"JARVIS Link wouldn't send that file: {r.get('text')}"
        file = r["data"]
    if not (text or url or file):
        return "Nothing to send."
    ok = Push("note", title or "From Orion", text or (file or {}).get("name", "") or url, url=url, file=file)
    if not ok:
        return LINK_DOWN if not BRIDGE.connected else "That didn't reach JARVIS Link. Try again."
    return "Sent to your phone."


def Report(session: str, task: str, state: str) -> bool:
    """Orion's own work as an agent on the phone: working → done. Never raises."""
    try:
        return bool(Sealed("report", session=session, task=task[:60], state=state).get("ok"))
    except Exception:
        return False
