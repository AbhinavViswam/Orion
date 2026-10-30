"""
Interface/FloatingReactor.py — Orion as its own reactor, floating on the desktop.

One Orion, two looks, never both on screen: the floating reactor, or the full
window. Both show frames from the SAME reactor thread (HudCanvas owns it), so
there is never a second renderer, and the hidden window draws nothing.

    drag            move it (the place is remembered)
    click           type to Orion
    double-click    open the full window
    right-click     the menu: window, type, mic, stop, settings, size, quit

Closing the full window brings the reactor back instead of quitting; Quit is in
the menus. A second launch of Orion only shows the running one (a local socket,
like Ultron's).
"""
from __future__ import annotations

import getpass
import json
import time

from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QLineEdit, QMenu, QSystemTrayIcon, QWidget

from Interface.Window import C, CONFIG_DIR, UI_FONT, Qcol

SETTINGS = CONFIG_DIR / "Floating.json"
SIZES = (("Small", 110), ("Medium", 150), ("Large", 200))
DEFAULTS = {"start_floating": True, "size": 150, "x": None, "y": None}
SERVER_NAME = f"Orion.Assistant.{getpass.getuser()}"
_CAPTION_S = 10.0          # how long the last answer stays under it


def Load() -> dict:
    out = dict(DEFAULTS)
    try:
        out.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
    except Exception:
        pass
    return out


def Save(**kw) -> None:
    data = Load()
    data.update(kw)
    try:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[Floating] not saved: {e}")


# ── One copy of Orion ─────────────────────────────────────────────────────────

def ClaimSingleInstance():
    """A listening QLocalServer when this is the only Orion, else None (the
    running one has been asked to show itself)."""
    from PyQt6.QtNetwork import QLocalServer, QLocalSocket
    probe = QLocalSocket()
    probe.connectToServer(SERVER_NAME)
    if probe.waitForConnected(300):
        probe.write(b"show")
        probe.flush()
        probe.waitForBytesWritten(300)
        probe.disconnectFromServer()
        return None
    QLocalServer.removeServer(SERVER_NAME)      # a stale name from a crash
    server = QLocalServer()
    if not server.listen(SERVER_NAME):
        print(f"[Floating] single-copy guard off: {server.errorString()}")
    return server


def ServeSummons(server, summon) -> None:
    # Any connection is a summons. Waiting for its bytes lost them: they could
    # arrive before readyRead was connected, and readyRead never fires again.
    def Incoming():
        came = False
        while server.hasPendingConnections():
            sock = server.nextPendingConnection()
            sock.disconnected.connect(sock.deleteLater)
            sock.disconnectFromServer()
            came = True
        if came:
            summon()
    server.newConnection.connect(Incoming)


# ── The floating reactor ──────────────────────────────────────────────────────

class FloatingReactor(QWidget):
    _PILL_H = 22
    _GAP = 6
    _INPUT_H = 30

    def __init__(self, win):
        super().__init__(None, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowTitle("Orion")
        self._win = win
        self._side = int(Load().get("size") or 150)
        self._frame_id = 0
        self._frame = None
        self._press: QPoint | None = None
        self._dragged = False
        self._blink = True
        self._blink_t = 0.0

        self._input = QLineEdit(self)
        self._input.setPlaceholderText("Type to Orion…  (Esc to close)")
        self._input.setFont(QFont(UI_FONT, 10))
        self._input.setStyleSheet(f"""
            QLineEdit {{ background: rgba(8, 14, 22, 0.92); color: {C.WHITE};
                         border: 1px solid {C.BORDER_B}; border-radius: 15px; padding: 3px 12px; }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}""")
        self._input.returnPressed.connect(self.SendTyped)
        self._input.hide()
        self._input.installEventFilter(self)

        self.Resize()
        self.Place()

        self._tmr = QTimer(self)
        self._tmr.setTimerType(Qt.TimerType.PreciseTimer)
        self._tmr.timeout.connect(self.Tick)

    # ── size and place ────────────────────────────────────────────────────────
    def Resize(self) -> None:
        w = max(self._side + 24, 280)
        h = self._side + self._GAP + 2 * (self._PILL_H + 4) + self._GAP + self._INPUT_H
        self.setFixedSize(w, h)
        self._input.setGeometry(10, h - self._INPUT_H, w - 20, self._INPUT_H)

    def SetSide(self, side: int) -> None:
        centre = self.geometry().center()
        self._side = int(side)
        Save(size=self._side)
        self.Resize()
        self.move(centre.x() - self.width() // 2, self.y())
        self.update()

    def Place(self) -> None:
        cfg = Load()
        screen = QApplication.primaryScreen().availableGeometry()
        x, y = cfg.get("x"), cfg.get("y")
        if x is None or y is None:
            x = screen.right() - self.width() - 24
            y = screen.bottom() - self.height() - 24
        # Back on a screen that still exists (a monitor may have been unplugged).
        on = any(s.availableGeometry().intersects(
            self.geometry().translated(int(x) - self.x(), int(y) - self.y()))
            for s in QApplication.screens())
        if not on:
            x = screen.right() - self.width() - 24
            y = screen.bottom() - self.height() - 24
        self.move(int(x), int(y))

    def ReactorRect(self) -> QRectF:
        return QRectF((self.width() - self._side) / 2, 0, self._side, self._side)

    # ── showing ───────────────────────────────────────────────────────────────
    def showEvent(self, e):
        self._win.hud.float_on = True
        self._tmr.start(16)
        super().showEvent(e)

    def hideEvent(self, e):
        self._win.hud.float_on = False
        self._tmr.stop()
        self._input.hide()
        super().hideEvent(e)

    def Summon(self) -> None:
        """A second launch of Orion: show what is there (the reactor, or the
        window if the reactor was already up)."""
        if self.isVisible():
            self._win.ShowWindow()
        elif self._win.isVisible():
            self._win.raise_()
            self._win.activateWindow()
        else:
            self.show()

    def Tick(self) -> None:
        th = getattr(self._win.hud, "_reactor", None)
        changed = False
        if th is not None:
            th.size = int(self._side)          # small: costs a fraction of the window's frame
            fid, frame = th.frame()
            if fid != self._frame_id and frame is not None:
                self._frame_id, self._frame = fid, frame
                changed = True
        now = time.monotonic()
        if now - self._blink_t > 0.6:
            self._blink, self._blink_t = not self._blink, now
            changed = True
        if changed:
            self.update()

    # ── drawing ───────────────────────────────────────────────────────────────
    def Status(self) -> tuple[str, str]:
        hud = self._win.hud
        hint = getattr(hud, "hold_hint", "")
        dot = "●" if self._blink else "○"
        if hud.muted and not (hint and hud.speaking):
            return (f"⊘  HOLD {hint.upper()}" if hint else "⊘  MUTED"), C.MUTED_C
        if hud.speaking:
            return "●  SPEAKING", C.ACC
        if hud.state in ("THINKING", "PROCESSING"):
            return f"{'◈' if self._blink else '◇'}  {hud.state}", C.ACC2
        if hud.state == "LISTENING":
            return f"{dot}  LISTENING", C.GREEN
        return f"{dot}  {hud.state}", C.PRI

    def Caption(self) -> str:
        text, at = getattr(self._win.hud, "_cap_ai", ("", 0.0))
        if text and at and time.time() - at < _CAPTION_S:
            return " ".join(str(text).split())
        return ""

    def Pill(self, p: QPainter, y: float, text: str, colour: str, size: int, bold: bool) -> None:
        f = QFont(UI_FONT, size, QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
        if bold:
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2)
        fm = QFontMetrics(f)
        text = fm.elidedText(text, Qt.TextElideMode.ElideRight, self.width() - 40)
        w = fm.horizontalAdvance(text) + 24
        r = QRectF((self.width() - w) / 2, y, w, self._PILL_H)
        # Dark glass behind the words, so they read on a light wallpaper too.
        p.setPen(QPen(QColor(255, 255, 255, 26), 1))
        p.setBrush(QColor(8, 14, 22, 205))
        p.drawRoundedRect(r, self._PILL_H / 2, self._PILL_H / 2)
        p.setPen(QPen(Qcol(colour), 1))
        p.setFont(f)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        rect = self.ReactorRect()
        # Almost invisible, but solid to the mouse: a fully clear pixel lets
        # clicks fall through to the desktop, and the strands have gaps.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 2))
        p.drawEllipse(rect.adjusted(6, 6, -6, -6))
        if self._frame is not None:
            crisp, bloom = self._frame
            p.save()
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            p.drawImage(rect, bloom)
            p.drawImage(rect, crisp)
            p.restore()
        y = rect.bottom() + self._GAP
        text, colour = self.Status()
        self.Pill(p, y, text, colour, 8, True)
        cap = self.Caption()
        if cap:
            self.Pill(p, y + self._PILL_H + 4, cap, C.WHITE, 9, False)
        p.end()

    # ── mouse ─────────────────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._dragged = False
        elif e.button() == Qt.MouseButton.RightButton:
            self.Menu().exec(e.globalPosition().toPoint())

    def mouseMoveEvent(self, e):
        if self._press is None or not (e.buttons() & Qt.MouseButton.LeftButton):
            return
        to = e.globalPosition().toPoint() - self._press
        if self._dragged or (to - self.pos()).manhattanLength() > 4:
            self._dragged = True
            self.move(to)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or self._press is None:
            return
        self._press = None
        if self._dragged:
            Save(x=self.x(), y=self.y())
        elif self.ReactorRect().contains(QPointF(e.position())):
            # Wait out a double-click before opening the box.
            QTimer.singleShot(QApplication.doubleClickInterval(), self.MaybeType)
            self._clicks = getattr(self, "_clicks", 0) + 1

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._clicks = -1          # cancel the pending single click
            self._win.ShowWindow()

    def MaybeType(self) -> None:
        if getattr(self, "_clicks", 0) > 0:
            self.ToggleTyping()
        self._clicks = 0

    # ── typing ────────────────────────────────────────────────────────────────
    def ToggleTyping(self) -> None:
        if self._input.isVisible():
            self._input.hide()
            return
        self._input.show()
        self.activateWindow()
        self._input.setFocus()

    def SendTyped(self) -> None:
        txt = self._input.text().strip()
        self._input.clear()
        self._input.hide()
        if txt:
            self._win._input.setText(txt)
            self._win.Send()            # the window's own path: log line + on_text_command

    def eventFilter(self, obj, ev):
        from PyQt6.QtCore import QEvent
        if obj is self._input and ev.type() == QEvent.Type.KeyPress and ev.key() == Qt.Key.Key_Escape:
            self._input.clear()
            self._input.hide()
            return True
        return super().eventFilter(obj, ev)

    # ── menu (here and on the tray icon) ──────────────────────────────────────
    def Menu(self) -> QMenu:
        m = QMenu(self)
        self.FillMenu(m)
        return m

    def FillMenu(self, m: QMenu) -> None:
        """Fill `m` afresh (the tray reuses one menu, refilled as it opens)."""
        m.clear()
        for old in m.findChildren(QMenu):
            old.deleteLater()
        m.setStyleSheet(f"""
            QMenu {{ background: {C.PANEL}; color: {C.TEXT}; border: 1px solid {C.BORDER_B};
                     border-radius: 8px; padding: 4px; }}
            QMenu::item {{ padding: 6px 18px; border-radius: 5px; }}
            QMenu::item:selected {{ background: {C.PRI_GHO}; color: {C.WHITE}; }}
            QMenu::separator {{ height: 1px; background: {C.BORDER}; margin: 4px 8px; }}""")

        def Add(menu, text, fn, checked=None):
            a = QAction(text, menu)
            if checked is not None:
                a.setCheckable(True)
                a.setChecked(checked)
            a.triggered.connect(lambda _=False: fn())
            menu.addAction(a)
            return a

        Add(m, "Open Orion window", self._win.ShowWindow)
        if self.isVisible():
            Add(m, "Type to Orion", self.ToggleTyping)
        else:
            Add(m, "Show floating reactor", self._win.ShowFloating)
        Add(m, "Turn the mic on" if self._win._muted else "Mute the mic", self._win.ToggleMute)
        Add(m, "Stop talking", self._win.DoInterrupt)
        m.addSeparator()
        Add(m, "Settings", lambda: (self._win.ShowWindow(), self._win.OpenSettings("")))
        size = m.addMenu("Reactor size")
        for name, side in SIZES:
            Add(size, name, lambda s=side: self.SetSide(s), checked=(side == self._side))
        m.addSeparator()
        Add(m, "Quit Orion", self._win.QuitOrion)


def MakeTray(win, floating: FloatingReactor):
    """The tray icon: click to switch between the reactor and the window."""
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None
    tray = QSystemTrayIcon(QApplication.windowIcon(), win)
    tray.setToolTip(win._assistant_name.title() if win._assistant_name else "Orion")

    def Activated(reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if win.isVisible():
                win.ShowFloating()
            else:
                win.ShowWindow()

    tray.activated.connect(Activated)
    menu = QMenu()
    # Refilled each time it opens, so "Mute"/"Turn on" and the size tick are current.
    menu.aboutToShow.connect(lambda: floating.FillMenu(menu))
    floating.FillMenu(menu)
    tray.setContextMenu(menu)
    tray._menu = menu                  # keep it alive with the icon
    tray.show()
    return tray
