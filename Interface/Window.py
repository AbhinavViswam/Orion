from __future__ import annotations

import json
import math
import os
import platform
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import psutil

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}

# Qt's video backend prints the ffmpeg stream banner — codec, bitrate, the
# whole signed googlevideo URL — to the console for every stream it opens. That
# is two screenfuls per video, it looks like an error to anyone reading the log,
# and it puts a URL carrying the viewer's IP address into a terminal people
# paste into bug reports. Silenced here, before Qt initialises its logging.
# An explicit setting from the user is left alone.
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")

from PyQt6.QtCore import (
    QEasingCurve, QLineF, QMimeData, QObject, QParallelAnimationGroup, QPointF,
    QPoint, QPropertyAnimation, QRect, QRectF, QSize, QSizeF, Qt, QTimer,
    QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QDragEnterEvent, QDropEvent, QFont,
    QFontDatabase, QFontMetrics, QIcon, QKeySequence, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QRadialGradient, QShortcut,
)
# Video playback for the HUD. Part of PyQt6, so it costs no new dependency —
# but the multimedia plugins are a separate piece of the Qt install and can be
# absent on a stripped-down system, so a failure here disables one feature
# rather than stopping ULTRON from starting.
try:
    from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem
    HAVE_VIDEO = True
except Exception as _e:            # noqa: BLE001 - reported, never fatal
    QAudioOutput = QMediaPlayer = QGraphicsVideoItem = None
    HAVE_VIDEO = False
    print(f"[Video] playback unavailable ({_e}) — the HUD will not show video.")

from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QGraphicsScene, QGraphicsView,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QProgressBar, QSlider,
)

try:
    from Interface.Reactor import ReactorThread, StateOf as reactor_state
except Exception:      # pragma: no cover — HUD must never die over cosmetics
    ReactorThread = None


def BaseDir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR   = BaseDir()
CONFIG_DIR = BASE_DIR / "Config"
API_FILE   = CONFIG_DIR / "ApiKeys.json"


def ReadFullConfig() -> dict:
    """Read ApiKeys.json config dict. Returns {} on any error."""
    try:
        return json.loads(API_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# Single source of truth for the release name — the window title, the header
# badge and the readme must never disagree again.
APP_VERSION  = "V2"
APP_PROTOCOL = APP_VERSION.split()[-1]

_DEFAULT_W, _DEFAULT_H = 980, 700
_MIN_W,     _MIN_H     = 820, 580
_LEFT_W  = 210          # the stats card
_RIGHT_W = 380          # the conversation drawer
_TOP_H   = 58           # the top bar
_DOCK_H  = 82           # the input dock

# One clean UI font everywhere. Segoe UI ships with every Windows; the others
# are their platforms' own.
UI_FONT = {"Windows": "Segoe UI", "Darwin": "Helvetica Neue"}.get(platform.system(), "Noto Sans")

# Glass surfaces: one look for every floating panel.
GLASS_BG     = "rgba(8, 14, 22, 0.86)"
GLASS_BORDER = "rgba(255, 255, 255, 0.08)"
GLASS_HOVER  = "rgba(255, 255, 255, 0.06)"

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


class C:
    BG        = "#03060a"
    PANEL     = "#0a0f15"
    PANEL2    = "#0c121a"
    BORDER    = "#1c2632"
    BORDER_B  = "#2e3c4a"
    BORDER_A  = "#223040"
    PRI       = "#dcefff"
    PRI_DIM   = "#7f93a6"
    PRI_GHO   = "#141c26"
    ACC       = "#f4f9ff"
    ACC2      = "#c6dcf5"
    GREEN     = "#a9f0d1"
    GREEN_D   = "#5fae8c"
    RED       = "#ff6b81"
    MUTED_C   = "#ff6b81"
    TEXT      = "#e6f2fb"
    TEXT_DIM  = "#5f7182"
    TEXT_MED  = "#94a7b9"
    WHITE     = "#f4faff"
    DARK      = "#05090e"
    BAR_BG    = "#0d141c"


# Keys tied to the accent colour — status colours (ACC, GREEN, RED…) stay fixed
_HUE_LINKED = (
    "BG", "PANEL", "PANEL2", "BORDER", "BORDER_B", "BORDER_A",
    "PRI", "PRI_DIM", "PRI_GHO", "TEXT", "TEXT_DIM", "TEXT_MED",
    "WHITE", "DARK", "BAR_BG",
)
_PALETTE_DEFAULTS: dict[str, str] = {k: getattr(C, k) for k in _HUE_LINKED}

DEFAULT_UI_COLOR = _PALETTE_DEFAULTS["PRI"]


def ApplyUiAccent(accent_hex: str) -> bool:
    """
    Re-derives the whole teal-family palette from the chosen accent colour
    (hue shift — brightness/saturation ratios are preserved, design stays intact).
    Painted elements (HUD, waveform, metrics) pick up the new colour on the next
    frame; stylesheet-based panels pick it up when they are rebuilt.
    """
    import colorsys

    accent_hex = (accent_hex or "").strip().lower()
    if not (accent_hex.startswith("#") and len(accent_hex) == 7):
        return False
    try:
        int(accent_hex[1:], 16)
    except ValueError:
        return False

    def _Hsv(h: str) -> tuple[float, float, float]:
        r = int(h[1:3], 16) / 255
        g = int(h[3:5], 16) / 255
        b = int(h[5:7], 16) / 255
        return colorsys.rgb_to_hsv(r, g, b)

    base_h            = _Hsv(_PALETTE_DEFAULTS["PRI"])[0]
    acc_h, acc_s, _av = _Hsv(accent_hex)
    dh   = acc_h - base_h
    grey = acc_s < 0.08   # near-grey accent → the whole theme is desaturated

    for key, hex0 in _PALETTE_DEFAULTS.items():
        h, s, v = _Hsv(hex0)
        if grey:
            s *= 0.15
        r, g, b = colorsys.hsv_to_rgb((h + dh) % 1.0, s, v)
        setattr(C, key, "#{:02x}{:02x}{:02x}".format(
            int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5)))
    return True


def CurrentPalette() -> dict[str, str]:
    """A snapshot of the accent-linked colours currently on class C."""
    return {k: getattr(C, k) for k in _HUE_LINKED}


def RethemeAllWidgets(old: dict[str, str], new: dict[str, str]) -> None:
    """
    LIVE full theme change. Replaces the old palette colours with the new ones
    in EVERY widget's stylesheet across the app and repaints them. This way the
    colour change applies INSTANTLY across the whole interface — panels, buttons,
    borders included — not just the painted elements. No restart needed.
    """
    mapping = {old[k].lower(): new[k].lower()
               for k in old if old[k].lower() != new.get(k, old[k]).lower()}
    if not mapping:
        return
    app = QApplication.instance()
    if app is None:
        return
    for w in app.allWidgets():
        try:
            ss = w.styleSheet()
            if ss:
                s2 = ss
                for o, n in mapping.items():
                    if o in s2:
                        s2 = s2.replace(o, n)
                if s2 != ss:
                    w.setStyleSheet(s2)
            w.update()
        except Exception:
            pass


def MicIcon(color: str, muted: bool, px: int = 20) -> QIcon:
    """A line-drawn microphone (with a slash through it when muted)."""
    scale = 3                                   # drawn big, so it stays crisp on HiDPI
    n = px * scale
    pm = QPixmap(n, n)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), n * 0.085)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    # capsule
    w, h = n * 0.30, n * 0.46
    p.drawRoundedRect(QRectF((n - w) / 2, n * 0.10, w, h), w / 2, w / 2)
    # cradle arc
    p.drawArc(QRectF(n * 0.24, n * 0.26, n * 0.52, n * 0.46), 200 * 16, 140 * 16)
    # stand and base
    p.drawLine(QPointF(n / 2, n * 0.72), QPointF(n / 2, n * 0.86))
    p.drawLine(QPointF(n * 0.36, n * 0.88), QPointF(n * 0.64, n * 0.88))
    if muted:
        p.drawLine(QPointF(n * 0.18, n * 0.14), QPointF(n * 0.82, n * 0.88))
    p.end()
    pm.setDevicePixelRatio(scale)
    return QIcon(pm)


def Qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h); c.setAlpha(a); return c


# ── Windows GPU via NVML DLL (no subprocess, no console window) ──────────────
_nvml_lib: object = None   # cached ctypes DLL
_nvml_ok:  object = None   # None=untested, True=works, False=unavailable


def NvmlGpuWindows() -> float:
    """Return NVIDIA GPU utilisation % using nvml.dll directly — zero subprocess."""
    global _nvml_lib, _nvml_ok
    if _nvml_ok is False:
        return -1.0
    try:
        import ctypes

        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        if _nvml_lib is None:
            for dll_name in ("nvml", r"C:\Windows\System32\nvml.dll"):
                try:
                    lib = ctypes.WinDLL(dll_name)
                    lib.nvmlInit_v2()
                    _nvml_lib = lib
                    break
                except Exception:
                    continue

        if _nvml_lib is None:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            _nvml_ok = True
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)

        dev = ctypes.c_void_p()
        _nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
        util = _Util()
        _nvml_lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(util))
        _nvml_ok = True
        return float(util.gpu)
    except Exception:
        _nvml_ok = False
        return -1.0


class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0   
        self.gpu  = -1.0  
        self.tmp  = -1.0  
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._running = True
        # Probe caches — GPU (NVML) and temperature (WMI) are the expensive
        # queries; initialise their handles once and reuse them instead of
        # rebuilding a connection on every poll.
        self._slow_tick = 0            # gpu/temp refreshed every 3rd cycle
        self._pynvml    = None         # cached pynvml module + device handle
        self._pynvml_h  = None
        self._pynvml_ok = None         # None=untested, False=unavailable here
        self._nv_unix   = None         # cached (lib, dev) for Linux/macOS NVML
        self._wmi_conn  = None         # cached WMI connection (creating one is slow)
        self._wmi_ok    = None         # None=untested, False=unavailable here
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while self._running:
            try:
                self.Update()
            except Exception:
                pass
            time.sleep(2.0)

    def Update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net  = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net   = nc
        self._last_net_t = now

        # GPU and temperature change slowly and are the most expensive probes
        # (NVML / WMI) — refresh them every 3rd cycle (~6 s) instead of every
        # cycle, reusing the previous reading in between.
        self._slow_tick = (self._slow_tick + 1) % 3
        if self._slow_tick == 1:
            gpu = self.GetGpu()
            tmp = self.GetTemp()
        else:
            gpu = self.gpu
            tmp = self.tmp

        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp

    def GetGpu(self) -> float:
        # pynvml — subprocess-free; initialise once and reuse the handle.
        # Re-initialising NVML on every poll is slow, so cache it and stop
        # retrying pynvml entirely once it proves unavailable here.
        if self._pynvml_ok is not False:
            try:
                if self._pynvml_h is None:
                    import pynvml  # type: ignore
                    pynvml.nvmlInit()
                    self._pynvml    = pynvml
                    self._pynvml_h  = pynvml.nvmlDeviceGetHandleByIndex(0)
                    self._pynvml_ok = True
                return float(self._pynvml.nvmlDeviceGetUtilizationRates(self._pynvml_h).gpu)
            except Exception:
                self._pynvml_ok = False

        # Windows: nvml.dll via ctypes (already cached in _nvml_gpu_windows)
        if _OS == "Windows":
            return NvmlGpuWindows()

        # Linux / macOS: libnvidia-ml shared lib via ctypes — init once, reuse
        try:
            import ctypes

            class _Util(ctypes.Structure):
                _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

            if self._nv_unix is None:
                _lib = "libnvidia-ml.so.1" if _OS == "Linux" else "libnvidia-ml.dylib"
                nv = ctypes.CDLL(_lib)
                nv.nvmlInit_v2()
                dev = ctypes.c_void_p()
                nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
                self._nv_unix = (nv, dev)

            nv, dev = self._nv_unix
            u = _Util()
            nv.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u))
            return float(u.gpu)
        except Exception:
            pass

        return -1.0   # N/A — zero subprocess on all platforms

    def GetTemp(self) -> float:
        # psutil — works on Linux; occasionally Windows with driver support
        try:
            temps = psutil.sensors_temperatures()
            for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                         "cpu-thermal", "zenpower", "it8688"]:
                if name in temps and temps[name]:
                    return temps[name][0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass

        # Windows: wmi module (pure Python COM, zero subprocess). Reuse a single
        # connection — building a fresh wmi.WMI() on every poll spins up a COM
        # connection each time and is very slow. Give up after one failure.
        if _OS == "Windows" and self._wmi_ok is not False:
            try:
                if self._wmi_conn is None:
                    import wmi  # type: ignore
                    self._wmi_conn = wmi.WMI(namespace="root/wmi")
                tz = self._wmi_conn.MSAcpi_ThermalZoneTemperature()
                if tz:
                    return (tz[0].CurrentTemperature / 10.0) - 273.15
            except Exception:
                self._wmi_ok   = False
                self._wmi_conn = None

        return -1.0   # N/A — zero subprocess on all platforms

    def Snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu": self.cpu,
                "mem": self.mem,
                "net": self.net,
                "gpu": self.gpu,
                "tmp": self.tmp,
            }


_metrics = _SysMetrics()

class HudCanvas(QWidget):
    """The centre of the window: Ultron's reactor, the status line and the
    voice waveform."""

    def __init__(self, assistant_name: str = "ULTRON", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.muted    = False
        self.speaking = False
        self.state    = "INITIALISING"
        self._assistant_name = assistant_name

        # The 3D strand reactor (core/reactor.py), rendered on its own thread at
        # up to 60 fps. This widget only shows the newest finished frame, so
        # neither ever waits for the other. If it could not be built the panel
        # still shows the status line and waveform, never a crash.
        self._reactor = None
        # Width covered on the right by the conversation drawer; everything
        # centres in what is left, so the reactor is never under the drawer.
        self.right_inset = 0
        self._frame_id = 0
        self._frame = None
        if ReactorThread is not None:
            try:
                self._reactor = ReactorThread(self)
                self._reactor.start()
                app = QApplication.instance()
                if app is not None:
                    app.aboutToQuit.connect(self._reactor.stop)
            except Exception:
                self._reactor = None

        self._tick       = 0
        self._step_t     = time.time()
        self._blink      = True
        self._blink_tick = 0
        # Repaint throttle counter (idle frames drop to ~20 Hz — see _step()).
        self._paint_tick = 0

        # Live audio reactivity: _live_amp is written from the audio threads
        # (0.0–1.0), _amp_disp is the smoothed value the paint code reads.
        self._live_amp  = 0.0
        self._amp_disp  = 0.0
        # (frames, start_time, hop) posted by the playback thread — see
        # push_visemes(). None means "no schedule; use the plain level".
        self._visemes = None
        self._vis_i = None        # first schedule frame not yet played

        # The last exchange, shown softly under the reactor: (text, when).
        self._cap_you = ("", 0.0)
        self._cap_ai = ("", 0.0)

        # A precise timer: the default one may wander by a few ms each tick,
        # which shows as judder in a moving image.
        self._tmr = QTimer(self)
        self._tmr.setTimerType(Qt.TimerType.PreciseTimer)
        self._tmr.timeout.connect(self.Step)
        self._tmr.start(8)

    def PushVisemes(self, frames, hop: float, at: float) -> None:
        """Thread-safe: hand over a schedule of 20 ms voice-level frames.

        The playback thread writes up to 200 ms of audio in one go; posting the
        whole slice as 20 ms frames lets `_step()` play them out against the wall
        clock, so the reactor moves in step with the sound rather than five
        times a second. `at` is when this batch begins to sound, and batches are
        appended into one timeline so a long reply never stutters at the joins.
        """
        try:
            if not frames:
                return
            hop = max(1e-3, float(hop))
            at = float(at)
            new = list(frames)
            cur = self._visemes
            if cur is not None:
                old, t0, ohop = cur
                if abs(ohop - hop) < 1e-6:
                    i = int(round((at - t0) / hop))
                    if 0 <= i <= len(old) + 1:
                        merged = old[:i] + new
                        played = int((time.time() - t0) / hop) - 2
                        if played > 60:
                            merged = merged[played:]
                            t0 += played * hop
                            if self._vis_i is not None:
                                self._vis_i = max(0, self._vis_i - played)
                        self._visemes = (merged, t0, hop)
                        return
            self._visemes = (new, at, hop)
            self._vis_i = None
        except Exception:
            pass

    def SetCaption(self, who: str, text: str) -> None:
        """Show `text` under the reactor as the user's line ("you") or the
        assistant's ("ai"). A new line from the user clears the old answer."""
        now = time.time()
        if who == "you":
            self._cap_you = (text, now)
            self._cap_ai = ("", 0.0)
        else:
            self._cap_ai = (text, now)
        self.update()

    def PaintCaptions(self, p: QPainter, W: float, y: float) -> None:
        """Two quiet lines: what you said, then the answer. They fade after a
        while so the reactor has the window to itself again."""
        now = time.time()
        max_w = int(min(W * 0.78, 760))
        for (text, at), size, col, dy in (
                (self._cap_you, 10, C.TEXT_MED, 0),
                (self._cap_ai, 11, C.WHITE, 22)):
            if not text or not at:
                continue
            age = now - at
            fade = 1.0 if age < 14 else max(0.0, 1.0 - (age - 14) / 2.0)
            if fade <= 0:
                continue
            f = QFont(UI_FONT, size)
            p.setFont(f)
            line = QFontMetrics(f).elidedText(" ".join(text.split()),
                                              Qt.TextElideMode.ElideRight, max_w)
            c = Qcol(col)
            c.setAlphaF(0.9 * fade)
            p.setPen(QPen(c, 1))
            p.drawText(QRectF(0, y + dy, W, 20), Qt.AlignmentFlag.AlignCenter, line)

    def SetAudioLevel(self, level: float) -> None:
        """Thread-safe entry point for the audio threads. Stores the louder of
        the incoming level and the current value so brief gaps between chunks
        don't make the waveform stutter; _step() decays it back down."""
        try:
            lv = float(level)
        except (TypeError, ValueError):
            return
        lv = 0.0 if lv < 0.0 else (1.0 if lv > 1.0 else lv)
        if lv > self._live_amp:
            self._live_amp = lv

    def Step(self):
        self._tick += 1
        now = time.time()

        # Ultron's own voice, from the schedule if one is playing (every frame
        # since the last tick, so short peaks are not skipped).
        sched = self._visemes
        if sched is not None:
            frames, t0, hop = sched
            i = int((now - t0) / hop)
            if 0 <= i < len(frames):
                j = self._vis_i if self._vis_i is not None else i
                seq = frames[max(0, j):i + 1]
                self._vis_i = max(j, i + 1)
                if seq:
                    peak = max(f[0] for f in seq)
                    if peak > self._live_amp:
                        self._live_amp = peak
            elif i >= len(frames):
                self._visemes = None        # schedule spent
                self._vis_i = None

        # Decay toward silence so gaps fade instead of freezing, then smooth.
        self._live_amp *= 0.86
        self._amp_disp += (self._live_amp - self._amp_disp) * 0.45
        amp = self._amp_disp

        self._step_t = now
        visible = self.OnScreen()
        new_frame = False
        if self._reactor is not None:
            th = self._reactor
            th.state = reactor_state(self.state, self.speaking, self.muted)
            th.level = amp
            th.tint = Qcol(C.PRI)
            th.visible = visible
            fid, frame = th.frame()
            if fid != self._frame_id and frame is not None:
                self._frame_id, self._frame = fid, frame
                new_frame = True

        self._blink_tick += 1
        if self._blink_tick >= 38:
            self._blink = not self._blink
            self._blink_tick = 0
            _blinked = True
        else:
            _blinked = False

        # Repaint whenever the reactor has a new frame (up to 60 a second), and
        # for the status blink; otherwise the waveform alone at ~30 Hz.
        self._paint_tick = (self._paint_tick + 1) % 4
        if visible and (new_frame or _blinked or self._paint_tick == 0):
            self.update()

    def OnScreen(self) -> bool:
        """True only when this canvas can actually be seen by the user."""
        try:
            if not self.isVisible():
                return False
            win = self.window()
            return not (win.isMinimized() or win.isHidden())
        except Exception:
            return True      # never let a visibility check stop the HUD drawing

    def PaintStars(self, p: QPainter, W: float, H: float) -> None:
        """A faint, slowly twinkling starfield behind the reactor."""
        r = self._reactor.reactor
        s = r.stars
        t = r.t
        dim = 0.45 + 0.55 * min(1.0, r.brightness())
        tw = 0.35 + 0.65 * (0.5 + 0.5 * np.sin(t * 0.9 + s[:, 3] * 3.0))
        xs = ((s[:, 0] + t * 0.002) % 1.0) * W
        ys = s[:, 1] * H
        base = Qcol(C.WHITE)
        for lo, hi, a in ((0.0, 0.55, 0.18), (0.55, 0.85, 0.34), (0.85, 1.01, 0.6)):
            m = (tw >= lo) & (tw < hi)
            if not m.any():
                continue
            c = QColor(base)
            c.setAlphaF(a * dim)
            p.setPen(QPen(c, 1.4))
            p.drawPoints([QPointF(x, y) for x, y in zip(xs[m].tolist(), ys[m].tolist())])

    def Colours(self):
        """The reactor's two colours for the current state."""
        if self.muted:
            return Qcol(C.MUTED_C), Qcol(C.MUTED_C)
        if self.speaking:
            return Qcol(C.PRI), Qcol(C.ACC)
        if self.state in ("THINKING", "PROCESSING"):
            return Qcol(C.PRI), Qcol(C.ACC2)
        if self.state == "LISTENING":
            return Qcol(C.PRI), Qcol(C.GREEN)
        return Qcol(C.PRI), Qcol(C.PRI_DIM)

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():      # device not ready (e.g. 0-size during layout) — skip cleanly
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), Qcol(C.BG))

        W, H = max(120, self.width() - self.right_inset), self.height()
        cx, cy = W / 2, H / 2
        fw = min(W, H)

        # The reactor fills the band between the top of the canvas and the
        # status line, at any window size, without touching the text below.
        # Status near the bottom; the reactor fills what is above the captions.
        _sy_status = H - 62
        _cap_y = _sy_status - 52
        if self._reactor is not None:
            self.PaintStars(p, W, H)
            _band_t = 4.0
            _band_h = max(60.0, _cap_y - 4.0 - _band_t)
            side = min(W * 0.92, _band_h * 1.05)
            # Render no bigger than this and let Qt scale it up smoothly: it
            # costs a third of the work and keeps the strands continuous.
            self._reactor.size = int(min(side, 440))
            if self._frame is not None:
                crisp, bloom = self._frame
                rect = QRectF(cx - side / 2, _band_t + _band_h / 2 - side / 2, side, side)
                p.save()
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
                p.drawImage(rect, bloom)
                p.drawImage(rect, crisp)
                p.restore()

        # status text
        sy = _sy_status
        if self.muted:
            txt, col = "⊘  MUTED",     Qcol(C.MUTED_C)
        elif self.speaking:
            txt, col = "●  SPEAKING",  Qcol(C.ACC)
        elif self.state == "THINKING":
            sym = "◈" if self._blink else "◇"
            txt, col = f"{sym}  THINKING",   Qcol(C.ACC2)
        elif self.state == "PROCESSING":
            sym = "▷" if self._blink else "▶"
            txt, col = f"{sym}  PROCESSING", Qcol(C.ACC2)
        elif self.state == "LISTENING":
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  LISTENING",  Qcol(C.GREEN)
        else:
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  {self.state}", Qcol(C.PRI)

        self.PaintCaptions(p, W, _cap_y)

        p.setPen(QPen(col, 1))
        sf = QFont(UI_FONT, 9, QFont.Weight.DemiBold)
        sf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3)
        p.setFont(sf)
        p.drawText(QRectF(0, sy, W, 22), Qt.AlignmentFlag.AlignCenter, txt)

        # waveform — reacts to the real audio level (mic while listening,
        # ULTRON's own voice while speaking), a gentle ripple when quiet.
        wy = sy + 18
        N, bw = 36, 8
        wx0 = (W - N * bw) / 2
        amp = self._amp_disp
        mid = (N - 1) / 2.0
        for i in range(N):
            if self.muted:
                hgt, cl = 2, Qcol(C.MUTED_C)
            else:
                env     = (1.0 - abs(i - mid) / mid) ** 0.7      # center-weighted hump
                shimmer = 0.55 + 0.45 * math.sin(self._tick * 0.18 + i * 0.7)
                idle    = 3.0 + 2.0 * math.sin(self._tick * 0.09 + i * 0.6)
                hgt     = int(max(2, min(24, idle + amp * 22.0 * env * shimmer)))
                if amp > 0.05:
                    cl = Qcol(C.PRI) if hgt > 12 else Qcol(C.PRI_DIM)
                else:
                    cl = Qcol(C.BORDER_B)
            p.fillRect(QRectF(wx0 + i * bw, wy + 20 - hgt, bw - 1, hgt), cl)

        p.end()   # end deterministically so the backing store never flushes an active painter

class MetricBar(QWidget):

    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0       # 0–100
        self._text  = "--"
        self.setFixedHeight(38)
        self.setMinimumWidth(80)

    def _SetValue(self, pct: float, text: str):
        v = max(0.0, min(100.0, pct))
        if v == self._value and text == self._text:
            return          # unchanged — skip the repaint
        self._value = v
        self._text  = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        p.setBrush(QBrush(Qcol(C.PANEL2)))
        p.setPen(QPen(Qcol(C.BORDER_A), 1))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 4, 4)

        bar_h   = 4
        bar_y   = H - bar_h - 5
        bar_w   = W - 12
        bar_x   = 6
        fill_w  = int(bar_w * self._value / 100)

        p.setBrush(QBrush(Qcol(C.BAR_BG)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2, 2)

        if self._value > 85:
            bar_col = Qcol(C.RED)
        elif self._value > 65:
            bar_col = Qcol(C.ACC)
        else:
            bar_col = Qcol(self._color)

        if fill_w > 0:
            p.setBrush(QBrush(bar_col))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2, 2)

        p.setFont(QFont(UI_FONT, 8, QFont.Weight.Bold))
        p.setPen(QPen(Qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(8, 5, 50, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        p.setFont(QFont(UI_FONT, 10, QFont.Weight.Bold))
        p.setPen(QPen(bar_col if self._text != "--" else Qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, 4, W - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._text)

        p.end()

class LogWidget(QTextEdit):
    _sig = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        # Cap scrollback so an hours-long session can't grow the document
        # without bound — keeps memory flat and every insert cheap. Oldest
        # lines drop off the top automatically.
        self.document().setMaximumBlockCount(600)
        self.setFont(QFont(UI_FONT, 10))
        self.setStyleSheet(f"""
            QTextEdit {{
                background: {C.PANEL};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                padding: 6px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG};
                width: 8px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B};
                border-radius: 4px;
                min-height: 20px;
            }}
        """)
        self._queue: list[str] = []
        self._typing  = False
        self._text    = ""
        self._pos     = 0
        self._tag     = "sys"
        self._ai_name_lc = "ultron"   # updated when assistant name changes
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self.Step)
        self._sig.connect(self.Enqueue)

    def AppendLog(self, text: str):
        self._sig.emit(text)

    def Enqueue(self, text: str):
        self._queue.append(text)
        if not self._typing:
            self._next()

    def _next(self):
        if not self._queue:
            self._typing = False
            return
        self._typing = True
        self._text   = self._queue.pop(0)
        self._pos    = 0
        tl = self._text.lower()
        _ai_pfx = f"{self._ai_name_lc}:"
        if   tl.startswith("you:"):                              self._tag = "you"
        elif tl.startswith(_ai_pfx) or tl.startswith("ultron:"): self._tag = "ai"
        elif tl.startswith("file:"):                             self._tag = "file"
        elif "err" in tl:                                        self._tag = "err"
        else:                                                    self._tag = "sys"
        self._tmr.start(6)

    def Step(self):
        if self._pos < len(self._text):
            ch  = self._text[self._pos]
            cur = self.textCursor()
            fmt = cur.charFormat()
            col = {
                "you":  Qcol(C.WHITE),
                "ai":   Qcol(C.PRI),
                "err":  Qcol(C.RED),
                "file": Qcol(C.GREEN),
                # SYS lines are the bulk of the log. Amber fought the cyan HUD
                # and, being a fixed status colour rather than a hue-linked one,
                # stayed amber even after the accent picker retinted everything
                # else. TEXT_MED follows the theme and drops the contrast to a
                # level you can read past.
                "sys":  Qcol(C.TEXT_MED),
            }.get(self._tag, Qcol(C.TEXT))
            fmt.setForeground(QBrush(col))
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText(ch, fmt)
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            self._pos += 1
        else:
            self._tmr.stop()
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText("\n")
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            QTimer.singleShot(20, self._next)

_FILE_ICONS = {
    "image":   ("🖼", "#00d4ff"), "video":   ("🎬", "#ff6b00"),
    "audio":   ("🎵", "#cc44ff"), "pdf":     ("📄", "#ff4444"),
    "word":    ("📝", "#4488ff"), "excel":   ("📊", "#44bb44"),
    "code":    ("💻", "#ffcc00"), "archive": ("📦", "#ff8844"),
    "pptx":    ("📊", "#ff6622"), "text":    ("📃", "#aaaaaa"),
    "data":    ("🔧", "#88ddff"), "unknown": ("📎", "#888888"),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg","jpeg","png","gif","webp","bmp","tiff","svg","ico"], "image"),
    **dict.fromkeys(["mp4","avi","mov","mkv","wmv","flv","webm","m4v"],         "video"),
    **dict.fromkeys(["mp3","wav","ogg","m4a","aac","flac","wma","opus"],        "audio"),
    **dict.fromkeys(["pdf"],                                                     "pdf"),
    **dict.fromkeys(["doc","docx"],                                              "word"),
    **dict.fromkeys(["xls","xlsx","ods"],                                        "excel"),
    **dict.fromkeys(["ppt","pptx"],                                              "pptx"),
    **dict.fromkeys(["py","js","ts","jsx","tsx","html","css","java","c","cpp",
                     "cs","go","rs","rb","php","swift","kt","sh","sql","lua"],   "code"),
    **dict.fromkeys(["zip","rar","tar","gz","7z","bz2","xz"],                   "archive"),
    **dict.fromkeys(["txt","md","rst","log"],                                    "text"),
    **dict.fromkeys(["csv","tsv","json","xml"],                                  "data"),
}

def FileCategory(path: Path) -> str:
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")

def FmtSize(size: int) -> str:
    if   size < 1024:    return f"{size} B"
    elif size < 1024**2: return f"{size/1024:.1f} KB"
    elif size < 1024**3: return f"{size/1024**2:.1f} MB"
    else:                return f"{size/1024**3:.1f} GB"


class FileDropZone(QWidget):
    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(100)
        self._current_file: str | None = None
        self._hovering  = False
        self._drag_over = False
        self._dash_offset = 0.0
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self.Animate)
        self._anim_tmr.start(40)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def Animate(self):
        # The marching-ants dashed border is only meaningful while the user is
        # hovering or dragging a file over the zone. When idle, skip the repaint
        # entirely instead of redrawing the whole zone 25×/s forever — that idle
        # repaint held the GIL and stole time from the audio/response threads.
        if not (self._hovering or self._drag_over):
            return
        self._dash_offset = (self._dash_offset + 0.8) % 20
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True; self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False; self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).is_file():
                self.SetFile(path)
        self._canvas.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.Browse()

    def enterEvent(self, e):
        self._hovering = True; self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False; self._canvas.update()

    def CurrentFile(self) -> str | None:
        return self._current_file

    def ClearFile(self):
        self._current_file = None; self._canvas.update()

    def Browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a file for ULTRON", str(Path.home()),
            "All Files (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Data (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if path:
            self.SetFile(path)

    def SetFile(self, path: str):
        self._current_file = path
        self._canvas.update()
        self.file_selected.emit(path)


class _DropCanvas(QWidget):
    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z    = self._z
        W, H = self.width(), self.height()
        pad  = 6
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)

        bg_col = Qcol("#001a24" if z._drag_over else ("#001218" if z._hovering else C.PANEL))
        p.setBrush(QBrush(bg_col)); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   border_col = Qcol(C.GREEN, 200)
        elif z._drag_over:    border_col = Qcol(C.PRI, 230)
        elif z._hovering:     border_col = Qcol(C.BORDER_B, 200)
        else:                 border_col = Qcol(C.BORDER, 160)

        pen = QPen(border_col, 1.5, Qt.PenStyle.DashLine)
        pen.setDashOffset(z._dash_offset)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   self.PaintFile(p, W, H)
        elif z._drag_over:    self.PaintDragOver(p, W, H)
        else:                 self.PaintIdle(p, W, H, z._hovering)

        p.end()

    def PaintIdle(self, p, W, H, hover):
        cx, cy = W / 2, H / 2
        col = Qcol(C.PRI_DIM if not hover else C.PRI)
        p.setPen(QPen(col, 2)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, cy - 14), QPointF(cx, cy + 4))
        p.drawLine(QPointF(cx - 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx + 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx - 14, cy + 4), QPointF(cx + 14, cy + 4))
        p.setFont(QFont(UI_FONT, 9))
        p.setPen(QPen(Qcol(C.PRI_DIM if not hover else C.TEXT), 1))
        p.drawText(QRectF(0, cy + 8, W, 16), Qt.AlignmentFlag.AlignCenter,
                   "Drop file here  or  Click to Browse")
        p.setFont(QFont(UI_FONT, 8))
        p.setPen(QPen(Qcol("#1a4a5a"), 1))
        p.drawText(QRectF(0, cy + 24, W, 14), Qt.AlignmentFlag.AlignCenter,
                   "Images · Video · Audio · PDF · Docs · Code · Data")

    def PaintDragOver(self, p, W, H):
        cx, cy = W / 2, H / 2
        p.setFont(QFont(UI_FONT, 20))
        p.setPen(QPen(Qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy - 24, W, 32), Qt.AlignmentFlag.AlignCenter, "⬇")
        p.setFont(QFont(UI_FONT, 9, QFont.Weight.Bold))
        p.setPen(QPen(Qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy + 12, W, 16), Qt.AlignmentFlag.AlignCenter, "Release to load")

    def PaintFile(self, p, W, H):
        path = Path(self._z._current_file)
        cat  = FileCategory(path)
        icon, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size_str = FmtSize(path.stat().st_size)
        ext_str  = path.suffix.upper().lstrip(".") or "FILE"

        block_x, block_w = 10, 60
        p.setFont(QFont("Segoe UI Emoji", 22) if _OS == "Windows" else QFont("Arial", 22))
        p.setPen(QPen(Qcol(icon_col), 1))
        p.drawText(QRectF(block_x, 0, block_w, H), Qt.AlignmentFlag.AlignCenter, icon)

        tx = block_x + block_w + 6
        tw = W - tx - 38

        p.setFont(QFont(UI_FONT, 9, QFont.Weight.Bold))
        p.setPen(QPen(Qcol(C.WHITE), 1))
        name = path.name if len(path.name) <= 34 else path.name[:31] + "..."
        p.drawText(QRectF(tx, H * 0.18, tw, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(QFont(UI_FONT, 8))
        p.setPen(QPen(Qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(tx, H * 0.18 + 18, tw, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{ext_str}  ·  {size_str}")

        p.setFont(QFont(UI_FONT, 7))
        p.setPen(QPen(Qcol("#1e5c6a"), 1))
        par = str(path.parent)
        if len(par) > 42: par = "…" + par[-41:]
        p.drawText(QRectF(tx, H * 0.18 + 34, tw, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, par)

        p.setFont(QFont(UI_FONT, 10, QFont.Weight.Bold))
        p.setPen(QPen(Qcol(C.RED, 180), 1))
        p.drawText(QRectF(W - 34, 0, 28, H), Qt.AlignmentFlag.AlignCenter, "✕")

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 34:
            z.ClearFile()
        else:
            z.mousePressEvent(e)


class _CameraPreview(QWidget):
    """Floating overlay that briefly shows what the camera captured."""

    _W, _H = 244, 188

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            _CameraPreview {{
                background: rgba(0, 6, 10, 242);
                border: 1px solid {C.PRI};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 5, 6, 6)
        lay.setSpacing(4)

        hdr = QHBoxLayout()
        title = QLabel("◈  VISUAL INPUT")
        title.setFont(QFont(UI_FONT, 8, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(title)
        hdr.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(16, 16)
        close_btn.setFont(QFont(UI_FONT, 9))
        close_btn.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent; border: none;"
        )
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        hdr.addWidget(close_btn)
        lay.addLayout(hdr)

        self._img_lbl = QLabel()
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setStyleSheet("background: transparent;")
        lay.addWidget(self._img_lbl)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

        self.hide()

    def ShowFrame(self, img_bytes: bytes) -> None:
        px = QPixmap()
        px.loadFromData(img_bytes)
        if not px.isNull():
            max_w = self._W - 12
            scaled = px.scaled(
                max_w, 160,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._img_lbl.setPixmap(scaled)
            self._img_lbl.setFixedSize(scaled.width(), scaled.height())
            self.adjustSize()
        self.show()
        self.raise_()
        self._timer.start(6_000)   # auto-dismiss after 6 s


class SetupOverlay(QWidget):
    done = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)

        detected = {"darwin": "mac", "windows": "windows"}.get(
            _OS.lower(), "linux"
        )
        self._sel_os = detected

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 22, 30, 22)
        layout.setSpacing(8)

        def Lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont(UI_FONT, font_size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        layout.addWidget(Lbl("◈  INITIALISATION REQUIRED", 13, True))
        layout.addWidget(Lbl("Configure ULTRON before first boot.", 9, color=C.PRI_DIM))
        layout.addSpacing(6)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep)
        layout.addSpacing(4)

        layout.addWidget(Lbl("GEMINI API KEY", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("AIza…")
        self._key_input.setFont(QFont(UI_FONT, 10))
        self._key_input.setFixedHeight(32)
        self._key_input.setStyleSheet(f"""
            QLineEdit {{
                background: #000d12; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        layout.addWidget(self._key_input)
        layout.addSpacing(12)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep2)
        layout.addSpacing(4)

        layout.addWidget(Lbl("OPERATING SYSTEM", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        det_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}[detected]
        layout.addWidget(Lbl(f"Auto-detected: {det_name}", 8, color=C.ACC2,
                               align=Qt.AlignmentFlag.AlignLeft))

        os_row = QHBoxLayout(); os_row.setSpacing(6)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows","⊞  Windows"),("mac","  macOS"),("linux","🐧  Linux")]:
            btn = QPushButton(label)
            btn.setFont(QFont(UI_FONT, 10, QFont.Weight.Bold))
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self.Sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        layout.addLayout(os_row)
        self.Sel(detected)
        layout.addSpacing(12)

        init_btn = QPushButton("▸  INITIALISE SYSTEMS")
        init_btn.setFont(QFont(UI_FONT, 10, QFont.Weight.Bold))
        init_btn.setFixedHeight(36)
        init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        init_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: {C.PRI_GHO}; border: 1px solid {C.PRI};
            }}
        """)
        init_btn.clicked.connect(self.Submit)
        layout.addWidget(init_btn)

    def Sel(self, key: str):
        self._sel_os = key
        pal = {"windows":(C.PRI,"#001a22"),"mac":(C.ACC2,"#1a1400"),"linux":(C.GREEN,"#001a0d")}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {fg}; color: {bg};
                        border: none; border-radius: 3px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #000d12; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px;
                    }}
                    QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
                """)

    def Submit(self):
        key = self._key_input.text().strip()
        if not key:
            self._key_input.setStyleSheet(
                self._key_input.styleSheet() +
                f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            return
        self.done.emit(key, self._sel_os)


class HueWheel(QWidget):
    """
    Circular colour picker. The user drags the handle (small white circle)
    around the wheel to choose from ALL hues. The filled circle in the centre
    is a live preview of the selected colour.
    """

    hue_picked    = pyqtSignal(str)   # while dragging (live)
    hue_committed = pyqtSignal(str)   # when the handle is released

    _RING = 16   # ring thickness (px)

    def __init__(self, initial_hex: str = DEFAULT_UI_COLOR, parent=None):
        super().__init__(parent)
        self.setFixedSize(148, 148)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hue  = 0.53
        self._drag = False
        self.SetColor(initial_hex)

    # ── API ──────────────────────────────────────────────────────────────────
    def color(self) -> str:
        return QColor.fromHsvF(self._hue, 1.0, 1.0).name()

    def SetColor(self, hex_str: str):
        c = QColor((hex_str or "").strip())
        if c.isValid() and c.hsvHueF() >= 0:
            self._hue = c.hsvHueF()
            self.update()

    # ── geometry helpers ─────────────────────────────────────────────────────
    def RingRect(self) -> QRectF:
        m = self._RING / 2 + 3
        return QRectF(self.rect()).adjusted(m, m, -m, -m)

    def HueFromPos(self, pos: QPointF) -> float:
        c  = QRectF(self.rect()).center()
        dx = pos.x() - c.x()
        dy = c.y() - pos.y()          # screen y goes down — flip to math axis
        ang = math.atan2(dy, dx)      # [-π, π], counter-clockwise
        return (ang / (2 * math.pi)) % 1.0

    # ── drawing ──────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect   = self.RingRect()
        center = rect.center()

        grad = QConicalGradient(center, 0)
        for i in range(0, 361, 20):
            grad.setColorAt(i / 360.0, QColor.fromHsvF((i % 360) / 360.0, 1.0, 1.0))
        p.setPen(QPen(QBrush(grad), self._RING))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(rect)

        # centre preview circle
        preview = QColor.fromHsvF(self._hue, 1.0, 1.0)
        inner   = rect.adjusted(30, 30, -30, -30)
        p.setPen(QPen(Qcol(C.BORDER_B), 1))
        p.setBrush(QBrush(preview))
        p.drawEllipse(inner)

        # draggable handle
        r   = rect.width() / 2
        ang = self._hue * 2 * math.pi
        hx  = center.x() + r * math.cos(ang)
        hy  = center.y() - r * math.sin(ang)
        p.setPen(QPen(QColor("#00060a"), 2))
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawEllipse(QPointF(hx, hy), 7.5, 7.5)
        p.end()

    # ── fare ─────────────────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        self._drag = True
        self._hue  = self.HueFromPos(e.position())
        self.update()
        self.hue_picked.emit(self.color())

    def mouseMoveEvent(self, e):
        if self._drag:
            self._hue = self.HueFromPos(e.position())
            self.update()
            self.hue_picked.emit(self.color())

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            self.hue_committed.emit(self.color())


class CustomizeOverlay(QWidget):
    """Floating overlay — change assistant name, user name, UI colour and voice."""

    saved = pyqtSignal(str, str, str, str)   # assistant_name, user_name, ui_color, voice
    _OW, _OH = 400, 588

    def __init__(self, assistant_name="ULTRON", user_name="",
                 ui_color=DEFAULT_UI_COLOR, voice="", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            CustomizeOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 18)
        lay.setSpacing(8)

        def Lbl(txt, fs=9, bold=False, color=C.PRI, align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt); w.setAlignment(align)
            w.setFont(QFont(UI_FONT, fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        _fs = (f"QLineEdit {{ background: #000d12; color: {C.TEXT}; "
               f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
               f"QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")

        lay.addWidget(Lbl("⚙  CUSTOMISE ASSISTANT", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        lay.addWidget(Lbl("ASSISTANT NAME", 8, color=C.TEXT_DIM,
                            align=Qt.AlignmentFlag.AlignLeft))
        self._name_input = QLineEdit(assistant_name)
        self._name_input.setFont(QFont(UI_FONT, 10))
        self._name_input.setFixedHeight(32)
        self._name_input.setStyleSheet(_fs)
        lay.addWidget(self._name_input)

        lay.addSpacing(4)
        lay.addWidget(Lbl("YOUR NAME  (leave blank for default sir / efendim)", 8,
                            color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        self._user_input = QLineEdit(user_name)
        self._user_input.setPlaceholderText("e.g.  Abhinav   (leave blank for auto)")
        self._user_input.setFont(QFont(UI_FONT, 10))
        self._user_input.setFixedHeight(32)
        self._user_input.setStyleSheet(_fs)
        lay.addWidget(self._user_input)

        # ── Assistant voice — Gemini prebuilt voices ─────────────────────────
        # Names are language-neutral proper nouns, so the row reads the same in
        # every locale. Selecting one and applying rebuilds the Live session.
        from Brain.Settings import AVAILABLE_VOICES, DEFAULT_VOICE
        lay.addSpacing(4)
        lay.addWidget(Lbl("ASSISTANT VOICE", 8, color=C.TEXT_DIM,
                            align=Qt.AlignmentFlag.AlignLeft))
        self._sel_voice   = (voice or DEFAULT_VOICE)
        if self._sel_voice not in AVAILABLE_VOICES:
            self._sel_voice = DEFAULT_VOICE
        self._voice_btns: dict[str, QPushButton] = {}
        voice_row = QHBoxLayout(); voice_row.setSpacing(4)
        for _v in AVAILABLE_VOICES:
            b = QPushButton(_v)
            b.setCheckable(True)
            b.setFixedHeight(28)
            b.setFont(QFont(UI_FONT, 9, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, name=_v: self.OnVoicePick(name))
            self._voice_btns[_v] = b
            voice_row.addWidget(b)
        lay.addLayout(voice_row)
        self.RefreshVoiceBtns()

        # ── UI colour — colour wheel ─────────────────────────────────────────
        lay.addSpacing(4)
        clr_hdr = QHBoxLayout()
        clr_hdr.addWidget(Lbl("UI COLOUR  —  drag the handle", 8,
                               color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        clr_hdr.addStretch()
        df_btn = QPushButton("DEFAULT")
        df_btn.setFixedSize(64, 20)
        df_btn.setFont(QFont(UI_FONT, 8, QFont.Weight.Bold))
        df_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        df_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        df_btn.clicked.connect(lambda: self._SetColor(DEFAULT_UI_COLOR))
        clr_hdr.addWidget(df_btn)
        lay.addLayout(clr_hdr)

        self._initial_color = (ui_color or DEFAULT_UI_COLOR).strip().lower()
        self._sel_color     = self._initial_color
        self.on_preview     = None   # callable(hex) — live preview; MainWindow wires it

        self._wheel = HueWheel(self._sel_color)
        wheel_row = QHBoxLayout()
        wheel_row.addStretch(); wheel_row.addWidget(self._wheel); wheel_row.addStretch()
        lay.addLayout(wheel_row)
        self._wheel.hue_picked.connect(self.OnWheelPick)
        self._wheel.hue_committed.connect(self.OnWheelCommit)

        self._hex_input = QLineEdit(self._sel_color)
        self._hex_input.setPlaceholderText("#00d4ff   (custom hex colour)")
        self._hex_input.setFont(QFont(UI_FONT, 10))
        self._hex_input.setFixedHeight(28)
        self._hex_input.setStyleSheet(_fs)
        self._hex_input.textEdited.connect(self.OnHexEdited)
        lay.addWidget(self._hex_input)

        lay.addSpacing(6)
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)

        save_btn = QPushButton("▸  APPLY CHANGES")
        save_btn.setFixedHeight(34)
        save_btn.setFont(QFont(UI_FONT, 10, QFont.Weight.Bold))
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        save_btn.clicked.connect(self._Save)
        btn_row.addWidget(save_btn)

        cancel_btn = QPushButton("CANCEL")
        cancel_btn.setFixedHeight(34)
        cancel_btn.setFont(QFont(UI_FONT, 10))
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel_btn.clicked.connect(self._Cancel)
        btn_row.addWidget(cancel_btn)
        lay.addLayout(btn_row)

    # ── voice selection ──────────────────────────────────────────────────────
    def OnVoicePick(self, name: str):
        self._sel_voice = name
        self.RefreshVoiceBtns()

    def RefreshVoiceBtns(self):
        """Highlight the selected voice pill; dim the rest."""
        for name, b in self._voice_btns.items():
            on = (name == self._sel_voice)
            b.setChecked(on)
            if on:
                b.setStyleSheet(f"""
                    QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI};
                        border: 1px solid {C.PRI}; border-radius: 3px; }}
                """)
            else:
                b.setStyleSheet(f"""
                    QPushButton {{ background: transparent; color: {C.TEXT_MED};
                        border: 1px solid {C.BORDER}; border-radius: 3px; }}
                    QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
                """)

    # ── colour flow ──────────────────────────────────────────────────────────
    def _SetColor(self, hx: str, update_wheel: bool = True, preview: bool = True):
        """Updates the selected colour; hex box + wheel stay in sync, theme is live-previewed."""
        self._sel_color = hx.strip().lower()
        self._hex_input.blockSignals(True)
        self._hex_input.setText(self._sel_color)
        self._hex_input.blockSignals(False)
        if update_wheel:
            self._wheel.SetColor(self._sel_color)
        if preview and self.on_preview:
            self.on_preview(self._sel_color)

    def OnWheelPick(self, hx: str):
        # While dragging: update the hex box, don't apply the theme yet
        self._sel_color = hx
        self._hex_input.blockSignals(True)
        self._hex_input.setText(hx)
        self._hex_input.blockSignals(False)

    def OnWheelCommit(self, hx: str):
        # Handle released → live-preview the whole interface
        self._SetColor(hx, update_wheel=False)

    def OnHexEdited(self, text: str):
        t = text.strip().lower()
        if t.startswith("#") and len(t) == 7:
            try:
                int(t[1:], 16)
            except ValueError:
                return
            self._SetColor(t, update_wheel=True, preview=True)

    def _Cancel(self):
        # If a preview was applied, revert to the colour from launch
        if self.on_preview and self._sel_color != self._initial_color:
            self.on_preview(self._initial_color)
        self.hide()

    def _Save(self):
        name = self._name_input.text().strip() or "ULTRON"
        user = self._user_input.text().strip()
        self.saved.emit(name, user, self._sel_color or DEFAULT_UI_COLOR, self._sel_voice)
        self.hide()


class PluginManagerOverlay(QWidget):
    """Floating overlay — lists discovered plugins with per-plugin ON/OFF toggles."""

    _OW = 420

    def __init__(self, plugins: list[dict], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            PluginManagerOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(6)

        hdr = QLabel("🧩  PLUGIN MANAGER")
        hdr.setFont(QFont(UI_FONT, 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(hdr)
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        if not plugins:
            empty = QLabel("No plugins installed yet. Drop a plugin file into the Plugins folder and restart.")
            empty.setFont(QFont(UI_FONT, 9))
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(empty)

        for p in plugins:
            lay.addLayout(self.BuildRow(p))

        lay.addSpacing(4)
        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(30)
        close_btn.setFont(QFont(UI_FONT, 10))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self.hide)
        lay.addWidget(close_btn)
        self.adjustSize()

    def BuildRow(self, p: dict) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(6)

        label_text = p["name"] if p["valid"] else f"{p['name']}  (⚠ {p['file']})"
        lbl = QLabel(label_text)
        lbl.setFont(QFont(UI_FONT, 9))
        lbl.setStyleSheet(f"color: {C.TEXT if p['valid'] else C.TEXT_DIM}; background: transparent;")
        lbl.setToolTip(p["description"] if p["valid"] else p["error"])
        lbl.setWordWrap(False)
        row.addWidget(lbl, stretch=1)

        btn = QPushButton()
        btn.setFixedSize(72, 24)
        btn.setFont(QFont(UI_FONT, 8, QFont.Weight.Bold))
        if not p["valid"]:
            btn.setText("BROKEN")
            btn.setEnabled(False)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
            """)
        else:
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.StyleToggle(btn, p["enabled"])
            btn.clicked.connect(lambda _, name=p["name"], b=btn: self._Toggle(name, b))
        row.addWidget(btn)
        return row

    def StyleToggle(self, btn: QPushButton, enabled: bool):
        if enabled:
            btn.setText("ON")
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            btn.setText("OFF")
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
            """)

    def _Toggle(self, name: str, btn: QPushButton):
        from Brain.Settings import GetPluginEnabled, SavePluginEnabled
        new_val = not GetPluginEnabled(name)
        SavePluginEnabled(name, new_val)
        self.StyleToggle(btn, new_val)


class _HudOverlay(QWidget):
    """Base for the floating panels placed by hand over the HUD.

    They are children of the central widget but sit in no layout, so Qt never
    invalidates the region they occupy when they hide or shrink: the HUD keeps
    painting around them and their last frame stays on screen as a ghost. Any
    overlay positioned with _centre_overlay needs this."""

    def hideEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            # Repaint exactly what we were covering, before we stop covering it.
            p.update(self.geometry())
        super().hideEvent(e)

    def closeEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            p.update(self.geometry())
        super().closeEvent(e)


class ConfirmBanner(_HudOverlay):
    """The gate in front of an action that cannot be taken back.

    The old confirmation was a tool parameter the model filled in itself, which
    means it confirmed its own shutdown requests. This is the interface asking,
    and the answer travels from a human finger to core/confirm.py without the
    model in the loop. Nothing blocks while it is up: the assistant keeps
    talking, so this costs no latency — unlike the old gate, which spent two
    tool round trips on every power command."""

    answered = pyqtSignal(bool)
    _OW = 430

    def __init__(self, title: str, detail: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ConfirmBanner {{
                background: rgba(14, 3, 0, 250);
                border: 1px solid {C.ACC};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)

        hdr = QLabel("⚠  CONFIRM")
        hdr.setFont(QFont(UI_FONT, 11, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.ACC}; background: transparent;")
        lay.addWidget(hdr)

        ttl = QLabel(title)
        ttl.setWordWrap(True)
        ttl.setFont(QFont(UI_FONT, 10, QFont.Weight.Bold))
        ttl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        lay.addWidget(ttl)

        if detail:
            dtl = QLabel(detail)
            dtl.setWordWrap(True)
            dtl.setFont(QFont(UI_FONT, 9))
            dtl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            lay.addWidget(dtl)

        row = QHBoxLayout(); row.setSpacing(8)

        yes = QPushButton("▸  CONFIRM")
        yes.setFixedHeight(32)
        yes.setFont(QFont(UI_FONT, 10, QFont.Weight.Bold))
        yes.setCursor(Qt.CursorShape.PointingHandCursor)
        yes.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.ACC};
                border: 1px solid {C.ACC}; border-radius: 3px; }}
            QPushButton:hover {{ background: rgba(255,107,0,40); }}
        """)
        yes.clicked.connect(lambda: self.answered.emit(True))
        row.addWidget(yes)

        no = QPushButton("CANCEL")
        no.setFixedHeight(32)
        no.setFont(QFont(UI_FONT, 10))
        no.setCursor(Qt.CursorShape.PointingHandCursor)
        no.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        no.clicked.connect(lambda: self.answered.emit(False))
        row.addWidget(no)
        lay.addLayout(row)

        # Default focus on CANCEL: if someone hits Enter without reading, the
        # safe answer wins.
        no.setDefault(True)
        no.setFocus()


class AudioDeviceOverlay(_HudOverlay):
    """Choose which microphone ULTRON listens to and which speakers it uses.

    Both audio streams used to open with no `device=` at all, so they always
    took the OS default — which on Windows moves by itself the moment a headset
    is plugged in. 'ULTRON can't hear me' is usually 'ULTRON is listening to the
    webcam'."""

    picked = pyqtSignal()      # emitted after Apply, when something changed

    # THESE EXIST BECAUSE QTimer.singleShot FROM A WORKER THREAD NEVER FIRES.
    #
    # The first version posted results back with `QTimer.singleShot(0, fn)` from
    # inside the audio thread. A QTimer belongs to the thread that creates it,
    # and that thread has no event loop — so the timer was created, never ran,
    # and the panel sat on "Speak now…" for ever while the recording finished
    # silently behind it. Reported as "I saw no sign of anything at all", which
    # is precisely what it looked like.
    #
    # A signal is the thread-safe way across: Qt queues it onto the receiving
    # object's thread, which for a widget is the GUI thread. Both carry only
    # plain values — never a widget — because the widgets belong to the GUI
    # thread and the worker must not touch them even to pass them along.
    _level_seen = pyqtSignal(int)          # 0-100, while recording
    _test_result = pyqtSignal(str, bool)   # sentence, was it good

    _OW = 460

    def __init__(self, parent=None):
        super().__init__(parent)
        from Voice.AudioDevices import ListDevices, DEFAULT_LABEL
        from Brain.Settings import GetInputDevice, GetOutputDevice

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            AudioDeviceOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(6)

        hdr = QLabel("🎧  AUDIO DEVICES")
        hdr.setFont(QFont(UI_FONT, 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        _combo_css = (
            f"QComboBox {{ background: #000d12; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
            f"QComboBox:hover {{ border-color: {C.BORDER_B}; }}"
            f"QComboBox QAbstractItemView {{ background: #000d12; color: {C.TEXT}; "
            f"selection-background-color: {C.PRI_GHO}; border: 1px solid {C.BORDER}; }}"
        )

        _test_css = (
            f"QPushButton {{ background: transparent; color: {C.TEXT_DIM}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; }}"
            f"QPushButton:hover {{ color: {C.PRI}; border-color: {C.PRI_DIM}; }}"
            f"QPushButton:disabled {{ color: {C.BORDER}; }}"
        )

        def Row(label: str, kind: str, current: str) -> QComboBox:
            cap = QLabel(label)
            cap.setFont(QFont(UI_FONT, 9))
            cap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(cap)

            box = QComboBox()
            box.setFont(QFont(UI_FONT, 10))
            box.setFixedHeight(30)
            box.setStyleSheet(_combo_css)
            # The list is served from a cache warmed on a background thread at
            # startup, so opening this panel never blocks the Qt thread on the
            # host audio API.
            box.addItem(DEFAULT_LABEL, "")
            for name in ListDevices(kind):
                box.addItem(name, name)
            idx = box.findData(current) if current else 0
            box.setCurrentIndex(idx if idx >= 0 else 0)
            if current and idx < 0:
                # Saved device is not plugged in right now. Show it rather than
                # silently resetting the user's choice to default.
                box.addItem(f"{current}  (not connected)", current)
                box.setCurrentIndex(box.count() - 1)

            # THE ONE CHECK THE STARTUP PROBE CANNOT MAKE.
            #
            # core/audio_devices decides at launch whether a device opens and
            # whether it moves audio, and it does that with silence. A muted
            # microphone, a level slider at zero and a privacy shutter all pass
            # every one of those tests and still deliver a flat line — the only
            # thing that separates them from a working microphone is a person
            # speaking into it.
            #
            # So the button is here, beside the dropdown, in the panel somebody
            # opens when ULTRON cannot hear them. It replaces a loose
            # `ses_testi.py` in the project root that nobody would ever find.
            btn_row = QHBoxLayout(); btn_row.setSpacing(6)
            btn_row.addWidget(box, 1)
            test = QPushButton("TEST")
            test.setFixedSize(64, 30)
            test.setFont(QFont(UI_FONT, 9, QFont.Weight.Bold))
            test.setCursor(Qt.CursorShape.PointingHandCursor)
            test.setStyleSheet(_test_css)
            btn_row.addWidget(test)
            lay.addLayout(btn_row)

            # A meter, because a static label is indistinguishable from a hang.
            # Three seconds of "Speak now" with nothing moving was reported as
            # "I saw no sign of anything" — correctly, since nothing on screen
            # responded to the user's voice.
            meter = QProgressBar()
            meter.setRange(0, 100)
            meter.setValue(0)
            meter.setTextVisible(False)
            meter.setFixedHeight(6)
            meter.setStyleSheet(
                f"QProgressBar {{ background: #000d12; border: 1px solid {C.BORDER}; "
                f"border-radius: 3px; }}"
                f"QProgressBar::chunk {{ background: {C.PRI}; border-radius: 2px; }}")
            meter.hide()
            lay.addWidget(meter)

            result = QLabel("")
            result.setWordWrap(True)
            result.setFont(QFont(UI_FONT, 8))
            result.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(result)

            test.clicked.connect(
                lambda _=False, k=kind, b=box, t=test, r=result, m=meter:
                    self.RunTest(k, b, t, r, m))
            return box

        # Which row is being tested right now. One at a time — the button is
        # disabled for the duration — so a single slot is enough, and it keeps
        # widgets off the signal payload.
        self._active = None                # (button, label, meter)
        self._level_seen.connect(self.OnLevel)
        self._test_result.connect(self.OnResult)

        self._in_box  = Row("MICROPHONE — what ULTRON hears you with",
                             "input", GetInputDevice())
        lay.addSpacing(4)
        self._out_box = Row("SPEAKERS — what ULTRON talks through",
                             "output", GetOutputDevice())

        # ── Output volume, beside the output device ──────────────────────────
        #
        # Choosing "Realtek(R) Audio" here and finding it far too quiet used to
        # mean leaving this panel, opening the system sound settings, finding
        # the same endpoint again and moving a slider there. The volume belongs
        # next to the device it applies to.
        _vol_now = None
        try:
            from Voice.AudioDevices import GetOutputVolume
            # The SELECTED output, not the system default. Somebody puts the
            # assistant on their speakers and keeps everything else in their
            # headphones; a slider labelled with one device that moves the
            # other is worse than no slider at all.
            _vol_now = GetOutputVolume(self._out_box.currentData() or "")
        except Exception:
            pass

        if _vol_now is not None:
            vcap = QLabel("OUTPUT VOLUME")
            vcap.setFont(QFont(UI_FONT, 9))
            vcap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addSpacing(4)
            lay.addWidget(vcap)

            vrow = QHBoxLayout(); vrow.setSpacing(8)
            self._vol = QSlider(Qt.Orientation.Horizontal)
            self._vol.setRange(0, 100)
            self._vol.setValue(int(_vol_now))
            self._vol.setFixedHeight(22)
            self._vol.setStyleSheet(
                f"QSlider::groove:horizontal {{ background: #000d12; height: 4px; "
                f"border: 1px solid {C.BORDER}; border-radius: 2px; }}"
                f"QSlider::sub-page:horizontal {{ background: {C.PRI_DIM}; "
                f"border-radius: 2px; }}"
                f"QSlider::handle:horizontal {{ background: {C.PRI}; width: 10px; "
                f"margin: -5px 0; border-radius: 5px; }}")
            self._vol_lbl = QLabel(f"{int(_vol_now)}%")
            self._vol_lbl.setFont(QFont(UI_FONT, 9))
            self._vol_lbl.setFixedWidth(40)
            self._vol_lbl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
            vrow.addWidget(self._vol, 1)
            vrow.addWidget(self._vol_lbl)
            lay.addLayout(vrow)

            # valueChanged fires for every pixel of a drag, and each one is a
            # COM call into the audio endpoint. sliderReleased fires once, when
            # the user has decided — the label still follows the handle so the
            # number moves while they drag.
            self._vol.valueChanged.connect(
                lambda v: self._vol_lbl.setText(f"{v}%"))
            self._vol.sliderReleased.connect(self.ApplyVolume)
            self._out_box.currentIndexChanged.connect(
                lambda _i: self.VolumeFollowsDevice())

        note = QLabel("Applying reconnects the session. Your conversation is kept.")
        note.setWordWrap(True)
        note.setFont(QFont(UI_FONT, 8))
        note.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        lay.addSpacing(6)
        lay.addWidget(note)

        row = QHBoxLayout(); row.setSpacing(8)
        ok = QPushButton("▸  APPLY")
        ok.setFixedHeight(32)
        ok.setFont(QFont(UI_FONT, 10, QFont.Weight.Bold))
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """)
        ok.clicked.connect(self._Apply)
        row.addWidget(ok)

        cancel = QPushButton("CLOSE")
        cancel.setFixedHeight(32)
        cancel.setFont(QFont(UI_FONT, 10))
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel.clicked.connect(self.hide)
        row.addWidget(cancel)
        lay.addLayout(row)

    def ApplyVolume(self):
        """Set the system output volume from the slider, off the Qt thread.

        The Windows path goes through COM, which can block for tens of
        milliseconds; that is not long enough to see but is long enough to
        stutter a drag, and there is no reason to make the interface wait for
        it.
        """
        import threading
        value = self._vol.value()
        device = self._out_box.currentData() or ""

        def Work():
            from Voice import AudioDevices
            if not AudioDevices.SetOutputVolume(value, device):
                print(f"[Audio] the volume of '{device}' could not be set "
                      f"to {value}%")

        threading.Thread(target=Work, daemon=True, name="audio-volume").start()

    def VolumeFollowsDevice(self):
        """Re-read the slider when the chosen output changes.

        Each device keeps its own level, so leaving the handle where it was
        would show the previous device's volume against the new device's name —
        and the next drag would then jump it there.
        """
        if not hasattr(self, "_vol"):
            return
        try:
            from Voice.AudioDevices import GetOutputVolume
            now = GetOutputVolume(self._out_box.currentData() or "")
        except Exception:
            now = None
        if now is None:
            return
        self._vol.blockSignals(True)
        self._vol.setValue(int(now))
        self._vol.blockSignals(False)
        self._vol_lbl.setText(f"{int(now)}%")

    def RunTest(self, kind, box, button, label, meter=None):
        """Record or play for real, off the Qt thread.

        Both calls block for seconds — recording waits for the user to speak,
        the tone waits for itself — and doing that on the Qt thread freezes the
        HUD, which is the mistake this project already paid for once with a slow
        import in the settings drawer.

        Results come back by signal, not by timer. See the note on
        `_level_seen` above for why that distinction is the whole feature.
        """
        import threading

        name = box.currentData() or ""
        self._active = (button, label, meter)
        button.setEnabled(False)
        label.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        label.setText("Speak now…" if kind == "input" else "Playing a tone…")
        if meter is not None and kind == "input":
            meter.setValue(0)
            meter.show()

        def Work():
            from Voice import AudioDevices
            try:
                if kind == "input":
                    res = AudioDevices.MeasureInput(
                        name,
                        on_level=lambda f: self._level_seen.emit(int(f * 100)))
                    text = f"peak {res['peak']}  —  {res['verdict']}"
                else:
                    res = AudioDevices.PlayTone(name)
                    text = res["verdict"]
                good = bool(res.get("ok", False))
            except Exception as e:
                text, good = f"The test itself failed: {e}", False
            self._test_result.emit(text, good)

        threading.Thread(target=Work, daemon=True, name=f"audio-test-{kind}").start()

    def OnLevel(self, value: int):
        """A live level, already on the GUI thread thanks to the signal."""
        if self._active and self._active[2] is not None:
            self._active[2].setValue(max(0, min(100, value)))

    def OnResult(self, text: str, good: bool):
        if not self._active:
            return
        button, label, meter = self._active
        self._active = None
        button.setEnabled(True)
        if meter is not None:
            meter.setValue(0)
            meter.hide()
        label.setStyleSheet(
            f"color: {C.PRI if good else C.TEXT_DIM}; background: transparent;")
        label.setText(text)

    def _Apply(self):
        from Brain.Settings import GetInputDevice, GetOutputDevice, SaveInputDevice, SaveOutputDevice
        new_in  = self._in_box.currentData()  or ""
        new_out = self._out_box.currentData() or ""
        changed = (new_in != GetInputDevice()) or (new_out != GetOutputDevice())
        SaveInputDevice(new_in)
        SaveOutputDevice(new_out)
        self.hide()
        # Only rebuild the session if something actually moved — a no-op Apply
        # should not cost a reconnect.
        if changed:
            self.picked.emit()


class MemoryOverlay(_HudOverlay):
    """Everything ULTRON has stored about you, and when it learned it.

    Memory used to be a 2200-character store that deleted its oldest entries
    when full and mentioned it only on stdout. The cap is gone; this panel is
    the other half of that change — a memory you cannot inspect is a memory you
    cannot trust, and 'delete' has to be something the person can do."""

    _OW = 520

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            MemoryOverlay {{
                background: rgba(0, 6, 10, 246);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(20, 16, 20, 16)
        self._lay.setSpacing(5)
        self.Rebuild()

    def ClearLayout(self):
        """Take every item out of the layout and detach it from the widget tree
        in this call.

        deleteLater() on its own is not enough: it queues destruction for the
        next event-loop pass, and until then the old rows are still children of
        this widget and still paint — which is what drew half of the previous
        panel over the new one. setParent(None) removes them from the tree now;
        deleteLater() then frees them safely."""
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                # hide() stops it painting in this frame; deleteLater() frees it
                # safely afterwards. setParent(None) would also stop the paint,
                # but it turns the widget into a top-level window for the moment
                # between the two calls, which is not something to leave lying
                # around inside a click handler.
                w.hide()
                w.deleteLater()
                continue
            sub = item.layout()
            if sub is not None:
                while sub.count():
                    si = sub.takeAt(0)
                    sw = si.widget()
                    if sw is not None:
                        sw.hide()
                        sw.deleteLater()
                sub.deleteLater()

    def Settle(self, before):
        """Size the panel to its content, re-centre it, and repaint what the old
        size covered.

        The re-size has to happen here rather than at the end of _rebuild
        because Qt has not polished the freshly-created children at that point,
        so the size hint it would read is the empty-layout one. Measured: a
        first adjustSize() returned 32 px for a panel whose content needed 155,
        and a second call — after the same widgets had been through the event
        loop — returned 155. So this runs twice: once now, once on the next
        turn, from _rebuild.

        The re-centre and the repaint are needed because the overlay is placed
        by hand and is in no layout: shrinking it leaves it off-centre and
        leaves its former pixels on screen, since nothing tells the parent that
        region changed. The repaint has to cover the union of the old and new
        rectangles."""
        self._lay.invalidate()
        self._lay.activate()
        self.updateGeometry()
        self.adjustSize()

        p = self.parentWidget()
        if p is None:
            self.update()
            return
        self.move(max(0, (p.width()  - self.width())  // 2),
                  max(0, (p.height() - self.height()) // 2))
        p.update(before.united(self.geometry()))
        self.update()

    def Rebuild(self):
        before = self.geometry()
        self.ClearLayout()

        from Brain.Memory import AllEntriesForUi

        hdr = QLabel("🧠  WHAT ULTRON REMEMBERS")
        hdr.setFont(QFont(UI_FONT, 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._lay.addWidget(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        self._lay.addWidget(sep)

        rows = AllEntriesForUi()

        cap = QLabel(f"{len(rows)} stored facts — newest first. "
                     f"Nothing here is sent anywhere; it lives in "
                     f"Brain/LongTerm.json on this machine.")
        cap.setWordWrap(True)
        cap.setFont(QFont(UI_FONT, 8))
        cap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._lay.addWidget(cap)

        if not rows:
            empty = QLabel("Nothing stored yet.")
            empty.setFont(QFont(UI_FONT, 10))
            empty.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            self._lay.addWidget(empty)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFixedHeight(min(420, 34 * len(rows) + 10))
            scroll.setStyleSheet(
                f"QScrollArea {{ border: 1px solid {C.BORDER}; border-radius: 3px; "
                f"background: transparent; }}"
            )
            inner = QWidget()
            ilay  = QVBoxLayout(inner)
            ilay.setContentsMargins(6, 6, 6, 6)
            ilay.setSpacing(3)

            for r in rows:
                line = QHBoxLayout(); line.setSpacing(6)
                txt = QLabel(f"<b>{r['key'].replace('_', ' ')}</b> "
                             f"<span style='color:{C.TEXT_MED}'>— {r['value']}</span>")
                txt.setWordWrap(True)
                txt.setFont(QFont(UI_FONT, 9))
                txt.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
                line.addWidget(txt, 1)

                meta = QLabel(f"{r['category'][:4]} · {r['updated'] or '—'}")
                meta.setFont(QFont(UI_FONT, 8))
                meta.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
                line.addWidget(meta)

                rm = QPushButton("✕")
                rm.setFixedSize(20, 20)
                rm.setFont(QFont(UI_FONT, 9, QFont.Weight.Bold))
                rm.setCursor(Qt.CursorShape.PointingHandCursor)
                rm.setToolTip("Forget this")
                rm.setStyleSheet(f"""
                    QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px; }}
                    QPushButton:hover {{ color: {C.RED}; border-color: {C.RED}; }}
                """)
                rm.clicked.connect(
                    lambda _=False, c=r["category"], k=r["key"]: self.Forget(c, k))
                line.addWidget(rm)

                holder = QWidget()
                holder.setLayout(line)
                ilay.addWidget(holder)

            ilay.addStretch()
            scroll.setWidget(inner)
            self._lay.addWidget(scroll)

        close = QPushButton("CLOSE")
        close.setFixedHeight(30)
        close.setFont(QFont(UI_FONT, 10))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close.clicked.connect(self.hide)
        self._lay.addWidget(close)

        self.Settle(before)
        # …and again once Qt has polished the new children, because the size
        # hint is not final until then. Harmless when the first pass already
        # got it right: _settle is idempotent.
        QTimer.singleShot(0, lambda g=before: self.Settle(g))

    def Forget(self, category: str, key: str):
        from Brain.Memory import forget
        forget(key, category)
        # Rebuild on the NEXT event-loop turn, not inside this click handler.
        # The rebuild destroys the very ✕ button that emitted this signal, and
        # Qt is entitled to touch the sender after a slot returns; tearing it
        # down mid-emission is how a widget ends up half-alive on screen.
        QTimer.singleShot(0, self.Rebuild)


class _ScrollSafeCombo(QComboBox):
    """A dropdown that does not change when you scroll past it.

    Qt's default is that a combo box under the pointer swallows the wheel and
    steps its own value with it. Inside a scrolling panel that is not a
    convenience, it is a trap: scrolling down the list of settings silently
    changes whichever ones the pointer crossed on the way, and nothing on screen
    says so. It was reported as "selections move around when I scroll", and it
    is the same bug every Qt settings panel has until somebody fixes it.

    The wheel is only accepted when the box has been clicked into. Otherwise the
    event is passed up, which is what makes the panel scroll instead.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        # StrongFocus so it can still be tabbed to and used from the keyboard;
        # the wheel is gated on having focus, not on being focusable.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, e):
        if self.hasFocus():
            super().wheelEvent(e)
        else:
            e.ignore()


class ProvidersOverlay(_HudOverlay):
    """Choose what runs the assistant: Gemini Live, or your own stack.

    WHAT THE FIRST VERSION GOT WRONG, AND WHY IT IS WORTH WRITING DOWN
        It listed the PROVIDERS, and a provider here covers thirteen services at
        once because they all speak the same protocol. That is the right shape
        for the code — it is why `llm_openai.py` is one file instead of thirteen
        — and it is the wrong shape for a person.

        Watched in use: somebody who wanted to run a model on their own machine
        opened the dropdown, saw a single entry reading "OpenAI-compatible", and
        concluded the feature was not there. They were right to. Nobody looking
        for Ollama searches for the name of a protocol, and "OpenAI" is the last
        word you expect to find under "run it locally".

        So the panel now lists what people are actually looking for — Ollama, LM
        Studio, Groq, DeepSeek — from each provider's own `catalog`. One file
        behind the scenes, thirteen recognisable names in front of them.

    THE OTHER TWO CHANGES
        It was 560 pixels of stacked dropdowns with no grouping, which reads as
        a form to be endured rather than three decisions to be made. It is 760
        wide now, and the three parts of a conversation are three numbered boxes
        that say what they do in plain words.

        And every field a provider declares was shown at once — download folders,
        stability weights, thread counts. A setting almost nobody changes costs
        more in the confusion it adds to the five that matter than it saves for
        the one person who wants it, so `advanced` fields sit behind MORE.

    WHAT IT COSTS WHEN NOBODY OPENS IT
        Nothing. The button is built with the rest of the SETUP drawer, but the
        `providers` package is imported inside `__init__` of THIS class — on the
        click. An import on the Qt thread once made the drawer take two seconds
        to open and look broken.
    """

    changed = pyqtSignal()          # emitted on APPLY when the engine moved
    # Emitted from a worker thread when something the machine had to be asked
    # for — the installed voices — has arrived. A signal rather than a direct
    # call because the worker must not touch a widget.
    _live_ready = pyqtSignal()
    # (provider id, [(value, label), ...]) from a worker that went and asked an
    # endpoint what it offers.
    _models_ready = pyqtSignal(str, list)
    _OW = 760                       # the window's own minimum is 820

    # Which provider fills each slot, and what the slot is FOR — in the words
    # somebody configuring it would use, not the words the code uses.
    _SLOTS = (
        ("stt", "①", "HEARING",  "turns what you say into words"),
        ("llm", "②", "THINKING", "understands, decides, uses your tools"),
        ("tts", "③", "SPEAKING", "reads the answer out loud"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        # Imported HERE, on the click — never at startup. See the class docstring.
        from Brain.Settings import GetPipeline, GetProviderConfig, GetProviderMode, GetSidecarFirst, GetSidecarProvider
        from Engines import Registry

        self._registry = Registry
        self._get_cfg = GetProviderConfig
        # Warm anything a provider can only learn by asking the machine, off
        # this thread. The installed voice list costs a PowerShell subprocess —
        # measured at 1.70 seconds — and the form asks for it on every rebuild,
        # so without this the panel freezes for most of two seconds every time
        # a dropdown is touched. Started here rather than at startup, because
        # nothing in this folder may run before somebody opens it.
        self._live_ready.connect(self.OnLiveReady)
        self._models_ready.connect(self.OnModelsReady)
        self._model_cache: dict[str, list] = {}
        self._fetching: set = set()
        for _kind in ("tts", "stt", "llm"):
            for _decl in Registry.discover(_kind):
                try:
                    _mod = __import__(f"Engines.{_decl['_module']}",
                                      fromlist=["prefetch"])
                    _pf = getattr(_mod, "prefetch", None)
                    if _pf:
                        _pf(self._live_ready.emit)
                except Exception:
                    pass
        self._edits: dict[str, dict] = {}       # provider id -> {key: (widget, Field)}
        self._status: dict[str, QLabel] = {}    # provider id -> status line
        self._more: dict[str, bool] = {}        # provider id -> advanced shown
        # Fields deliberately not drawn — the catalog already answered them, or
        # they are behind MORE. Tracked rather than asked of Qt: see _fields_into.
        self._hidden: dict[str, set] = {}
        self._picked: dict[str, str] = {}       # slot -> catalog entry id

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ProvidersOverlay {{
                background: rgba(0, 6, 10, 249);
                border: 1px solid {C.BORDER_B};
                border-radius: 8px;
            }}
        """)
        self.setFixedWidth(self._OW)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(10)

        title = QLabel("BRAIN")
        title.setFont(QFont(UI_FONT, 13, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        outer.addWidget(title)

        sub = QLabel("Choose what powers the conversation. "
                     "Gemini is the default and needs nothing set up here.")
        sub.setWordWrap(True)
        sub.setFont(QFont(UI_FONT, 9))
        sub.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        outer.addWidget(sub)

        # ── Mode: two cards, not a dropdown ─────────────────────────────────
        # A dropdown hides the option you did not choose. This is the one
        # decision on the panel that changes everything below it, and both
        # answers deserve to be readable at the same time.
        self._mode = GetProviderMode()
        cards = QHBoxLayout(); cards.setSpacing(10)
        self._card_gemini = self.ModeCard(
            "☁  GEMINI LIVE",
            "One connection carries your voice, its voice, the transcript and "
            "the tools together. The fastest option — about half a second to "
            "the first word.",
            "gemini")
        self._card_local = self.ModeCard(
            "🖥  MY OWN SETUP",
            "Your choice of hearing, thinking and speaking. Can run entirely on "
            "this computer, with nothing leaving it. Slower: 1.5 to 4 seconds.",
            "pipeline")
        cards.addWidget(self._card_gemini)
        cards.addWidget(self._card_local)
        outer.addLayout(cards)

        # ── Body ────────────────────────────────────────────────────────────
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; }"
            f"QScrollBar:vertical {{ background: {C.DARK}; width: 9px; "
            f"margin: 0; border-radius: 4px; }}"
            f"QScrollBar::handle:vertical {{ background: {C.BORDER_B}; "
            f"border-radius: 4px; min-height: 30px; }}"
            "QScrollBar::add-line, QScrollBar::sub-line { height: 0; }"
            "QScrollBar::add-page, QScrollBar::sub-page { background: none; }")
        self._scroll.setMinimumHeight(230)
        self._scroll.setMaximumHeight(360)
        outer.addWidget(self._scroll, 1)

        self._body = QWidget()
        self._body.setStyleSheet("background: transparent;")
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(0, 2, 8, 2)
        self._body_lay.setSpacing(10)
        self._scroll.setWidget(self._body)

        # Built BEFORE the body, because `_rebuild_body` writes into it — and
        # added to the layout after, so it still sits under the scroll area.
        self._footer = QLabel("")
        self._footer.setWordWrap(True)
        self._footer.setFont(QFont(UI_FONT, 8))
        self._footer.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")

        self._pipe = dict(GetPipeline())
        self._sidecar = GetSidecarProvider()
        self._sidecar_first = GetSidecarFirst()
        self.RebuildBody()

        outer.addWidget(self._footer)

        row = QHBoxLayout(); row.setSpacing(8)
        apply_btn = QPushButton("▸  SAVE  &  APPLY")
        apply_btn.setFixedHeight(34)
        apply_btn.setFont(QFont(UI_FONT, 10, QFont.Weight.Bold))
        apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_btn.setStyleSheet(f"""
            QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 4px; }}
            QPushButton:hover {{ background: {C.BORDER}; border-color: {C.PRI}; }}
        """)
        apply_btn.clicked.connect(self._Apply)
        row.addWidget(apply_btn, 2)

        close = QPushButton("CLOSE")
        close.setFixedHeight(34)
        close.setFont(QFont(UI_FONT, 10))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 4px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close.clicked.connect(self.hide)
        row.addWidget(close, 1)
        outer.addLayout(row)
        self.RefreshCards()

    # ── pieces ───────────────────────────────────────────────────────────────
    def ModeCard(self, title: str, body: str, value: str) -> QPushButton:
        b = QPushButton()
        b.setCheckable(True)
        b.setMinimumHeight(84)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setText(f"{title}\n\n{body}")
        b.setFont(QFont(UI_FONT, 8))
        b.clicked.connect(lambda _=0, v=value: self.SetMode(v))
        b.setProperty("modeValue", value)
        return b

    def RefreshCards(self) -> None:
        for card in (self._card_gemini, self._card_local):
            on = card.property("modeValue") == self._mode
            card.setChecked(on)
            card.setStyleSheet(f"""
                QPushButton {{
                    text-align: left; padding: 10px 12px;
                    background: {C.PRI_GHO if on else 'transparent'};
                    color: {C.TEXT if on else C.TEXT_DIM};
                    border: 1px solid {C.PRI_DIM if on else C.BORDER};
                    border-radius: 5px;
                }}
                QPushButton:hover {{ border-color: {C.BORDER_B}; }}
            """)

    def OnLiveReady(self) -> None:
        """A machine-supplied list has arrived — redraw once so it appears."""
        if not self.isHidden():
            self.RebuildBody()

    # ── Asking an endpoint what it has, without being asked to ──────────────
    def FetchModels(self, pid: str) -> None:
        """Go and get the model list in the background.

        Pressing LIST worked, and making somebody press it was still wrong: at
        the moment a local server is chosen, the one thing they need is the list
        of models on it, and the app can simply go and read it. A button whose
        only job is "now do the obvious next thing" is a button that should not
        exist.

        Off the interface thread, because it is a network call — a local server
        answers in milliseconds and a cloud one does not, and a panel that
        freezes while it finds out is worse than one that fills in a second
        later. Failure is silent: not reaching the endpoint yet is the normal
        state while somebody is still typing an address, and TEST is where a
        real answer about reachability belongs."""
        import threading

        if not pid or pid in self._fetching:
            return
        try:
            values = self.Values(pid) or dict(self._get_cfg(pid))
        except Exception:
            values = dict(self._get_cfg(pid))
        if not values.get("base_url") and not values.get("api_key"):
            return
        self._fetching.add(pid)

        def work():
            items = []
            try:
                inst = self._registry.build(pid, values)
                if hasattr(inst, "models"):
                    items = [(m, m) for m in inst.models(timeout=6.0)]
                elif hasattr(inst, "voices"):
                    items = list(inst.voices())
            except Exception:
                items = []
            finally:
                self._fetching.discard(pid)
            if items:
                self._models_ready.emit(pid, items)

        threading.Thread(target=work, daemon=True).start()

    def _prepare(self, pid: str) -> None:
        """Let a provider get itself ready as soon as it is configured.

        Piper uses it to download the chosen voice. Generic on purpose: the
        panel asks whether the module has a `prepare`, and providers that do not
        need one are simply skipped — nothing here knows what a voice is."""
        import threading

        try:
            decl = self._registry.GetDeclaration(pid)
            mod = __import__(f"Engines.{decl['_module']}", fromlist=["prepare"])
            fn = getattr(mod, "prepare", None)
        except Exception:
            fn = None
        if fn is None:
            return
        values = self.Values(pid) or dict(self._get_cfg(pid))

        def Report(ok, msg):
            colour = C.TEXT_DIM if ok is None else (C.GREEN if ok else C.RED)
            mark = "" if ok is None else ("✓  " if ok else "✕  ")
            QTimer.singleShot(0, lambda: self.Say(pid, mark + msg, colour))

        threading.Thread(target=lambda: fn(values, Report), daemon=True).start()

    def OnModelsReady(self, pid: str, items: list) -> None:
        self._model_cache[pid] = list(items)
        if self.isHidden():
            return
        key = "voice_id" if any(v != l for v, l in items) else "model"
        entry = self._edits.get(pid, {}).get(key)
        if entry is None:
            entry = self._edits.get(pid, {}).get("model")
        if entry is None or not isinstance(entry[0], QComboBox):
            return
        box = entry[0]
        keep = box.currentText()
        box.clear()
        for value, label in items:
            box.addItem(str(label), str(value))
        j = box.findData(keep)
        if j >= 0:
            box.setCurrentIndex(j)
        elif keep:
            box.setCurrentText(keep)
        self.Say(pid, f"{len(items)} model{'s' if len(items) != 1 else ''} "
                       f"found on this server — pick one above.", C.TEXT_DIM)

    def SetMode(self, value: str) -> None:
        if value == self._mode:
            self.RefreshCards()
            return
        self._mode = value
        self.RememberEdits()
        self.RefreshCards()
        QTimer.singleShot(0, self.RebuildBody)

    def _Box(self, title_widgets: list) -> tuple[QWidget, QVBoxLayout]:
        """A bordered section. Grouping is the difference between three
        decisions and eleven controls."""
        w = QWidget()
        w.setStyleSheet(f"background: {C.PANEL}; border: 1px solid {C.BORDER}; "
                        f"border-radius: 5px;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 10, 12, 11)
        lay.setSpacing(5)
        for tw in title_widgets:
            lay.addWidget(tw)
        return w, lay

    def _label(self, text: str, size: int = 8, colour: str = None,
               bold: bool = False) -> QLabel:
        lb = QLabel(text)
        lb.setWordWrap(True)
        lb.setFont(QFont(UI_FONT, size,
                         QFont.Weight.Bold if bold else QFont.Weight.Normal))
        lb.setStyleSheet(f"color: {colour or C.TEXT_MED}; background: transparent; "
                         f"border: none;")
        return lb

    def Combo(self) -> QComboBox:
        box = _ScrollSafeCombo()
        box.setFont(QFont(UI_FONT, 9))
        box.setFixedHeight(29)
        box.setStyleSheet(
            f"QComboBox {{ background: {C.DARK}; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 3px 9px; }}"
            f"QComboBox:hover {{ border-color: {C.BORDER_B}; }}"
            f"QComboBox::drop-down {{ border: none; width: 18px; }}"
            f"QComboBox QAbstractItemView {{ background: {C.DARK}; color: {C.TEXT}; "
            f"selection-background-color: {C.PRI_GHO}; "
            f"border: 1px solid {C.BORDER_B}; padding: 2px; }}")
        return box

    def _Line(self, text: str = "") -> QLineEdit:
        w = QLineEdit(text)
        w.setFont(QFont(UI_FONT, 9))
        w.setFixedHeight(27)
        w.setStyleSheet(
            f"QLineEdit {{ background: {C.DARK}; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 9px; }}"
            f"QLineEdit:focus {{ border-color: {C.PRI_DIM}; }}")
        return w

    def Btn(self, text: str, slot, colour: str = None) -> QPushButton:
        b = QPushButton(text)
        b.setFixedHeight(25)
        b.setFont(QFont(UI_FONT, 8, QFont.Weight.Bold))
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        col = colour or C.TEXT_MED
        b.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {col};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 0 12px; }}
            QPushButton:hover {{ border-color: {C.BORDER_B}; color: {C.TEXT}; }}
        """)
        b.clicked.connect(slot)
        return b

    # ── catalog ──────────────────────────────────────────────────────────────
    def _catalog(self, kind: str) -> list[tuple[str, str, dict, dict]]:
        """(provider id, entry id, entry, declaration) for one slot.

        A provider with no catalog contributes itself as a single entry, so
        nothing has to declare one to appear."""
        out = []
        for decl in self._registry.discover(kind):
            entries = decl.get("catalog") or (
                {"id": decl["id"], "label": decl["label"],
                 "note": decl.get("hint", ""), "local": None, "values": {}},)
            for e in entries:
                out.append((decl["id"], e["id"], e, decl))
        return out

    def CurrentEntry(self, kind: str, pid: str) -> str:
        """Which catalog entry is selected.

        STORED, not derived. The first version worked it out by comparing the
        saved address against each entry's — which is elegant and wrong, and it
        showed up immediately: pick "Whisper on this computer", reopen the
        panel, and the box reads "— not set —" while the form beneath it is
        fully drawn. That entry's address is the empty string, so any config
        whose address had ever been touched matched nothing and fell through to
        no selection at all.

        The entry is a CHOICE somebody made, not a fact about the settings.
        Choices get stored. The derivation is kept only as a fallback for a
        config written before this key existed."""
        if not pid:
            return ""
        cfg = self._get_cfg(pid)
        saved = str(cfg.get("catalog") or "").strip()
        entries = [(eid, entry) for _p, eid, entry, _d in self._catalog(kind)
                   if _p == pid]
        if saved and any(eid == saved for eid, _e in entries):
            return saved

        # Written before the key existed: work it out once, from the address.
        best = ""
        for eid, entry in entries:
            vals = entry.get("values") or {}
            if not vals:
                best = best or eid            # a catch-all like "Something else"
                continue
            if all(str(cfg.get(k, "")).rstrip("/") == str(v).rstrip("/")
                   for k, v in vals.items() if k in ("base_url", "backend")):
                return eid
        return best or (entries[0][0] if len(entries) == 1 else "")

    # ── body ─────────────────────────────────────────────────────────────────
    def _Clear(self, lay) -> None:
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            elif item.layout() is not None:
                self._Clear(item.layout())

    def RebuildBody(self) -> None:
        # Keep whatever is in the boxes before they are destroyed.
        #
        # A redraw can be triggered by things the person did not do: a model
        # list arriving from a background fetch, a slot changing, the panel
        # settling after a click. Every one of those replaces the widgets, and
        # anything typed but not yet saved went with them — most visibly a model
        # name entered by hand, which came back empty on SAVE with no
        # indication that it had been dropped.
        self.RememberEdits()
        self._Clear(self._body_lay)
        self._edits.clear()
        self._status.clear()

        if self._mode == "pipeline":
            for slot, num, name, what in self._SLOTS:
                self._body_lay.addWidget(self.SlotBox(slot, num, name, what))
        else:
            w, lay = self._Box([])
            lay.addWidget(self._label(
                "Nothing to set up. Your voice goes to Gemini and comes back as "
                "speech over one connection, with the transcript and every tool "
                "call on the same wire.", 8, C.TEXT_DIM))
            lay.addWidget(self._label(
                "Pick MY OWN SETUP above to run it on your own machine, or "
                "through another company's model.", 7, C.TEXT_DIM))
            self._body_lay.addWidget(w)

        self._body_lay.addWidget(self.SidecarBoxWidget())
        self._body_lay.addStretch(1)
        self.UpdateFooter()

    def SlotBox(self, slot: str, num: str, name: str, what: str) -> QWidget:
        head = self._label(f"{num}  {name}", 9, C.PRI, bold=True)
        w, lay = self._Box([head, self._label(what, 7, C.TEXT_DIM)])

        entries = self._catalog(slot)
        box = self.Combo()
        box.addItem("— not set —", ("", ""))
        for pid, eid, entry, decl in entries:
            label = entry["label"]
            if entry.get("local") is True:
                label = f"🖥  {label}"
            elif entry.get("local") is False:
                label = f"☁  {label}"
            else:
                label = f"    {label}"
            note = entry.get("note") or ""
            if not decl.get("_available", True):
                note = (note + " · needs install").strip(" ·")
            box.addItem(f"{label}{'   —  ' + note if note else ''}", (pid, eid))

        pid_now = self._pipe.get(slot, "")
        eid_now = self._picked.get(slot) or self.CurrentEntry(slot, pid_now)
        idx = box.findData((pid_now, eid_now)) if pid_now else 0
        if pid_now and idx < 0:
            # A provider IS chosen — the form for it is drawn right below — so
            # the box must never read "not set". It did, and it was the first
            # thing anybody noticed: an entry whose settings no longer match
            # any catalog row (upgraded from an older layout, or edited by
            # hand) resolved to nothing at all, and the panel contradicted
            # itself on screen. Falling back to that provider's first row is
            # always closer to the truth than claiming nothing is selected.
            for i in range(box.count()):
                data = box.itemData(i)
                if data and data[0] == pid_now:
                    idx = i
                    self._picked[slot] = data[1]
                    break
        box.setCurrentIndex(idx if idx >= 0 else 0)
        box.currentIndexChanged.connect(
            lambda _i, s=slot, b=box: self.SlotChanged(s, b))
        if pid_now:
            QTimer.singleShot(0, lambda p=pid_now: self._prepare(p))
        lay.addWidget(box)

        if pid_now:
            self.FieldsInto(lay, slot, pid_now, eid_now)
            # If a list was already fetched, use it; if not, go and get one.
            if self._model_cache.get(pid_now):
                QTimer.singleShot(0, lambda p=pid_now: self.OnModelsReady(
                    p, self._model_cache[p]))
            else:
                QTimer.singleShot(0, lambda p=pid_now: self.FetchModels(p))
        return w

    def SlotChanged(self, slot: str, box: QComboBox) -> None:
        pid, eid = box.currentData() or ("", "")
        self.RememberEdits()
        self._pipe[slot] = pid
        self._picked[slot] = eid
        if pid and eid:
            # Applying the shortcut is the entire point of the catalog: pick
            # Ollama and the address is already right. The choice itself is
            # saved alongside it — see _current_entry for why.
            from Brain.Settings import SaveProviderConfig
            for _p, _e, entry, _d in self._catalog(slot):
                if _p == pid and _e == eid:
                    values = dict(entry.get("values") or {})
                    values["catalog"] = eid
                    SaveProviderConfig(pid, values)
                    break
            self.FetchModels(pid)
        QTimer.singleShot(0, self.RebuildBody)

    def FieldsInto(self, lay, slot: str, pid: str, eid: str) -> None:
        from Engines.Base import Field

        decl = self._registry.GetDeclaration(pid)
        if decl is None:
            return
        stored = dict(self._get_cfg(pid))
        entry = next((e for p, i, e, _ in self._catalog(slot)
                      if p == pid and i == eid), {})
        is_local = entry.get("local") is True
        entry_values = entry.get("values") or {}
        # An entry that MENTIONS the address has answered it — including
        # answering it with "", which is what the local speech recogniser does
        # because it does not use one at all. Testing the value rather than the
        # presence of the key is what put an empty "API address" box under
        # "Whisper on this computer", inviting somebody to fill in a field that
        # backend never reads.
        custom = "base_url" not in entry_values

        # Three buckets, tracked explicitly.
        #
        # `isHidden()` is NOT a usable test for "I hid this on purpose": a widget
        # that has not been added to a layout yet is already hidden, so asking it
        # returned True for every field and the form came up completely empty.
        # Which of these a field lands in is a decision made here, so it is kept
        # here rather than inferred from Qt afterwards.
        shown: dict = {}        # drawn now
        carried: dict = {}      # kept for saving, never drawn — answered by the catalog
        advanced: list = []     # drawn only behind MORE

        for f in decl.get("needs", ()):
            if not isinstance(f, Field):
                continue
            # Do not show what the catalog entry already answered. The address
            # somebody did not have to type is one more thing to read and one
            # more thing to break; a key a local server never asks for is a box
            # that makes people think they need an account.
            if f.key == "base_url" and not custom:
                carried[f.key] = (self._Line(
                    str(stored.get(f.key, f.default) or "")), f)
                continue
            if f.key == "api_key" and is_local:
                carried[f.key] = (self._Line(str(stored.get(f.key, "") or "")), f)
                continue
            if f.key == "backend":
                carried[f.key] = (self._Line(
                    str(entry.get("values", {}).get("backend")
                        or stored.get(f.key, f.default) or "")), f)
                continue
            w = self.MakeWidget(pid, f, stored.get(f.key, f.default))
            if f.advanced:
                advanced.append((f, w))
            else:
                shown[f.key] = (w, f)

        for _key, (w, f) in shown.items():
            lay.addWidget(self._label(
                f.label + ("" if f.required else "   (optional)"), 7, C.TEXT_DIM))
            lay.addWidget(w)
            # A text box already shows its hint as placeholder text. Adding the
            # label as well printed every hint twice, which is how the panel
            # came to look careless.
            if f.hint and not isinstance(w, QLineEdit):
                lay.addWidget(self._label(f.hint, 7, C.TEXT_DIM))

        fields = dict(shown)
        fields.update(carried)
        self._hidden[pid] = set(carried) | {f.key for f, _w in advanced}

        # Buttons and status.
        status = self._label("", 7, C.TEXT_DIM)
        self._status[pid] = status

        brow = QHBoxLayout(); brow.setSpacing(6)
        brow.addWidget(self.Btn("TEST", lambda _=0, p=pid: self._test(p), C.PRI))
        # Not "LIST" any more: the list arrives on its own when a service is
        # chosen. This is the button for when it did not — the server was
        # starting up, or a model has just been pulled.
        if any(k in shown for k in ("model", "voice_id", "voice")):
            brow.addWidget(self.Btn("↻ REFRESH", lambda _=0, p=pid: self._List(p)))
        if advanced:
            brow.addWidget(self.Btn(
                ("⌃ LESS" if self._more.get(pid) else "⌄ MORE"),
                lambda _=0, p=pid: self.ToggleMore(p)))
        if not decl.get("_available", True) and decl.get("install"):
            brow.addWidget(self.Btn(f"INSTALL {decl['install']}",
                                     lambda _=0, p=pid: self.Install(p), C.ACC2))
        brow.addStretch(1)
        holder = QWidget()
        holder.setStyleSheet("background: transparent; border: none;")
        holder.setLayout(brow)
        lay.addWidget(holder)
        lay.addWidget(status)

        if self._more.get(pid):
            lay.addWidget(self._label("─" * 46, 7, C.BORDER))
            for f, w in advanced:
                lay.addWidget(self._label(f"{f.label}   (optional)", 7, C.TEXT_DIM))
                lay.addWidget(w)
                if f.hint and not isinstance(w, QLineEdit):
                    lay.addWidget(self._label(f.hint, 7, C.TEXT_DIM))
                fields[f.key] = (w, f)
                self._hidden[pid].discard(f.key)
        else:
            for f, w in advanced:
                fields[f.key] = (w, f)

        self._edits[pid] = fields

    def MakeWidget(self, pid: str, f, value):
        if f.kind == "choice":
            w = self.Combo()
            choices = list(self.LiveChoices(pid, f.key) or f.choices)
            if not f.required:
                w.addItem("— default —", "")
            for c in choices:
                w.addItem(str(c), str(c))
            w.setEditable(f.key in ("base_url", "model", "voice_id"))
            if w.isEditable():
                w.lineEdit().setStyleSheet(f"color: {C.TEXT}; background: transparent;")
            if value:
                j = w.findData(str(value))
                if j < 0:
                    w.addItem(str(value), str(value))
                    j = w.count() - 1
                w.setCurrentIndex(j)
                if w.isEditable():
                    w.setCurrentText(str(value))
            return w
        if f.kind == "bool":
            w = self.Combo()
            w.addItem("no", "")
            w.addItem("yes", "1")
            w.setCurrentIndex(1 if str(value) in ("1", "True", "true") else 0)
            return w
        w = self._Line(str(value if value is not None else ""))
        if f.kind == "secret":
            w.setEchoMode(QLineEdit.EchoMode.Password)
        if f.hint:
            w.setPlaceholderText(f.hint[:64])
        return w

    def ToggleMore(self, pid: str) -> None:
        self._more[pid] = not self._more.get(pid)
        self.RememberEdits()
        QTimer.singleShot(0, self.RebuildBody)

    def SidecarBoxWidget(self) -> QWidget:
        head = self._label("⚡  BACKGROUND REQUESTS", 9, C.PRI, bold=True)
        w, lay = self._Box([head])
        lay.addWidget(self._label(
            "Reading a document, looking at your screen, writing code — the "
            "assistant's own requests, not the conversation. These use Gemini's "
            "text quota, which runs out before the voice does. Sending them "
            "somewhere else keeps them working, and changes nothing you hear.",
            7, C.TEXT_DIM))

        # Read the stored choice BEFORE the box exists. The first version asked
        # `_sidecar_choice()`, which reads the combo — and at this point that is
        # the empty box being built, so the answer was always "nothing chosen"
        # and the form under it never appeared.
        cur_pid = self._sidecar
        cur_eid = self.CurrentEntry("llm", cur_pid) if cur_pid else ""

        self._sidecar_combo = self.Combo()
        self._sidecar_combo.addItem("Gemini only  —  the way it has always been",
                                    ("", ""))
        for pid, eid, entry, decl in self._catalog("llm"):
            mark = "🖥" if entry.get("local") else ("☁" if entry.get("local")
                                                   is False else "  ")
            self._sidecar_combo.addItem(
                f"{mark}  Fall back to {entry['label']}", (pid, eid))
        i = self._sidecar_combo.findData((cur_pid, cur_eid)) if cur_pid else 0
        if cur_pid and i < 0:
            for j in range(1, self._sidecar_combo.count()):
                if (self._sidecar_combo.itemData(j) or ("", ""))[0] == cur_pid:
                    i = j
                    break
        self._sidecar_combo.setCurrentIndex(max(0, i))
        self._sidecar_combo.currentIndexChanged.connect(self.SidecarChanged)
        lay.addWidget(self._sidecar_combo)

        self._first_combo = self.Combo()
        self._first_combo.addItem("Try Gemini first  —  faster", "after")
        self._first_combo.addItem(
            "Try the other one first  —  keeps more on your machine", "before")
        self._first_combo.setCurrentIndex(1 if self._sidecar_first else 0)
        lay.addWidget(self._first_combo)

        # In pipeline mode the model chosen for ② THINKING answers these too, so
        # there is nothing more to fill in. In Gemini mode there IS no ② section
        # — and the first version still said "it uses whatever you set up under
        # ② THINKING", which named a box that was not on the screen. So a
        # service picked here gets its own form, right here.
        if cur_pid:
            if self._mode == "pipeline" and cur_pid == self._pipe.get("llm"):
                lay.addWidget(self._label(
                    "Uses the model you chose for ② THINKING.", 7, C.TEXT_DIM))
            else:
                self.FieldsInto(lay, "llm", cur_pid, cur_eid)
        else:
            lay.addWidget(self._label(
                "Left as it is, these keep using Gemini and stop working when "
                "its text quota runs out.", 7, C.TEXT_DIM))
        return w

    def SidecarChoice(self) -> tuple:
        """(provider id, catalog entry id) for the background-request model."""
        box = getattr(self, "_sidecar_combo", None)
        if box is not None:
            data = box.currentData()
            if isinstance(data, tuple):
                return data
        pid = self._sidecar
        return (pid, self.CurrentEntry("llm", pid) if pid else "")

    def SidecarChanged(self, _ix) -> None:
        pid, eid = self.SidecarChoice()
        self.RememberEdits()
        self._sidecar = pid
        if pid and eid:
            from Brain.Settings import SaveProviderConfig
            for _p, _e, entry, _d in self._catalog("llm"):
                if _p == pid and _e == eid:
                    values = dict(entry.get("values") or {})
                    values["catalog"] = eid
                    SaveProviderConfig(pid, values)
                    break
        QTimer.singleShot(0, self.RebuildBody)

    def UpdateFooter(self) -> None:
        if self._mode != "pipeline":
            self._footer.setText(
                "Gemini Live. Nothing on this panel changes the conversation.")
            return
        missing = [name for slot, _n, name, _w in self._SLOTS
                   if not self._pipe.get(slot)]
        if "THINKING" in missing:
            self._footer.setText(
                "⚠  ② THINKING is not set — without it the assistant falls back "
                "to Gemini Live and says so in the log.")
        elif missing:
            words = " and ".join(f"{m.lower()}" for m in missing)
            self._footer.setText(
                f"Only {words} is missing — it will run, but you will type "
                f"instead of speak, or read instead of listen.")
        else:
            self._footer.setText(
                "All three set. SAVE & APPLY reconnects; the conversation is kept.")

    # ── actions ──────────────────────────────────────────────────────────────
    def LiveChoices(self, pid: str, key: str) -> tuple:
        try:
            decl = self._registry.GetDeclaration(pid)
            mod = __import__(f"Engines.{decl['_module']}", fromlist=["choices_for"])
            fn = getattr(mod, "choices_for", None)
            return tuple(fn(key)) if fn else ()
        except Exception:
            return ()

    def Values(self, pid: str) -> dict:
        out = {}
        hidden = self._hidden.get(pid, set())
        stored = self._get_cfg(pid)
        for key, (w, _f) in self._edits.get(pid, {}).items():
            if isinstance(w, QComboBox):
                value = (w.currentText().strip() if w.isEditable()
                         else (w.currentData() or ""))
            else:
                value = w.text().strip()
            # A field nobody can see must never blank out a setting that has
            # one. These widgets are rebuilt on every redraw, and a redraw that
            # raced a background fetch could recreate a hidden address as empty
            # and then save that over a working one — leaving a pipeline that
            # had been configured correctly pointing at nothing, with no visible
            # cause. Clearing a value is still possible; it just has to be done
            # in a box that is on screen.
            if not value and key in hidden and stored.get(key):
                continue
            out[key] = value
        return out

    def RememberEdits(self) -> None:
        """Keep what has been typed when the form is rebuilt.

        Without it, changing the speech engine wiped an API key somebody had
        just entered into the box above."""
        from Brain.Settings import SaveProviderConfig
        for pid in list(self._edits):
            try:
                SaveProviderConfig(pid, self.Values(pid))
            except Exception:
                pass

    def Say(self, pid: str, text: str, colour: str) -> None:
        lb = self._status.get(pid)
        if lb is not None:
            lb.setText(text)
            lb.setStyleSheet(f"color: {colour}; background: transparent; border: none;")

    def _test(self, pid: str) -> None:
        # Read the boxes BEFORE letting Qt run. `processEvents` will happily
        # deliver a queued rebuild, which deletes the very widgets this is
        # about to read — and a deleted QWidget raises rather than returning
        # something wrong, so it takes the click down with it.
        values = self.Values(pid)
        self.Say(pid, "checking…", C.TEXT_DIM)
        QApplication.processEvents()
        try:
            inst = self._registry.build(pid, values)
            probe = getattr(inst, "probe", None)
            if probe is None:
                self.Say(pid, "Nothing to check for this one.", C.TEXT_DIM)
                return
            ok, msg = probe()
        except Exception as e:
            self.Say(pid, f"✕  {type(e).__name__}: {str(e)[:150]}", C.RED)
            return
        self.Say(pid, ("✓  " if ok else "✕  ") + msg, C.GREEN if ok else C.ACC)
        # TEST is also where "can this model see?" gets answered — by asking the
        # endpoint, not the user. Write the answer back into the stored settings
        # so the assistant is told the truth about its own eyes.
        seen = (getattr(inst, "caps", {}) or {}).get("vision")
        if ok and seen is not None:
            from Brain.Settings import SaveProviderConfig
            SaveProviderConfig(pid, {"vision": "1" if seen else ""})
            entry = self._edits.get(pid, {}).get("vision")
            if entry is not None and isinstance(entry[0], QComboBox):
                entry[0].setCurrentIndex(1 if seen else 0)

    def _List(self, pid: str) -> None:
        values = self.Values(pid)          # before processEvents — see _test
        self.Say(pid, "reading…", C.TEXT_DIM)
        QApplication.processEvents()
        try:
            inst = self._registry.build(pid, values)
            if hasattr(inst, "models"):
                items, key = [(m, m) for m in inst.models()], "model"
            elif hasattr(inst, "voices"):
                items, key = list(inst.voices()), "voice_id"
            else:
                self.Say(pid, "Nothing to list for this one.", C.TEXT_DIM)
                return
        except Exception as e:
            self.Say(pid, f"✕  {type(e).__name__}: {str(e)[:150]}", C.RED)
            return

        entry = self._edits.get(pid, {}).get(key)
        if entry is None or not items:
            self.Say(pid, f"✓  {len(items)} found." if items else "None found.",
                      C.GREEN if items else C.TEXT_DIM)
            return
        box = entry[0]
        if isinstance(box, QComboBox):
            keep = box.currentText()
            box.clear()
            for value, label in items:
                box.addItem(str(label), str(value))
            j = box.findData(keep)
            box.setCurrentIndex(j if j >= 0 else 0)
        else:
            self.Say(pid, f"✓  {len(items)} available: "
                           f"{', '.join(str(v) for v, _ in items[:8])}", C.GREEN)
            return
        self.Say(pid, f"✓  {len(items)} available — pick one above.", C.GREEN)

    def Install(self, pid: str) -> None:
        """Fetch a provider's optional dependency, off the Qt thread — pip takes
        tens of seconds and a frozen window looks like a crashed one."""
        import subprocess
        import sys as _sys
        import threading

        decl = self._registry.GetDeclaration(pid) or {}
        pkg = decl.get("install")
        if not pkg:
            return
        self.Say(pid, f"installing {pkg} — this can take a minute…", C.ACC2)

        def work():
            try:
                r = subprocess.run(
                    [_sys.executable, "-m", "pip", "install", pkg],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900)
                good = r.returncode == 0
                tail = r.stdout.decode("utf-8", "replace").strip().splitlines()
                msg = ("✓  installed — close and reopen this panel"
                       if good else (tail[-1][:150] if tail else "pip failed"))
            except Exception as e:
                good, msg = False, f"{type(e).__name__}: {str(e)[:120]}"
            QTimer.singleShot(0, lambda: self.Say(pid, msg,
                                                   C.GREEN if good else C.RED))

        threading.Thread(target=work, daemon=True).start()

    def _Apply(self) -> None:
        from Brain.Settings import GetPipeline, GetProviderMode, SaveProviders

        before = (GetProviderMode(), dict(GetPipeline()))

        # ONE write, not five. See save_providers for what the five cost.
        creds = {pid: self.Values(pid) for pid in list(self._edits)}
        for slot, eid in self._picked.items():
            pid = self._pipe.get(slot, "")
            if pid and eid:
                creds.setdefault(pid, {})["catalog"] = eid
        sidecar_pid, sidecar_eid = self.SidecarChoice()
        if sidecar_pid and sidecar_eid:
            creds.setdefault(sidecar_pid, {}).setdefault("catalog", sidecar_eid)

        SaveProviders(
            mode=self._mode,
            pipeline={"stt": self._pipe.get("stt", ""),
                      "llm": self._pipe.get("llm", ""),
                      "tts": self._pipe.get("tts", "")},
            creds=creds,
            sidecar=sidecar_pid,
            sidecar_first=self._first_combo.currentData() == "before",
        )

        # Providers are cached by configuration; a changed setting must not go
        # on being served by an object built from the old one.
        try:
            self._registry.reset()
        except Exception:
            pass

        after = (GetProviderMode(), dict(GetPipeline()))
        self.hide()
        # Background-request settings take effect on the next call with no
        # reconnect. Only the conversation engine needs the session rebuilt.
        if before != after:
            self.changed.emit()


class ClipboardPanel(QWidget):
    """Floating panel shown when text is copied — offers quick Ultron actions."""

    action_requested = pyqtSignal(str)
    _W, _H = 326, 112

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ClipboardPanel {{
                background: rgba(0, 8, 14, 248);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)
        self._clip_text = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 7)
        lay.setSpacing(4)

        hdr = QHBoxLayout(); hdr.setSpacing(4)
        icon_lbl = QLabel("◈  CLIPBOARD DETECTED")
        icon_lbl.setFont(QFont(UI_FONT, 8, QFont.Weight.Bold))
        icon_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        hdr.addWidget(icon_lbl); hdr.addStretch()
        x_btn = QPushButton("✕")
        x_btn.setFixedSize(16, 16)
        x_btn.setFont(QFont(UI_FONT, 9))
        x_btn.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        x_btn.clicked.connect(self.hide)
        hdr.addWidget(x_btn)
        lay.addLayout(hdr)

        self._preview = QLabel()
        self._preview.setFont(QFont(UI_FONT, 9))
        self._preview.setStyleSheet(f"""
            color: {C.TEXT}; background: {C.PANEL2};
            border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 6px;
        """)
        self._preview.setWordWrap(False)
        self._preview.setFixedHeight(28)
        lay.addWidget(self._preview)

        btn_row = QHBoxLayout(); btn_row.setSpacing(4)
        _bs = (f"QPushButton {{ background: {C.PANEL2}; color: {C.TEXT_MED}; "
               f"border: 1px solid {C.BORDER}; border-radius: 2px; }}"
               f"QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}")
        for label, cmd_fmt in [
            ("TRANSLATE", "Translate this text to English: {text}"),
            ("SUMMARISE", "Summarise this: {text}"),
            ("EXPLAIN",   "Explain this: {text}"),
            ("FIX",       "Fix grammar and spelling: {text}"),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(22)
            b.setFont(QFont(UI_FONT, 8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(_bs)
            b.clicked.connect(lambda _, c=cmd_fmt: self._Trigger(c))
            btn_row.addWidget(b)
        lay.addLayout(btn_row)

        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self.hide)
        self.hide()

    def _Trigger(self, cmd_fmt: str):
        if self._clip_text:
            self.action_requested.emit(cmd_fmt.format(text=self._clip_text[:800]))
        self.hide()

    def ShowClipboard(self, text: str):
        self._clip_text = text
        preview = text[:58].replace('\n', ' ')
        if len(text) > 58:
            preview += "…"
        self._preview.setText(f'"{preview}"')
        self.show(); self.raise_()
        self._dismiss_timer.start(8000)


class PluginSettingsOverlay(QWidget):
    """Floating overlay — renders per-plugin settings forms.

    Fully generic: it iterates the settings schemas a plugin declared via its
    PLUGIN_SETTINGS constant (delivered by PluginRegistry.settings_schemas) and
    builds a form for each. It knows NOTHING about any specific plugin, so the
    core stays clean and plugins remain pure drop-in — install a plugin that
    declares fields (e.g. the 3D-printer suite) and its section appears here;
    install none and this panel simply says there's nothing to configure.
    """

    _test_done = pyqtSignal(str, bool, str)   # namespace, ok, message
    _OW = 460

    def __init__(self, sections: list[dict], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            PluginSettingsOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self._sections = sections or []
        self._widgets: dict[tuple, object] = {}    # (namespace, key) -> input widget
        self._types:   dict[tuple, str]    = {}     # (namespace, key) -> field type
        self._status_labels: dict[str, QLabel] = {} # namespace -> status QLabel
        self._test_done.connect(self.OnTestDone)

        self._fs = (f"QLineEdit {{ background: #000d12; color: {C.TEXT}; "
                    f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
                    f"QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 16)
        root.setSpacing(8)

        root.addWidget(self.Lbl("⚙  PLUGIN SETTINGS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        root.addWidget(sep)

        if not self._sections:
            root.addWidget(self.Lbl(
                "No configurable plugins are installed.\nDrop a plugin that needs "
                "settings (like the 3D-printer suite) into the plugins folder and "
                "it will show up here.", 9, color=C.TEXT_DIM))
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setStyleSheet("QScrollArea { background: transparent; }")
            inner = QWidget()
            inner.setStyleSheet("background: transparent;")
            form = QVBoxLayout(inner)
            form.setContentsMargins(0, 0, 6, 0)
            form.setSpacing(6)
            for sec in self._sections:
                self.BuildSection(form, sec)
            form.addStretch(1)
            scroll.setWidget(inner)
            root.addWidget(scroll, 1)

        # ── bottom buttons ───────────────────────────────────────────────────
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        if self._sections:
            save_btn = QPushButton("▸  SAVE")
            save_btn.setFixedHeight(34)
            save_btn.setFont(QFont(UI_FONT, 10, QFont.Weight.Bold))
            save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            save_btn.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {C.PRI};
                    border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
            """)
            save_btn.clicked.connect(self._SaveAll)
            btn_row.addWidget(save_btn)

        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(34)
        close_btn.setFont(QFont(UI_FONT, 10))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self.hide)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    # ── helpers ───────────────────────────────────────────────────────────────
    def Lbl(self, txt, fs=9, bold=False, color=C.PRI,
             align=Qt.AlignmentFlag.AlignLeft):
        w = QLabel(txt); w.setAlignment(align); w.setWordWrap(True)
        w.setFont(QFont(UI_FONT, fs,
                        QFont.Weight.Bold if bold else QFont.Weight.Normal))
        w.setStyleSheet(f"color: {color}; background: transparent;")
        return w

    def BuildSection(self, form: QVBoxLayout, sec: dict):
        ns     = sec.get("namespace") or sec.get("plugin") or "plugin"
        title  = sec.get("title") or ns
        fields = sec.get("fields") or []
        values = sec.get("values") or {}

        form.addSpacing(4)
        form.addWidget(self.Lbl(title, 10, True, C.PRI))

        for field in fields:
            if not isinstance(field, dict) or not field.get("key"):
                continue
            key   = field["key"]
            ftype = (field.get("type") or "text").lower()
            label = field.get("label") or key
            default = field.get("default")
            stored  = values.get(key, default)

            form.addWidget(self.Lbl(label.upper(), 8, color=C.TEXT_DIM))

            if ftype == "choice":
                w = QComboBox()
                w.addItems([str(o) for o in field.get("options", [])])
                w.setFont(QFont(UI_FONT, 10))
                w.setFixedHeight(30)
                w.setStyleSheet(
                    f"QComboBox {{ background: #000d12; color: {C.TEXT}; "
                    f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 8px; }}"
                    f"QComboBox QAbstractItemView {{ background: #000d12; color: {C.TEXT}; "
                    f"selection-background-color: {C.PRI_GHO}; }}")
                if stored is not None:
                    w.setCurrentText(str(stored))
            elif ftype == "toggle":
                w = QPushButton()
                w.setCheckable(True)
                w.setChecked(bool(stored))
                w.setFixedHeight(28)
                w.setFont(QFont(UI_FONT, 9, QFont.Weight.Bold))
                w.setCursor(Qt.CursorShape.PointingHandCursor)
                self.StyleToggle(w)
                w.toggled.connect(lambda _=False, b=w: self.StyleToggle(b))
            else:  # text / password
                w = QLineEdit("" if stored is None else str(stored))
                w.setFont(QFont(UI_FONT, 10))
                w.setFixedHeight(30)
                w.setStyleSheet(self._fs)
                if field.get("placeholder"):
                    w.setPlaceholderText(str(field["placeholder"]))
                if ftype == "password":
                    w.setEchoMode(QLineEdit.EchoMode.Password)

            self._widgets[(ns, key)] = w
            self._types[(ns, key)]   = ftype
            form.addWidget(w)

        # optional test/connect action button + status line
        action = sec.get("action")
        if isinstance(action, dict) and callable(action.get("run")):
            form.addSpacing(2)
            ab = QPushButton(str(action.get("label") or "TEST"))
            ab.setFixedHeight(30)
            ab.setFont(QFont(UI_FONT, 9, QFont.Weight.Bold))
            ab.setCursor(Qt.CursorShape.PointingHandCursor)
            ab.setStyleSheet(f"""
                QPushButton {{ background: #00091a; color: {C.PRI};
                    border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
            """)
            ab.clicked.connect(lambda _=False, n=ns: self.RunAction(n))
            form.addWidget(ab)

        status = self.Lbl("", 8, color=C.TEXT_DIM)
        self._status_labels[ns] = status
        form.addWidget(status)

        line = QFrame(); line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: {C.BORDER}; margin: 4px 0;")
        form.addWidget(line)

    def StyleToggle(self, btn: QPushButton):
        on = btn.isChecked()
        btn.setText("ON" if on else "OFF")
        if on:
            btn.setStyleSheet(f"QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI}; "
                              f"border: 1px solid {C.PRI}; border-radius: 3px; }}")
        else:
            btn.setStyleSheet(f"QPushButton {{ background: transparent; color: {C.TEXT_MED}; "
                              f"border: 1px solid {C.BORDER}; border-radius: 3px; }}")

    # ── data ──────────────────────────────────────────────────────────────────
    def Gather(self, ns: str) -> dict:
        out = {}
        for (n, key), w in self._widgets.items():
            if n != ns:
                continue
            t = self._types.get((n, key), "text")
            if t == "choice":
                out[key] = w.currentText()
            elif t == "toggle":
                out[key] = w.isChecked()
            else:
                out[key] = w.text().strip()
        return out

    def SaveNs(self, ns: str):
        from Brain.Settings import SavePluginConfig
        SavePluginConfig(ns, self.Gather(ns))

    def _SaveAll(self):
        for sec in self._sections:
            ns = sec.get("namespace") or sec.get("plugin")
            if ns:
                self.SaveNs(ns)
                lbl = self._status_labels.get(ns)
                if lbl:
                    lbl.setText("Saved ✓")
                    lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")

    def RunAction(self, ns: str):
        sec = next((s for s in self._sections
                    if (s.get("namespace") or s.get("plugin")) == ns), None)
        if not sec:
            return
        run_fn = (sec.get("action") or {}).get("run")
        if not callable(run_fn):
            return
        self.SaveNs(ns)                 # persist what the user typed before testing
        values = self.Gather(ns)
        lbl = self._status_labels.get(ns)
        if lbl:
            lbl.setText("Testing…")
            lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")

        def worker():
            try:
                res = run_fn(values)
                if isinstance(res, tuple) and len(res) == 2:
                    ok, msg = bool(res[0]), str(res[1])
                else:
                    ok, msg = bool(res), str(res)
            except Exception as e:
                ok, msg = False, str(e)
            self._test_done.emit(ns, ok, msg)

        threading.Thread(target=worker, daemon=True).start()

    def OnTestDone(self, ns: str, ok: bool, msg: str):
        lbl = self._status_labels.get(ns)
        if not lbl:
            return
        lbl.setText(msg)
        color = C.PRI if ok else "#ff6b6b"
        lbl.setStyleSheet(f"color: {color}; background: transparent;")


# ══════════════════════════════════════════════════════════════════════════════
# Settings window, ring gauges and the Usage / System pages
# ══════════════════════════════════════════════════════════════════════════════

class ToggleSwitch(QPushButton):
    """A small on/off switch (a checkable button painted as a pill)."""

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setFixedSize(42, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("background: transparent; border: none;")

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isChecked()
        r = QRectF(1, 2, self.width() - 2, self.height() - 4)
        p.setPen(QPen(Qcol(C.PRI_DIM if on else C.BORDER_B), 1))
        p.setBrush(Qcol(C.PRI, 70) if on else Qcol(C.BAR_BG))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = r.height() - 6
        x = r.right() - d - 3 if on else r.left() + 3
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(Qcol(C.WHITE) if on else Qcol(C.TEXT_DIM))
        p.drawEllipse(QRectF(x, r.top() + 3, d, d))
        p.end()


class RingGauge(QWidget):
    """A thin ring that fills with a percentage, the value in the middle and a
    short name under it. Used in the top bar and on Settings › System."""

    def __init__(self, label: str, size: int = 34, show_label: bool = True, parent=None):
        super().__init__(parent)
        self._label = label
        self._size = size
        self._show_label = show_label
        self._pct = 0.0
        self._shown = 0.0
        self._text = "–"
        self.setFixedSize(size + 8, size + (14 if show_label else 4))
        self._anim = QTimer(self)
        self._anim.setInterval(30)
        self._anim.timeout.connect(self._Ease)

    def SetValue(self, pct: float, text: str) -> None:
        self._pct = max(0.0, min(100.0, float(pct)))
        self._text = text
        self._anim.start()
        self.update()

    def _Ease(self):
        self._shown += (self._pct - self._shown) * 0.25
        if abs(self._pct - self._shown) < 0.3:
            self._shown = self._pct
            self._anim.stop()
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = self._size
        x0 = (self.width() - s) / 2
        rect = QRectF(x0 + 2, 2, s - 4, s - 4)
        thick = max(2.0, s / 12)
        p.setPen(QPen(Qcol(C.BORDER_B), thick))
        p.drawEllipse(rect)
        hot = self._pct >= 90
        col = Qcol(C.RED) if hot else Qcol(C.PRI)
        pen = QPen(col, thick)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        span = int(-self._shown / 100 * 360 * 16)
        if span:
            p.drawArc(rect, 90 * 16, span)
        p.setPen(Qcol(C.WHITE))
        f = QFont(UI_FONT, max(6, int(s / 5)), QFont.Weight.DemiBold)
        p.setFont(f)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._text)
        if self._show_label:
            p.setPen(Qcol(C.TEXT_DIM))
            lf = QFont(UI_FONT, 7)
            lf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1)
            p.setFont(lf)
            p.drawText(QRectF(0, s, self.width(), self.height() - s),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, self._label)
        p.end()


def _Compact(n: int) -> str:
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _Card() -> tuple[QFrame, QVBoxLayout]:
    card = QFrame()
    card.setObjectName("SettingsCard")
    card.setStyleSheet(f"""
        QFrame#SettingsCard {{
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid {GLASS_BORDER}; border-radius: 14px;
        }}
    """)
    lay = QVBoxLayout(card)
    lay.setContentsMargins(18, 14, 18, 14)
    lay.setSpacing(10)
    return card, lay


def _Text(text: str, size: int = 10, color: str = "", weight=QFont.Weight.Normal,
          wrap: bool = False) -> QLabel:
    l = QLabel(text)
    l.setFont(QFont(UI_FONT, size, weight))
    l.setStyleSheet(f"color: {color or C.TEXT}; background: transparent; border: none;")
    l.setWordWrap(wrap)
    return l


def _Caption(text: str) -> QLabel:
    l = QLabel(text.upper())
    f = QFont(UI_FONT, 8, QFont.Weight.DemiBold)
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2)
    l.setFont(f)
    l.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
    return l


def _SettingRow(title: str, detail: str, control: QWidget) -> QWidget:
    row = QWidget()
    row.setStyleSheet("background: transparent;")
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 2, 0, 2)
    h.setSpacing(14)
    col = QVBoxLayout()
    col.setSpacing(2)
    col.addWidget(_Text(title, 10, C.TEXT, QFont.Weight.DemiBold))
    if detail:
        col.addWidget(_Text(detail, 8, C.TEXT_DIM, wrap=True))
    h.addLayout(col, 1)
    h.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
    return row


def _GhostButton(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFont(QFont(UI_FONT, 9))
    b.setFixedHeight(30)
    b.setStyleSheet(f"""
        QPushButton {{ background: {GLASS_HOVER}; color: {C.TEXT};
                       border: 1px solid {GLASS_BORDER}; border-radius: 15px; padding: 0 16px; }}
        QPushButton:hover {{ background: {C.PRI_GHO}; color: {C.WHITE}; border-color: {C.PRI_DIM}; }}
    """)
    return b


class DailyBars(QWidget):
    """Tokens per day as thin bars, the usual day as a dashed line, today bright."""

    def __init__(self, days: list[dict], parent=None):
        super().__init__(parent)
        self._days = days
        self.setMinimumHeight(120)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height() - 18
        days = self._days
        top = max([d["tokens"] for d in days] + [1])
        n = len(days)
        slot = W / max(1, n)
        bw = max(3.0, slot * 0.45)
        p.setPen(QPen(Qcol(C.BORDER_B), 1))
        p.drawLine(QPointF(0, H), QPointF(W, H))
        used = [d["tokens"] for d in days if d["tokens"] > 0]
        if used:
            avg = sum(used) / len(used)
            y = H - avg / top * (H - 8)
            pen = QPen(Qcol(C.TEXT_DIM), 1, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(0, y), QPointF(W, y))
        p.setPen(Qt.PenStyle.NoPen)
        for i, d in enumerate(days):
            h = d["tokens"] / top * (H - 8)
            x = i * slot + (slot - bw) / 2
            col = Qcol(C.WHITE) if i == n - 1 else Qcol(C.PRI, 150)
            p.setBrush(col)
            if h > 0:
                p.drawRoundedRect(QRectF(x, H - h, bw, h), bw / 2, bw / 2)
        p.setPen(Qcol(C.TEXT_DIM))
        p.setFont(QFont(UI_FONT, 7))
        try:
            from datetime import date as _d
            first = _d.fromisoformat(days[0]["day"]).strftime("%d %b")
        except Exception:
            first = ""
        p.drawText(QRectF(0, H + 4, W / 2, 14), Qt.AlignmentFlag.AlignLeft, first)
        p.drawText(QRectF(W / 2, H + 4, W / 2, 14), Qt.AlignmentFlag.AlignRight, "Today")
        p.end()


def _ToolLabel(name: str) -> str:
    return name.replace("_", " ").capitalize()


class UsagePage(QWidget):
    """Settings › Usage: tokens today and over two weeks, and which tools ran."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from Brain import Usage
        self._usage = Usage
        self._window = 7
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        today = Usage.Today()
        usual = Usage.UsualDay()
        card, c = _Card()
        c.addWidget(_Caption("Today"))
        big = QHBoxLayout()
        big.setSpacing(8)
        big.addWidget(_Text(f"{today['tokens']:,}", 26, C.WHITE, QFont.Weight.Light))
        big.addWidget(_Text("tokens", 10, C.TEXT_DIM), 0, Qt.AlignmentFlag.AlignBottom)
        big.addStretch()
        c.addLayout(big)
        if usual > 0 and today["tokens"] > 0:
            ratio = today["tokens"] / usual
            if ratio >= 1.15:
                line = f"{(ratio - 1) * 100:.0f}% more than a usual day ({_Compact(usual)})"
            elif ratio <= 0.85:
                line = f"{(1 - ratio) * 100:.0f}% less than a usual day ({_Compact(usual)})"
            else:
                line = f"About a usual day ({_Compact(usual)})"
            c.addWidget(_Text(line, 9, C.TEXT_MED))
        stats = QHBoxLayout()
        for label, value in (("Turns", f"{today['turns']:,}"),
                             ("Per turn", _Compact(today["tokens"] / today["turns"]) if today["turns"] else "–"),
                             ("This session", _Compact(Usage.SessionTokens()))):
            col = QVBoxLayout()
            col.setSpacing(0)
            col.addWidget(_Text(value, 13, C.TEXT, QFont.Weight.DemiBold))
            col.addWidget(_Text(label, 8, C.TEXT_DIM))
            stats.addLayout(col, 1)
        c.addLayout(stats)
        lay.addWidget(card)

        days = Usage.RecentDays(14)
        card, c = _Card()
        c.addWidget(_Caption("Last 14 days"))
        c.addWidget(DailyBars(days))
        used = [d for d in days if d["tokens"] > 0]
        if used:
            total = sum(d["tokens"] for d in used)
            c.addWidget(_Text(f"{total:,} tokens over {len(used)} of 14 days · "
                              f"{total // len(used):,} on an average day of use", 8, C.TEXT_DIM))
        else:
            c.addWidget(_Text("Nothing used in the last 14 days.", 8, C.TEXT_DIM))
        lay.addWidget(card)

        card, self._tools_lay = _Card()
        head = QHBoxLayout()
        head.addWidget(_Caption("Tools"))
        head.addStretch()
        self._pills = []
        for n in (7, 30):
            b = QPushButton(f"{n} days")
            b.setCheckable(True)
            b.setChecked(n == self._window)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFont(QFont(UI_FONT, 8))
            b.setFixedHeight(24)
            b.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                               border: 1px solid {GLASS_BORDER}; border-radius: 12px; padding: 0 10px; }}
                QPushButton:checked {{ background: {C.PRI_GHO}; color: {C.WHITE}; border-color: {C.PRI_DIM}; }}
            """)
            b.clicked.connect(lambda _=False, n=n: self._ShowTools(n))
            self._pills.append((n, b))
            head.addWidget(b)
        self._tools_lay.addLayout(head)
        self._tools_box = QVBoxLayout()
        self._tools_box.setSpacing(6)
        self._tools_lay.addLayout(self._tools_box)
        lay.addWidget(card)
        lay.addWidget(_Text("Counted on this computer from what Gemini reports with each reply. "
                            "Nothing is sent anywhere.", 8, C.TEXT_DIM, wrap=True))
        lay.addStretch()
        self._ShowTools(self._window)

    def _ShowTools(self, days: int):
        self._window = days
        for n, b in self._pills:
            b.setChecked(n == days)
        while self._tools_box.count():
            item = self._tools_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        stats = self._usage.ToolStats(days)
        if not stats:
            self._tools_box.addWidget(_Text("No tools used in this period.", 9, C.TEXT_DIM))
            return
        for s in stats[:12]:
            calls, failed = s["calls"], s["failed"]
            worked = "all worked" if failed == 0 else f"{(calls - failed) * 100 // calls}% worked"
            secs = s["avg_ms"] / 1000
            row = QHBoxLayout()
            row.addWidget(_Text(_ToolLabel(s["name"]), 10, C.TEXT), 1)
            row.addWidget(_Text(f"{calls} call{'s' if calls != 1 else ''} · {worked} · "
                                f"{secs:.1f} s avg", 8, C.TEXT_MED))
            holder = QWidget()
            holder.setStyleSheet("background: transparent;")
            holder.setLayout(row)
            row.setContentsMargins(0, 0, 0, 0)
            self._tools_box.addWidget(holder)


class SystemPage(QWidget):
    """Settings › System: the computer's load, refreshed every two seconds."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        card, c = _Card()
        c.addWidget(_Caption("Right now"))
        rings = QHBoxLayout()
        self._rings = {}
        for key, label in (("cpu", "CPU"), ("mem", "MEMORY"), ("gpu", "GPU"), ("tmp", "TEMP")):
            g = RingGauge(label, size=74)
            self._rings[key] = g
            rings.addWidget(g, 1, Qt.AlignmentFlag.AlignHCenter)
        c.addLayout(rings)
        lay.addWidget(card)

        card, c = _Card()
        c.addWidget(_Caption("Details"))
        self._rows = {}
        for key, label in (("net", "Network"), ("up", "Up since boot"), ("proc", "Processes"),
                           ("os", "System"), ("py", "Python")):
            row = QHBoxLayout()
            row.addWidget(_Text(label, 10, C.TEXT_MED), 1)
            val = _Text("–", 10, C.TEXT)
            row.addWidget(val)
            self._rows[key] = val
            c.addLayout(row)
        lay.addWidget(card)
        lay.addStretch()
        os_name = {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}.get(_OS, _OS)
        self._rows["os"].setText(f"{os_name} {platform.release()}")
        self._rows["py"].setText(platform.python_version())
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.Refresh)
        self._timer.start(2000)
        self.Refresh()

    def Refresh(self):
        try:
            snap = _metrics.Snapshot()
        except Exception:
            return
        self._rings["cpu"].SetValue(snap["cpu"], f"{snap['cpu']:.0f}%")
        self._rings["mem"].SetValue(snap["mem"], f"{snap['mem']:.0f}%")
        g = snap["gpu"]
        self._rings["gpu"].SetValue(max(0, g), f"{g:.0f}%" if g >= 0 else "n/a")
        t = snap["tmp"]
        self._rings["tmp"].SetValue(max(0, t), f"{t:.0f}°" if t >= 0 else "n/a")
        net = snap["net"]
        self._rows["net"].setText(f"{net * 1024:.0f} KB/s" if net < 1 else f"{net:.1f} MB/s")
        try:
            up = time.time() - psutil.boot_time()
            self._rows["up"].setText(f"{int(up // 86400)} d {int(up % 86400 // 3600)} h "
                                     f"{int(up % 3600 // 60)} min")
            self._rows["proc"].setText(f"{len(psutil.pids())}")
        except Exception:
            pass


class SettingsOverlay(_HudOverlay):
    """Every setting in one window: a sidebar of sections and the chosen page.

    Pages are built when first opened. The older panels (Brain, voice and audio,
    look, memory, plugins) are hosted as they are, so their own save buttons and
    logic keep working; their CLOSE buttons close this window instead."""

    closed = pyqtSignal()

    def __init__(self, sections: list[tuple[str, str, object]], start: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("SettingsOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            QWidget#SettingsOverlay {{
                background: rgba(7, 12, 19, 0.97);
                border: 1px solid {GLASS_BORDER}; border-radius: 18px;
            }}
        """)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._sections = sections
        self._closing = False
        self._pages: dict[str, QWidget] = {}
        self._buttons: dict[str, QPushButton] = {}

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        side = QWidget()
        side.setFixedWidth(196)
        side.setStyleSheet(f"background: rgba(255, 255, 255, 0.02); border-top-left-radius: 18px;"
                           f" border-bottom-left-radius: 18px;")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 18, 14, 18)
        sl.setSpacing(4)
        title = _Text("Settings", 15, C.WHITE, QFont.Weight.DemiBold)
        sl.addWidget(title)
        sl.addSpacing(12)
        for key, (glyph, label, _factory) in [(s[0], s[1:]) for s in sections]:
            b = QPushButton(label.replace("&", "&&"))
            b.setCheckable(True)
            b.setFixedHeight(36)
            b.setFont(QFont(UI_FONT, 10))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {C.TEXT_MED}; border: none;
                               border-radius: 10px; text-align: left; padding: 0 12px; }}
                QPushButton:hover {{ background: {GLASS_HOVER}; color: {C.WHITE}; }}
                QPushButton:checked {{ background: {C.PRI_GHO}; color: {C.WHITE}; }}
            """)
            b.clicked.connect(lambda _=False, k=key: self.Show(k))
            self._buttons[key] = b
            sl.addWidget(b)
        sl.addStretch()
        root.addWidget(side)

        right = QVBoxLayout()
        right.setContentsMargins(26, 18, 18, 18)
        right.setSpacing(12)
        head = QHBoxLayout()
        self._title = _Text("", 16, C.WHITE, QFont.Weight.Light)
        head.addWidget(self._title, 1)
        close = QPushButton("✕")
        close.setFixedSize(32, 32)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setToolTip("Close  [Esc]")
        close.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED}; border: none;
                           border-radius: 16px; font-size: 13px; }}
            QPushButton:hover {{ background: {GLASS_HOVER}; color: {C.WHITE}; }}
        """)
        close.clicked.connect(self.close)
        head.addWidget(close)
        right.addLayout(head)
        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background: transparent;")
        right.addWidget(self._stack, 1)
        root.addLayout(right, 1)

        self.Show(start or sections[0][0])

    def Show(self, key: str) -> None:
        for k, b in self._buttons.items():
            b.setChecked(k == key)
        label = next((s[2] for s in self._sections if s[0] == key), "")
        self._title.setText(label)
        page = self._pages.get(key)
        if page is None:
            page = self._Build(key)
            self._pages[key] = page
            self._stack.addWidget(page)
        self._stack.setCurrentWidget(page)

    def _Build(self, key: str) -> QWidget:
        factory = next(s[3] for s in self._sections if s[0] == key)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
            QScrollBar::handle:vertical {{ background: {C.BORDER_B}; border-radius: 4px; min-height: 30px; }}
            QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
        """)
        holder = QWidget()
        holder.setStyleSheet("background: transparent;")
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 10, 0)
        try:
            content = factory(holder)
        except Exception as e:
            content = _Text(f"This section could not open: {e}", 10, C.TEXT_MED, wrap=True)
        for w in (content if isinstance(content, (list, tuple)) else [content]):
            if type(w).__name__.endswith("Overlay"):
                self._Adopt(w, key)
            hl.addWidget(w)
        hl.addStretch()
        scroll.setWidget(holder)
        return scroll

    def _Adopt(self, panel: QWidget, key: str) -> None:
        """Host an older floating panel inside a page: no frame of its own, no
        title (the page header names it), no CLOSE button (✕ and Esc close the
        window), and long lines wrap instead of pushing the page wider."""
        panel.setMinimumWidth(0)
        panel.setMaximumWidth(780)
        panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        name = type(panel).__name__
        panel.setStyleSheet(panel.styleSheet() +
                            "\n" + f"{name} {{ background: transparent; border: none; }}")
        if panel.layout() is not None:
            panel.layout().setContentsMargins(0, 0, 0, 0)
        title = self._FirstLabel(panel.layout())
        if title is not None and title.font().bold():
            title.hide()
            # the hairline that usually sits under the title
            for f in panel.findChildren(QFrame):
                if f.frameShape() == QFrame.Shape.HLine:
                    f.hide()
                    break
        for lbl in panel.findChildren(QLabel):
            if len(lbl.text()) > 50 and not lbl.wordWrap():
                lbl.setWordWrap(True)
        for b in panel.findChildren(QPushButton):
            t = b.text().strip().upper()
            if t == "CLOSE":
                b.hide()
            elif t == "CANCEL":
                b.clicked.connect(self.close)
        panel.installEventFilter(self)
        panel._settings_key = key

    @classmethod
    def _FirstLabel(cls, layout):
        if layout is None:
            return None
        for i in range(layout.count()):
            item = layout.itemAt(i)
            w = item.widget()
            if isinstance(w, QLabel):
                return w
            if w is not None and not isinstance(w, QLabel):
                return None
            if item.layout() is not None:
                found = cls._FirstLabel(item.layout())
                if found is not None:
                    return found
        return None

    def eventFilter(self, obj, ev):
        # A hosted panel that hid ITSELF (after saving, say): build it again so
        # the page is never left blank. Switching section or closing this
        # window also hides it, but then the panel is not hidden on its own
        # account (isHidden() is False), so nothing is rebuilt.
        if (ev.type() == ev.Type.Hide and not self._closing and obj.isHidden()
                and hasattr(obj, "_settings_key")):
            QTimer.singleShot(0, self, lambda k=obj._settings_key: self._Rebuild(k))
        return False

    def _Rebuild(self, key: str) -> None:
        if self._closing or not self.isVisible():
            return
        old = self._pages.pop(key, None)
        if old is not None:
            self._stack.removeWidget(old)
            old.deleteLater()
        if self._buttons.get(key) and self._buttons[key].isChecked():
            self.Show(key)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(e)

    def closeEvent(self, e):
        self._closing = True
        super().closeEvent(e)
        self.closed.emit()
        self.deleteLater()


class _ClickArea(QWidget):
    """A plain widget you can click (the top-bar gauges open Settings › System)."""
    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class MainWindow(QMainWindow):
    _log_sig        = pyqtSignal(str)
    _state_sig      = pyqtSignal(str)
    _content_sig    = pyqtSignal(str, str)   # (title, text) — thread-safe content display
    _reconfig_sig   = pyqtSignal()           # trigger setup overlay from any thread
    _camera_sig     = pyqtSignal(bytes)      # show camera frame preview (small overlay)
    _cam_stream_sig = pyqtSignal(bool)       # True=start live stream, False=stop
    _cam_frame_sig  = pyqtSignal(bytes)      # live camera frame → HUD area
    _video_open_sig  = pyqtSignal(str, str, bool, str)  # video, title, muted, audio
    _video_close_sig = pyqtSignal()
    _video_mute_sig  = pyqtSignal(bool)
    _clipboard_sig  = pyqtSignal(str)        # clipboard text changed (thread-safe)
    _confirm_sig    = pyqtSignal(str, str)   # (title, detail) — irreversible-action gate
    _confirm_hide_sig = pyqtSignal()
    _quiz_sig       = pyqtSignal(str, object, object)  # (topic, questions, grader)
    _quiz_hide_sig  = pyqtSignal()
    _review_sig     = pyqtSignal(str, str, object, object)  # document review payload

    def __init__(self):
        super().__init__()

        # Load customization from config
        _cfg = ReadFullConfig()
        self._assistant_name: str = (_cfg.get("assistant_name") or "ULTRON").strip()
        _display = self._assistant_name.upper()

        # Apply the saved UI colour BEFORE panels/stylesheets are built
        _ui_color = (_cfg.get("ui_color") or "").strip()
        if _ui_color and _ui_color.lower() != DEFAULT_UI_COLOR:
            ApplyUiAccent(_ui_color)

        self.setWindowTitle(f"{_display} — {APP_VERSION}")
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            (screen.width()  - _DEFAULT_W) // 2,
            (screen.height() - _DEFAULT_H) // 2,
        )

        self.on_text_command   = None
        self.on_interrupt      = None   # callable: () -> None — stop ULTRON mid-speech
        self.on_voice_change   = None   # callable: () -> None — rebuild session with new voice
        self.on_audio_device_change = None  # callable: () -> None — reopen audio streams
        self._confirm_overlay  = None   # live ConfirmBanner, if one is on screen
        self.get_plugins       = None   # callable: () -> list[dict], set by UltronLive
        self.get_plugin_settings = None # callable: () -> list[dict] settings schemas, set by UltronLive
        self.on_push_to_talk   = None   # callable: (enable: bool) -> str scope
        self.ptt_hold          = None   # callable: (held: bool) -> None — windowed chord
        self._muted            = False
        self._current_file: str | None = None
        self._customize_overlay: CustomizeOverlay | None = None

        central = QWidget()
        central.setStyleSheet(f"background: {C.BG};")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.BuildHeader())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        # Floating, not columns: the reactor gets the whole width. The stats
        # card and the conversation drawer open over it when asked for.

        # Center column: HUD + resizable content panel via QSplitter
        self.hud = HudCanvas(_display)
        self.hud.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._content_panel = self.BuildContentPanel()
        self._quiz_panel = self.BuildQuizPanel()

        # Live camera container — replaces HUD when camera stream is active
        _cam_cont = QWidget()
        _cam_cont.setStyleSheet("background: #000308;")
        _cam_v = QVBoxLayout(_cam_cont)
        _cam_v.setContentsMargins(0, 0, 0, 0)
        _cam_v.setSpacing(0)
        _cam_hdr = QHBoxLayout()
        _cam_hdr.setContentsMargins(8, 5, 8, 5)
        _cam_title = QLabel("◈  CAMERA FEED")
        _cam_title.setFont(QFont(UI_FONT, 9, QFont.Weight.Bold))
        _cam_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        _cam_hdr.addWidget(_cam_title)
        _cam_hdr.addStretch()
        _cam_x = QPushButton("✕  CLOSE")
        _cam_x.setFont(QFont(UI_FONT, 9, QFont.Weight.Bold))
        _cam_x.setCursor(Qt.CursorShape.PointingHandCursor)
        _cam_x.setStyleSheet(f"""
            QPushButton {{
                color: {C.TEXT_DIM}; background: transparent;
                border: none; padding: 2px 6px;
            }}
            QPushButton:hover {{ color: {C.PRI}; }}
        """)
        _cam_x.clicked.connect(self.StopCameraStream)
        _cam_hdr.addWidget(_cam_x)
        _cam_v.addLayout(_cam_hdr)
        self._cam_live_lbl = QLabel()
        self._cam_live_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cam_live_lbl.setStyleSheet("background: transparent;")
        self._cam_live_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        _cam_v.addWidget(self._cam_live_lbl, stretch=1)

        # ── Video surface ────────────────────────────────────────────────
        # Built exactly like the camera page above and stacked beside it,
        # because it is the same idea: something takes the centre of the HUD
        # for a while and then gives it back. Sharing the stack rather than
        # inventing a second mechanism means the reactor, the camera and a video
        # can never be on screen at once.
        self._video_split = False        # is the sound a separate stream?
        self._video_auto_muted = False   # did ULTRON close the mic, or the user?
        # A plain flag rather than reading the widget. video_is_playing() is
        # called from plugin threads, and reading a widget's state from one is
        # not something to rely on; an attribute is.
        self._video_on = False
        self._video_cont = QWidget()
        self._video_cont.setStyleSheet(f"background: {C.BG};")
        _vid_v = QVBoxLayout(self._video_cont)
        _vid_v.setContentsMargins(8, 6, 8, 8)
        _vid_v.setSpacing(4)

        _vid_hdr = QHBoxLayout()
        self._video_title = QLabel("▶  VIDEO")
        self._video_title.setFont(QFont(UI_FONT, 10, QFont.Weight.Bold))
        self._video_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        _vid_hdr.addWidget(self._video_title)
        _vid_hdr.addStretch()

        def VidBtn(text: str) -> QPushButton:
            b = QPushButton(text)
            b.setFont(QFont(UI_FONT, 9, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{
                    color: {C.TEXT_DIM}; background: transparent;
                    border: none; padding: 2px 6px;
                }}
                QPushButton:hover {{ color: {C.PRI}; }}
            """)
            return b

        # Muted is the default and the button says so, because a soundtrack
        # talking over ULTRON is the one way this feature could make the
        # assistant worse rather than better.
        self._video_mute_btn = VidBtn("🔇  SOUND OFF")
        self._video_mute_btn.clicked.connect(self.ToggleVideoMute)
        _vid_hdr.addWidget(self._video_mute_btn)

        _vid_x = VidBtn("✕  CLOSE")
        _vid_x.clicked.connect(self.StopVideo)
        _vid_hdr.addWidget(_vid_x)
        _vid_v.addLayout(_vid_hdr)

        if HAVE_VIDEO:
            # A GRAPHICS ITEM, NOT A QVideoWidget.
            #
            # QVideoWidget gets a native window of its own, and a native window
            # is painted by the compositor above every ordinary widget that
            # shares the screen with it — so the settings drawer opened
            # *underneath* the video and raise_() could not help, because the
            # two are not in the same painting order at all. A video item drawn
            # into a QGraphicsView goes through Qt's own painter like any other
            # widget, and anything laid over it stays over it.
            self._video_scene = QGraphicsScene(self)
            self._video_item = QGraphicsVideoItem()
            self._video_scene.addItem(self._video_item)
            self._video_widget = QGraphicsView(self._video_scene)
            self._video_widget.setStyleSheet("background: #000; border: none;")
            self._video_widget.setFrameShape(QGraphicsView.Shape.NoFrame)
            self._video_widget.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._video_widget.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._video_widget.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
            _vid_v.addWidget(self._video_widget, stretch=1)

            self._video_audio = QAudioOutput()
            self._video_audio.setMuted(True)
            self._video_player = QMediaPlayer()
            self._video_player.setVideoOutput(self._video_item)
            # Keep the picture filling the view as the window is resized.
            self._video_item.nativeSizeChanged.connect(self.FitVideo)
            self._video_player.setAudioOutput(self._video_audio)
            self._video_player.errorOccurred.connect(self.OnVideoError)

            # A SECOND player, for sound that arrives separately.
            #
            # YouTube no longer serves a single stream carrying both picture and
            # sound — not for new uploads and not for old ones; checked against
            # three videos including the oldest on the site, and every one of
            # them offered zero combined formats. Picture and sound are two
            # URLs, so they are two players, started together and nudged back
            # into line by the timer below.
            #
            # A local file or a direct video URL still uses the first player
            # alone: one stream, one player, nothing to synchronise.
            self._video_sound = QMediaPlayer()
            self._video_sound_out = QAudioOutput()
            self._video_sound_out.setMuted(True)
            self._video_sound.setAudioOutput(self._video_sound_out)

            self._video_sync = QTimer(self)
            self._video_sync.setInterval(1000)
            self._video_sync.timeout.connect(self.SyncVideoSound)
        else:
            self._video_scene = None
            self._video_item = None
            self._video_widget = None
            self._video_player = None
            self._video_audio = None
            self._video_sound = None
            self._video_sound_out = None
            self._video_sync = None
            _miss = QLabel("Video playback is not available in this Qt install.")
            _miss.setAlignment(Qt.AlignmentFlag.AlignCenter)
            _miss.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            _vid_v.addWidget(_miss, stretch=1)

        # Stack: 0 = animated HUD, 1 = live camera, 2 = video
        self._hud_cam_stack = QStackedWidget()
        self._hud_cam_stack.addWidget(self.hud)
        self._hud_cam_stack.addWidget(_cam_cont)
        self._hud_cam_stack.addWidget(self._video_cont)

        self._center_split = QSplitter(Qt.Orientation.Vertical)
        self._center_split.setStyleSheet(f"""
            QSplitter::handle {{
                background: {C.BORDER};
                height: 4px;
            }}
            QSplitter::handle:hover {{
                background: {C.PRI_DIM};
            }}
        """)
        self._center_split.addWidget(self._hud_cam_stack)
        self._center_split.addWidget(self._content_panel)
        self._center_split.addWidget(self._quiz_panel)
        self._center_split.setStretchFactor(0, 3)
        self._center_split.setStretchFactor(1, 1)
        self._center_split.setCollapsible(0, False)
        body.addWidget(self._center_split, stretch=1)

        self._right_panel = self.BuildRightPanel()

        root.addLayout(body, stretch=1)
        root.addWidget(self.BuildDock())


        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self.TickClock)
        self._clock_tmr.start(1000)
        self.TickClock()

        # Metric update timer
        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self.UpdateMetrics)
        self._metric_tmr.start(2000)
        self.UpdateMetrics()

        self._log_sig.connect(self._log.AppendLog)
        self._log_sig.connect(self.OnLogCaption)
        self._state_sig.connect(self.ApplyState)
        self._content_sig.connect(self._ShowContent)
        self._reconfig_sig.connect(self.ShowSetup)
        self._camera_sig.connect(self._ShowCameraFrame)
        self._confirm_sig.connect(self.ShowConfirmBanner)
        self._confirm_hide_sig.connect(self.HideConfirmBanner)
        self._cam_stream_sig.connect(self.OnCamStream)
        self._cam_frame_sig.connect(self.OnCamFrame)
        self._video_open_sig.connect(self.OnVideoOpen)
        self._video_close_sig.connect(self.OnVideoClose)
        self._video_mute_sig.connect(self.OnVideoMute)
        self._clipboard_sig.connect(self.ShowClipboardPanel)
        self._quiz_sig.connect(self._ShowQuiz)
        self._quiz_hide_sig.connect(self._HideQuiz)
        self._review_sig.connect(self._ShowReview)
        self._cam_stop = threading.Event()

        # The conversation drawer starts open (placed on first show).
        self._history_btn.setChecked(True)
        self._history_placed = False

        # Camera preview overlay (child of central widget, positioned in resizeEvent)
        self._cam_preview = _CameraPreview(self.centralWidget())

        # Clipboard panel (child of central widget, bottom-center)
        self._clipboard_panel = ClipboardPanel(self.centralWidget())
        self._clipboard_panel.action_requested.connect(self.OnClipboardAction)
        QApplication.clipboard().dataChanged.connect(self.OnClipboardChanged)

        self._overlay: SetupOverlay | None = None
        self._ready = self.CheckConfig()
        if not self._ready:
            self.ShowSetup()

        sc_mute = QShortcut(QKeySequence("F4"), self)
        sc_mute.activated.connect(self.ToggleMute)
        sc_full = QShortcut(QKeySequence("F11"), self)
        sc_full.activated.connect(self.ToggleFullscreen)
        sc_intr = QShortcut(QKeySequence("Escape"), self)
        sc_intr.activated.connect(self.DoInterrupt)

    def _ShowCameraFrame(self, img_bytes: bytes):
        """Slot — display camera preview overlay (main thread)."""
        self._cam_preview.ShowFrame(img_bytes)
        cw = self.centralWidget()
        pw = _CameraPreview._W
        ph = self._cam_preview.height()
        self._cam_preview.setGeometry(
            cw.width() - pw - 16,
            cw.height() - ph - 28,
            pw, ph,
        )

    # --- Live camera stream in HUD area ------------------------------------
    def OnCamStream(self, start: bool) -> None:
        if start:
            self._hud_cam_stack.setCurrentIndex(1)
        else:
            self._hud_cam_stack.setCurrentIndex(0)
            self._cam_live_lbl.clear()

    def OnCamFrame(self, data: bytes) -> None:
        px = QPixmap()
        px.loadFromData(data)
        if not px.isNull():
            w, h = self._cam_live_lbl.width(), self._cam_live_lbl.height()
            if w > 1 and h > 1:
                self._cam_live_lbl.setPixmap(
                    px.scaled(w, h,
                              Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
                )

    def StartCameraStream(self) -> None:
        self._cam_stop.clear()
        self._cam_stream_sig.emit(True)
        t = threading.Thread(target=self.CamLoop, daemon=True, name="cam-stream")
        t.start()

    def CamLoop(self) -> None:
        try:
            import cv2
            # Reuse camera index detected by screen_processor (cached in ApiKeys.json)
            cam_idx = 0
            try:
                import json as _j
                cfg = _j.loads((CONFIG_DIR / "ApiKeys.json").read_text())
                cam_idx = int(cfg.get("camera_index", 0))
            except Exception:
                pass
            try:
                backend = cv2.CAP_DSHOW if _OS == "Windows" else cv2.CAP_ANY
            except AttributeError:
                backend = 0
            cap = cv2.VideoCapture(cam_idx, backend)
            if not cap.isOpened():
                cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                return
            # warm-up frames
            for _ in range(5):
                cap.read()
            while not self._cam_stop.wait(0.033) and cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
                    self._cam_frame_sig.emit(buf.tobytes())
            cap.release()
        except Exception as e:
            print(f"[Camera] Stream error: {e}")
        finally:
            self._cam_stream_sig.emit(False)

    def StopCameraStream(self) -> None:
        self._cam_stop.set()

    # --- Video in the HUD area ---------------------------------------------
    #
    # Everything below runs on the Qt thread. Plugins and the assistant reach it
    # through the signals above, the same way every other panel is driven, so a
    # background thread never touches a widget.
    def OnVideoOpen(self, source: str, title: str, muted: bool,
                       audio_source: str = "") -> None:
        if not HAVE_VIDEO or not self._video_player:
            self._log.AppendLog("SYS: Video playback is not available in this Qt "
                           "install.")
            return
        # A video and the live camera cannot share the centre of the HUD.
        self._cam_stop.set()

        self._video_title.setText(f"▶  {(title or 'VIDEO')[:44].upper()}")
        self._video_split = bool(audio_source)
        self._SetVideoMuted(bool(muted))

        url = (QUrl.fromLocalFile(source) if Path(source).exists()
               else QUrl(source))
        self._video_player.setSource(url)
        if self._video_split:
            self._video_sound.setSource(QUrl(audio_source))
        self._hud_cam_stack.setCurrentIndex(2)
        self._video_on = True
        # Re-run now that the video counts as playing: _set_video_muted ran
        # before this line and saw no video, so its mic check was a no-op.
        self.SyncMicForVideo()
        self._video_player.play()
        if self._video_split:
            self._video_sound.play()
            self._video_sync.start()

    def FitVideo(self, *_a) -> None:
        """Size the picture to the panel, keeping its shape."""
        if not (self._video_item and self._video_widget):
            return
        try:
            native = self._video_item.nativeSize()
            if native.isEmpty():
                return
            view = self._video_widget.viewport().size()
            scale = min(view.width() / native.width(),
                        view.height() / native.height())
            w, h = native.width() * scale, native.height() * scale
            self._video_item.setSize(QSizeF(w, h))
            self._video_scene.setSceneRect(0, 0, w, h)
            self._video_widget.centerOn(self._video_item)
        except Exception:
            pass

    def OnVideoClose(self) -> None:
        if self._video_sync:
            self._video_sync.stop()
        for p in (self._video_player, self._video_sound):
            if p:
                p.stop()
                p.setSource(QUrl())
        self._video_split = False
        self._video_on = False
        self._hud_cam_stack.setCurrentIndex(0)
        self.SyncMicForVideo()      # gives the microphone back

    def _SetVideoMuted(self, muted: bool) -> None:
        """Silence whichever output is carrying the sound for this video."""
        muted = bool(muted)
        if self._video_audio:
            # When the sound is a separate stream this player has none, but
            # muting it too costs nothing and keeps the two paths identical.
            self._video_audio.setMuted(muted)
        if self._video_sound_out:
            self._video_sound_out.setMuted(muted)
        self.SyncVideoMuteBtn()
        self.SyncMicForVideo()

    def OnVideoMute(self, muted: bool) -> None:
        self._SetVideoMuted(muted)

    def SyncMicForVideo(self) -> None:
        """Close the microphone while the video is making sound.

        ULTRON subtracts its OWN output from the microphone — that is what
        core/echo.py does — but a video plays through a different output
        entirely, so the guard has never heard of it and the assistant answers
        the film. There is no arrangement in which an open microphone and a
        loudspeaker in the same room do not do that.

        So the microphone closes for exactly as long as the sound is on, and
        opens again by itself the moment it goes off or the video is closed.
        Only if ULTRON closed it: a microphone the user muted themselves stays
        muted, and pressing the mute key during a video hands the decision back
        to them for good.
        """
        sound_on = bool(self._video_on and self._video_sound_out
                        and not self._video_sound_out.isMuted())
        if sound_on and not self._muted:
            self._video_auto_muted = True
            self.SetMuted(True, "The video's sound is on — silence it, close "
                                  "it, or press F4 to talk. Typing still works.")
        elif not sound_on and self._video_auto_muted:
            self._video_auto_muted = False
            self.SetMuted(False, "The video is quiet again.")

    def SyncVideoSound(self) -> None:
        """Keep the separate soundtrack in step with the picture.

        Two players started at the same moment do not stay together: they buffer
        independently, and measured on a real stream they were about half a
        second apart after nine seconds. So the sound is nudged back to the
        picture whenever it drifts far enough to hear — and only then, because
        correcting a smaller gap is itself audible.
        """
        if not (self._video_split and self._video_sound and self._video_player):
            return
        try:
            if self._video_player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                return
            drift = self._video_sound.position() - self._video_player.position()
            if abs(drift) > 300:
                self._video_sound.setPosition(self._video_player.position())
        except Exception:
            pass

    def SyncVideoMuteBtn(self) -> None:
        out = self._video_sound_out if self._video_split else self._video_audio
        muted = bool(out and out.isMuted())
        self._video_mute_btn.setText("🔇  SOUND OFF" if muted else "🔊  SOUND ON")

    def ToggleVideoMute(self) -> None:
        out = self._video_sound_out if self._video_split else self._video_audio
        if out:
            self._SetVideoMuted(not out.isMuted())

    def OnVideoError(self, *_a) -> None:
        err = ""
        try:
            err = self._video_player.errorString()
        except Exception:
            pass
        self._log.AppendLog(f"SYS: The video could not be played{(' — ' + err) if err else ''}.")
        self.OnVideoClose()

    def StopVideo(self) -> None:
        self._video_close_sig.emit()

    def VideoIsPlaying(self) -> bool:
        return bool(self._video_on)

    # ------------------------------------------------------------------
    # Icon generation — arc-reactor style, rendered with Pillow
    # ------------------------------------------------------------------
    @staticmethod
    def BuildUltronIcon(out_path: Path) -> bool:
        """
        Render a ULTRON arc-reactor icon at 4× resolution and downsample
        for crisp results at all sizes. Saves a multi-res .ico to out_path.
        Returns True on success.
        """
        try:
            import math
            import PIL.Image
            import PIL.ImageDraw
            import PIL.ImageFilter
        except ImportError:
            return False

        CYAN   = (0, 212, 255)
        DIM    = (0, 100, 140)
        DARK   = (0, 6, 10)
        GLOW   = (0, 160, 200)
        WHITE  = (220, 240, 255)

        def Render(sz: int) -> PIL.Image.Image:
            S  = sz * 4                     # draw at 4× then downscale
            img = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            d   = PIL.ImageDraw.Draw(img)
            cx = cy = S // 2

            # ── filled background circle ──────────────────────────────────
            R = S // 2 - 2
            d.ellipse([cx-R, cy-R, cx+R, cy+R], fill=(*DARK, 255))

            # ── outer border ring ─────────────────────────────────────────
            lw = max(2, S // 40)
            d.ellipse([cx-R, cy-R, cx+R, cy+R],
                      outline=(*CYAN, 220), width=lw)

            # ── mid decorative ring ───────────────────────────────────────
            R2 = int(R * 0.72)
            d.ellipse([cx-R2, cy-R2, cx+R2, cy+R2],
                      outline=(*DIM, 180), width=max(1, lw // 2))

            # ── 6 radial spokes (hex bolt) ────────────────────────────────
            R_inner = int(R * 0.30)
            R_outer = int(R * 0.62)
            spoke_w = max(1, S // 80)
            for i in range(6):
                angle = math.radians(i * 60 - 30)
                x1 = cx + int(R_inner * math.cos(angle))
                y1 = cy + int(R_inner * math.sin(angle))
                x2 = cx + int(R_outer * math.cos(angle))
                y2 = cy + int(R_outer * math.sin(angle))
                d.line([x1, y1, x2, y2], fill=(*GLOW, 200), width=spoke_w)

            # ── 6 tick marks on outer ring ────────────────────────────────
            for i in range(6):
                angle = math.radians(i * 60)
                for dr in range(lw * 2):
                    rx = (R - lw - dr)
                    d.point(
                        [cx + int(rx * math.cos(angle)),
                         cy + int(rx * math.sin(angle))],
                        fill=(*WHITE, 220),
                    )

            # ── inner glowing ring ────────────────────────────────────────
            Ri = int(R * 0.26)
            d.ellipse([cx-Ri, cy-Ri, cx+Ri, cy+Ri],
                      outline=(*CYAN, 255), width=max(2, lw))

            # ── bright glow soft blur applied before core ─────────────────
            # (draw a slightly larger cyan circle on a separate layer)
            glow_layer = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            gd = PIL.ImageDraw.Draw(glow_layer)
            Rc = int(R * 0.13)
            gd.ellipse([cx-Rc*2, cy-Rc*2, cx+Rc*2, cy+Rc*2],
                       fill=(*CYAN, 110))
            glow_layer = glow_layer.filter(PIL.ImageFilter.GaussianBlur(S // 14))
            img = PIL.Image.alpha_composite(img, glow_layer)
            d   = PIL.ImageDraw.Draw(img)

            # ── core dot ──────────────────────────────────────────────────
            d.ellipse([cx-Rc, cy-Rc, cx+Rc, cy+Rc], fill=(*WHITE, 255))

            # ── downscale to target size ──────────────────────────────────
            return img.resize((sz, sz), PIL.Image.LANCZOS)

        try:
            sizes  = [256, 128, 64, 48, 32, 16]
            frames = [Render(s) for s in sizes]
            frames[0].save(
                out_path,
                format="ICO",
                append_images=frames[1:],
                sizes=[(s, s) for s in sizes],
            )
            return True
        except Exception as e:
            print(f"[Shortcut] ⚠️  Icon generation failed: {e}")
            return False

    @staticmethod
    def CreateLnkWindows(lnk: str, target: str, args: str,
                             work_dir: str, icon_loc: str) -> None:
        """
        Create a Windows .lnk shortcut WITHOUT launching PowerShell or cmd.
        Tries win32com (pywin32) first; falls back to wscript.exe + VBScript.
        wscript.exe is a GUI-mode host — it never opens a console window.
        """
        # ── Option 1: pywin32 (pure Python COM, zero subprocess) ──────────
        try:
            from win32com.client import Dispatch   # type: ignore
            sh = Dispatch("WScript.Shell")
            sc = sh.CreateShortCut(lnk)
            sc.TargetPath       = target
            sc.Arguments        = f'"{args}"'
            sc.WorkingDirectory = work_dir
            sc.Description      = "ULTRON AI Assistant"
            sc.IconLocation     = icon_loc
            sc.save()
            return
        except ImportError:
            pass

        # ── Option 2: wscript.exe + VBScript (always available on Windows,
        #    GUI-mode executable — never opens a console window) ────────────
        vbs = "\n".join([
            'Set ws = CreateObject("WScript.Shell")',
            f'Set sc = ws.CreateShortcut("{lnk}")',
            f'sc.TargetPath = "{target}"',
            f'sc.Arguments = Chr(34) & "{args}" & Chr(34)',
            f'sc.WorkingDirectory = "{work_dir}"',
            'sc.Description = "ULTRON AI Assistant"',
            f'sc.IconLocation = "{icon_loc}"',
            'sc.Save',
        ])
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".vbs")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(vbs)
            proc = subprocess.Popen(
                ["wscript.exe", "/nologo", tmp],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            )
            proc.wait(timeout=10)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    @staticmethod
    def GetDesktopDir() -> Path:
        """
        Resolve the user's REAL desktop directory instead of assuming
        ~/Desktop, which breaks when:
          • OneDrive "Known Folder Move" relocates the desktop
            (C:/Users/x/OneDrive/Desktop) — very common on Win 10/11;
          • the XDG desktop is localized on Linux (~/Masaüstü,
            ~/Schreibtisch, ~/Bureau, …).
        Falls back to ~/Desktop only as a last resort.
        """
        home = Path.home()
        _os = platform.system()

        if _os == "Windows":
            # ── 1) SHGetKnownFolderPath(FOLDERID_Desktop) — the canonical
            #       answer; follows OneDrive redirection. No dependencies. ──
            try:
                import ctypes
                from ctypes import wintypes

                class _GUID(ctypes.Structure):
                    _fields_ = [("Data1", wintypes.DWORD),
                                ("Data2", wintypes.WORD),
                                ("Data3", wintypes.WORD),
                                ("Data4", ctypes.c_ubyte * 8)]

                # FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
                fid = _GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                            (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9,
                                                 0x9A, 0x87, 0xC6, 0x41))
                buf = ctypes.c_wchar_p()
                if ctypes.windll.shell32.SHGetKnownFolderPath(
                        ctypes.byref(fid), 0, None, ctypes.byref(buf)) == 0:
                    p = Path(buf.value)
                    ctypes.windll.ole32.CoTaskMemFree(buf)
                    if p.is_dir():
                        return p
            except Exception:
                pass

            # ── 2) Registry: User Shell Folders (may contain %VARS%) ──────
            try:
                import winreg
                with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion"
                        r"\Explorer\User Shell Folders") as key:
                    val, _t = winreg.QueryValueEx(key, "Desktop")
                p = Path(os.path.expandvars(val))
                if p.is_dir():
                    return p
            except Exception:
                pass

        elif _os == "Linux":
            # ── xdg-user-dir honours localized names (~/Masaüstü, …) ──────
            try:
                out = subprocess.run(["xdg-user-dir", "DESKTOP"],
                                     capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
                p = Path(out.stdout.strip())
                if out.stdout.strip() and p != home and p.is_dir():
                    return p
            except Exception:
                pass
            try:
                cfg = home / ".config" / "user-dirs.dirs"
                for line in cfg.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("XDG_DESKTOP_DIR"):
                        val = line.split("=", 1)[1].strip().strip('"')
                        p = Path(val.replace("$HOME", str(home)))
                        if p != home and p.is_dir():
                            return p
            except Exception:
                pass

        # macOS: ~/Desktop is always the real path (localization is
        # display-only). Everything else lands here as a last resort.
        return home / "Desktop"

    def CreateDesktopShortcut(self):
        """
        Create a desktop shortcut on Windows / macOS / Linux.
        Never opens a terminal, console, or PowerShell window on any platform.
        """
        import stat as _stat
        script  = Path(__file__).resolve().parent.parent / "Main.py"
        python  = Path(sys.executable)
        desktop = self.GetDesktopDir()

        # Arc-reactor icon (.ico — also exported as .png for Linux/macOS)
        ico_path = Path(__file__).resolve().parent.parent / "Config" / "Ultron.ico"
        if not ico_path.exists():
            self.BuildUltronIcon(ico_path)

        try:
            _os = platform.system()

            # ── Windows ───────────────────────────────────────────────────────
            if _os == "Windows":
                pythonw  = python.parent / "pythonw.exe"
                target   = str(pythonw if pythonw.exists() else python)
                lnk      = str(desktop / "ULTRON.lnk")
                icon_loc = str(ico_path) if ico_path.exists() else f"{target},0"
                self.CreateLnkWindows(lnk, target, str(script),
                                         str(script.parent), icon_loc)

            # ── macOS — proper .app bundle (no Terminal window) ───────────────
            elif _os == "Darwin":
                app     = desktop / "ULTRON.app"
                mac_dir = app / "Contents" / "MacOS"
                res_dir = app / "Contents" / "Resources"
                mac_dir.mkdir(parents=True, exist_ok=True)
                res_dir.mkdir(exist_ok=True)

                # Launcher executable (bash — runs as background process,
                # macOS does NOT open Terminal for executables inside .app bundles)
                launcher = mac_dir / "ULTRON"
                launcher.write_text(
                    "#!/usr/bin/env bash\n"
                    f'cd "{script.parent}"\n'
                    f'exec "{python}" "{script}"\n'
                )
                launcher.chmod(launcher.stat().st_mode
                               | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH)

                # Minimal Info.plist (required for .app recognition)
                (app / "Contents" / "Info.plist").write_text(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                    '<plist version="1.0"><dict>\n'
                    '  <key>CFBundleExecutable</key><string>ULTRON</string>\n'
                    '  <key>CFBundleIdentifier</key>'
                    '<string>com.ultron.assistant</string>\n'
                    '  <key>CFBundleName</key><string>ULTRON</string>\n'
                    '  <key>CFBundlePackageType</key><string>APPL</string>\n'
                    '  <key>CFBundleVersion</key><string>1.0</string>\n'
                    '</dict></plist>\n'
                )

                # Optional: copy icon as .icns (skip silently if Pillow is missing)
                try:
                    import PIL.Image
                    icns = res_dir / "AppIcon.icns"
                    PIL.Image.open(ico_path).save(icns, format="ICNS")
                    # Inject icon reference into plist
                    plist = app / "Contents" / "Info.plist"
                    txt = plist.read_text()
                    plist.write_text(
                        txt.replace(
                            '</dict></plist>',
                            '  <key>CFBundleIconFile</key>'
                            '<string>AppIcon</string>\n</dict></plist>\n',
                        )
                    )
                except Exception:
                    pass  # icon is optional

            # ── Linux — .desktop file (Terminal=false, no console) ────────────
            else:
                # Export .ico → .png for better desktop integration
                png_path = ico_path.with_suffix(".png")
                if not png_path.exists() and ico_path.exists():
                    try:
                        import PIL.Image
                        PIL.Image.open(ico_path).resize(
                            (256, 256), PIL.Image.LANCZOS
                        ).save(png_path, format="PNG")
                    except Exception:
                        png_path = ico_path  # fallback to .ico

                icon_line = f"Icon={png_path}\n" if png_path.exists() else ""
                desk = desktop / "ULTRON.desktop"
                desk.write_text(
                    "[Desktop Entry]\n"
                    "Name=ULTRON\n"
                    f"Exec={python} {script}\n"
                    f"Path={script.parent}\n"
                    "Type=Application\n"
                    "Terminal=false\n"
                    "Categories=Utility;\n"
                    + icon_line
                )
                desk.chmod(desk.stat().st_mode | 0o755)

            self._log.AppendLog("SYS: Desktop shortcut created.")
        except Exception as e:
            self._log.AppendLog(f"ERR: Shortcut failed — {e}")

    def ToggleFullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cw = self.centralWidget()
        if self._overlay and self._overlay.isVisible():
            ow, oh = 460, 390
            self._overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._customize_overlay and self._customize_overlay.isVisible():
            ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
            self._customize_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        # Camera preview — bottom-right corner of the center/HUD area
        pw = _CameraPreview._W
        ph = self._cam_preview.height() or _CameraPreview._H
        self._cam_preview.setGeometry(
            cw.width() - pw - 16,
            cw.height() - ph - 28,
            pw, ph,
        )
        # Clipboard panel — bottom-center
        if hasattr(self, '_clipboard_panel') and self._clipboard_panel.isVisible():
            self.PositionClipboardPanel()
        # Settings window stays centred
        st = getattr(self, "_settings_overlay", None)
        if st is not None and st.isVisible():
            self.PlaceSettings(st)
        if hasattr(self, "_right_panel") and self._right_panel.isVisible():
            anim = getattr(self, "_history_anim", None)
            if anim is None or anim.state() != QPropertyAnimation.State.Running:
                self._right_panel.setGeometry(self.HistoryRect(True))
                self.SyncHudInset()
        self.FitVideo()

    def UpdateMetrics(self):
        """Feed the ring gauges in the top bar."""
        try:
            snap = _metrics.Snapshot()
        except Exception:
            return
        self._ring_cpu.SetValue(snap["cpu"], f"{snap['cpu']:.0f}")
        self._ring_mem.SetValue(snap["mem"], f"{snap['mem']:.0f}")
        g = snap["gpu"]
        self._ring_gpu.SetValue(max(0, g), f"{g:.0f}" if g >= 0 else "–")
        net = snap["net"]
        self._net_lbl.setText(f"{net * 1024:.0f} KB/s" if net < 1 else f"{net:.1f} MB/s")

    def IconBtn(self, glyph: str, tip: str, checkable: bool = False,
                  size: int = 34, accent: str = "") -> QPushButton:
        """A round, quiet icon button: the one button style of the new layout."""
        b = QPushButton(glyph)
        b.setFixedSize(size, size)
        b.setFont(QFont(UI_FONT, 11))
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setToolTip(tip)
        b.setCheckable(checkable)
        col = accent or C.TEXT_MED
        b.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {col};
                border: 1px solid transparent; border-radius: {size // 2}px;
            }}
            QPushButton:hover {{ background: {GLASS_HOVER}; color: {C.WHITE};
                                 border: 1px solid {GLASS_BORDER}; }}
            QPushButton:checked {{ background: {C.PRI_GHO}; color: {C.PRI};
                                   border: 1px solid {C.PRI_DIM}; }}
        """)
        return b

    def BuildHeader(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(_TOP_H)
        w.setStyleSheet(f"background: {C.BG};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(24, 0, 18, 0)
        lay.setSpacing(6)

        self._title_lbl = QLabel(self._assistant_name.upper())
        tf = QFont(UI_FONT, 13, QFont.Weight.DemiBold)
        tf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 6)
        self._title_lbl.setFont(tf)
        self._title_lbl.setStyleSheet("color: #e9f5ff; background: transparent;")
        lay.addWidget(self._title_lbl)
        lay.addStretch()

        # System load as small rings; a click opens Settings › System.
        gauges = _ClickArea()
        gauges.setToolTip("System — click for details")
        gauges.clicked.connect(lambda: self.OpenSettings("system"))
        gl = QHBoxLayout(gauges)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.setSpacing(2)
        self._ring_cpu = RingGauge("CPU", 30)
        self._ring_mem = RingGauge("RAM", 30)
        self._ring_gpu = RingGauge("GPU", 30)
        for r in (self._ring_cpu, self._ring_mem, self._ring_gpu):
            gl.addWidget(r)
        net_col = QVBoxLayout()
        net_col.setSpacing(0)
        self._net_lbl = QLabel("–")
        self._net_lbl.setMinimumWidth(64)
        self._net_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._net_lbl.setFont(QFont(UI_FONT, 9, QFont.Weight.DemiBold))
        self._net_lbl.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        net_cap = QLabel("NET")
        ncf = QFont(UI_FONT, 7)
        ncf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1)
        net_cap.setFont(ncf)
        net_cap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        net_col.addStretch()
        net_col.addWidget(self._net_lbl, 0, Qt.AlignmentFlag.AlignHCenter)
        net_col.addWidget(net_cap, 0, Qt.AlignmentFlag.AlignHCenter)
        net_col.addStretch()
        gl.addSpacing(6)
        gl.addLayout(net_col)
        lay.addWidget(gauges)
        lay.addSpacing(22)

        clock_col = QVBoxLayout()
        clock_col.setSpacing(0)
        self._clock_lbl = QLabel("00:00")
        self._clock_lbl.setFont(QFont(UI_FONT, 16, QFont.Weight.Light))
        self._clock_lbl.setStyleSheet("color: #dfe9f2; background: transparent;")
        self._clock_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        clock_col.addWidget(self._clock_lbl)
        self._date_lbl = QLabel("")
        self._date_lbl.setFont(QFont(UI_FONT, 8))
        self._date_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        clock_col.addWidget(self._date_lbl)
        lay.addLayout(clock_col)
        lay.addSpacing(16)

        self._history_btn = self.IconBtn("☰", "Conversation and files", checkable=True)
        self._history_btn.clicked.connect(self.ToggleHistory)
        lay.addWidget(self._history_btn)

        self._settings_btn = self.IconBtn("⚙", "Settings")
        self._settings_btn.clicked.connect(lambda: self.OpenSettings())
        lay.addWidget(self._settings_btn)
        return w

    def TickClock(self):
        self._clock_lbl.setText(time.strftime("%H:%M"))
        self._date_lbl.setText(time.strftime("%a %d %b %Y"))

    def BuildRightPanel(self) -> QWidget:
        """The full conversation and the file drop, in a drawer that slides in
        from the right. The reactor keeps the window the rest of the time."""
        w = QWidget(self.centralWidget())
        w.setObjectName("HistoryDrawer")
        w.setFixedWidth(_RIGHT_W)
        w.setStyleSheet(f"""
            QWidget#HistoryDrawer {{
                background: rgba(6, 11, 18, 0.95);
                border: 1px solid {GLASS_BORDER}; border-right: none;
                border-top-left-radius: 18px; border-bottom-left-radius: 18px;
            }}
        """)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 12, 14, 14)
        lay.setSpacing(8)

        def Sec(txt):
            l = QLabel(txt)
            sf = QFont(UI_FONT, 8, QFont.Weight.DemiBold)
            sf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3)
            l.setFont(sf)
            l.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            return l

        top = QHBoxLayout()
        title = QLabel("Conversation")
        title.setFont(QFont(UI_FONT, 12, QFont.Weight.DemiBold))
        title.setStyleSheet("color: #e9f5ff; background: transparent;")
        top.addWidget(title)
        top.addStretch()
        close = self.IconBtn("✕", "Close", size=28)
        close.clicked.connect(lambda: self.ToggleHistory(False))
        top.addWidget(close)
        lay.addLayout(top)

        self._log = LogWidget()
        lay.addWidget(self._log, stretch=1)

        lay.addWidget(Sec("FILES"))
        self._drop_zone = FileDropZone()
        self._drop_zone.file_selected.connect(self.OnFileSelected)
        lay.addWidget(self._drop_zone)

        self._file_hint = QLabel("No file yet — drop one here, or use + below")
        self._file_hint.setFont(QFont(UI_FONT, 8))
        self._file_hint.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._file_hint.setWordWrap(True)
        lay.addWidget(self._file_hint)

        w.hide()
        return w

    def BuildDock(self) -> QWidget:
        """The input dock: attach, type, send, microphone, interrupt — one pill."""
        w = QWidget()
        w.setFixedHeight(_DOCK_H)
        w.setStyleSheet(f"background: {C.BG};")
        outer = QHBoxLayout(w)
        outer.setContentsMargins(24, 8, 24, 18)
        outer.addStretch(1)

        bar = QWidget()
        bar.setObjectName("Dock")
        bar.setMinimumWidth(460)
        bar.setMaximumWidth(860)
        bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        bar.setFixedHeight(54)
        bar.setStyleSheet(f"""
            QWidget#Dock {{
                background: rgba(255, 255, 255, 0.045);
                border: 1px solid {GLASS_BORDER}; border-radius: 27px;
            }}
        """)
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 6, 8, 6)
        row.setSpacing(6)

        attach = self.IconBtn("+", "Attach a file")
        attach.clicked.connect(self._drop_zone.Browse)
        row.addWidget(attach)

        self._input = QLineEdit()
        self._input.setPlaceholderText(f"Ask {self._assistant_name.title()} anything…")
        self._input.setFont(QFont(UI_FONT, 11))
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: transparent; color: {C.WHITE};
                border: none; padding: 4px 6px;
                selection-background-color: {C.PRI_DIM};
            }}
        """)
        self._input.returnPressed.connect(self.Send)
        row.addWidget(self._input, stretch=1)

        send = self.IconBtn("➤", "Send  [Enter]", size=38, accent=C.PRI)
        send.clicked.connect(self.Send)
        row.addWidget(send)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFixedHeight(24)
        sep.setStyleSheet(f"color: {GLASS_BORDER}; background: {GLASS_BORDER}; max-width: 1px;")
        row.addWidget(sep)

        self._mute_btn = QPushButton()
        self._mute_btn.setFixedSize(38, 38)
        self._mute_btn.setFont(QFont(UI_FONT, 12))
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.clicked.connect(self.ToggleMute)
        self.StyleMuteBtn()
        row.addWidget(self._mute_btn)

        self._interrupt_btn = QPushButton("■")
        self._interrupt_btn.setFixedSize(38, 38)
        self._interrupt_btn.setFont(QFont(UI_FONT, 12))
        self._interrupt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._interrupt_btn.setToolTip("Stop talking  [Esc]")
        self._interrupt_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 107, 129, 0.10); color: {C.MUTED_C};
                border: 1px solid rgba(255, 107, 129, 0.35); border-radius: 19px;
            }}
            QPushButton:hover {{ background: rgba(255, 107, 129, 0.22); }}
            QPushButton:pressed {{ background: rgba(255, 107, 129, 0.32); }}
        """)
        self._interrupt_btn.clicked.connect(self.DoInterrupt)
        row.addWidget(self._interrupt_btn)

        outer.addWidget(bar, stretch=6)
        outer.addStretch(1)
        return w

    def HistoryRect(self, open_: bool) -> QRect:
        cw = self.centralWidget()
        h = max(200, cw.height() - _TOP_H - _DOCK_H)
        x = cw.width() - (_RIGHT_W if open_ else 0)
        return QRect(x, _TOP_H, _RIGHT_W, h)

    def ToggleHistory(self, checked: bool) -> None:
        """Slide the conversation drawer in or out (a short eased glide)."""
        d = self._right_panel
        if hasattr(self, "_history_btn"):
            self._history_btn.setChecked(bool(checked))
        anim = getattr(self, "_history_anim", None)
        if anim is not None:
            anim.stop()
        start = d.geometry() if d.isVisible() else self.HistoryRect(False)
        end = self.HistoryRect(bool(checked))
        d.setGeometry(start)
        d.show()
        d.raise_()
        anim = QPropertyAnimation(d, b"geometry", self)
        anim.setDuration(240)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(start)
        anim.setEndValue(end)
        if not checked:
            anim.finished.connect(d.hide)
        anim.valueChanged.connect(lambda _v: self.SyncHudInset())
        anim.finished.connect(self.SyncHudInset)
        anim.start()
        self._history_anim = anim

    def showEvent(self, event):
        super().showEvent(event)
        if not self._history_placed:
            self._history_placed = True
            d = self._right_panel
            d.setGeometry(self.HistoryRect(True))
            d.show()
            d.raise_()
            self.SyncHudInset()

    def SyncHudInset(self) -> None:
        """Tell the reactor canvas how much of it the drawer covers."""
        d = getattr(self, "_right_panel", None)
        hud = getattr(self, "hud", None)
        if d is None or hud is None:
            return
        cover = 0
        if d.isVisible():
            left = hud.mapTo(self.centralWidget(), QPoint(0, 0)).x()
            cover = max(0, min(hud.width(), left + hud.width() - d.x()))
        if cover != hud.right_inset:
            hud.right_inset = cover
            hud.update()

    def OnLogCaption(self, text: str) -> None:
        """The last exchange, shown softly under the reactor."""
        t = (text or "").strip()
        low = t.lower()
        name = (self._assistant_name or "ultron").lower()
        try:
            if low.startswith("you:"):
                self.hud.SetCaption("you", t[4:].strip())
            elif low.startswith(name + ":"):
                self.hud.SetCaption("ai", t[len(name) + 1:].strip())
            elif low.startswith("ultron:"):
                self.hud.SetCaption("ai", t[7:].strip())
        except Exception:
            pass

    def BuildInputRow(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(5)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a command or question…")
        self._input.setFont(QFont(UI_FONT, 10))
        self._input.setFixedHeight(30)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: #000d14; color: {C.WHITE};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 3px 7px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        self._input.returnPressed.connect(self.Send)
        row.addWidget(self._input)

        send = QPushButton("▸")
        send.setFixedSize(30, 30)
        send.setFont(QFont(UI_FONT, 11, QFont.Weight.Bold))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        send.clicked.connect(self.Send)
        row.addWidget(send)
        return row

    def BuildContentPanel(self) -> QWidget:
        """
        Collapsible panel below the HUD — shows search results, news, briefings.
        Hidden by default; appears when show_content() is called.
        """
        w = QWidget()
        w.setObjectName("ContentPanel")
        w.setStyleSheet(f"""
            QWidget#ContentPanel {{
                background: {C.PANEL};
                border-top: 1px solid {C.BORDER_B};
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 7, 12, 8)
        lay.setSpacing(5)

        # ── header row ───────────────────────────────────────────────────────
        hdr = QHBoxLayout(); hdr.setSpacing(6)

        dot = QLabel("◈")
        dot.setFont(QFont(UI_FONT, 10, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(dot)

        self._content_title_lbl = QLabel("BRIEFING")
        self._content_title_lbl.setFont(QFont(UI_FONT, 9, QFont.Weight.Bold))
        self._content_title_lbl.setStyleSheet(
            f"color: {C.PRI}; background: transparent; letter-spacing: 1px;"
        )
        hdr.addWidget(self._content_title_lbl)
        hdr.addStretch()

        self._content_ts_lbl = QLabel("")
        self._content_ts_lbl.setFont(QFont(UI_FONT, 8))
        self._content_ts_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._content_ts_lbl)

        dismiss = QPushButton("DISMISS  ✕")
        dismiss.setFont(QFont(UI_FONT, 8))
        dismiss.setFixedHeight(18)
        dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        dismiss.clicked.connect(w.hide)
        hdr.addWidget(dismiss)
        lay.addLayout(hdr)

        # ── separator ─────────────────────────────────────────────────────────
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); lay.addWidget(sep)

        # ── text display ──────────────────────────────────────────────────────
        self._content_display = QTextEdit()
        self._content_display.setReadOnly(True)
        self._content_display.setFont(QFont(UI_FONT, 9))
        self._content_display.setMinimumHeight(60)
        self._content_display.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._content_display.setStyleSheet(f"""
            QTextEdit {{
                background: {C.DARK};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 3px;
                padding: 6px 8px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG}; width: 6px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B}; border-radius: 3px; min-height: 16px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; border: none;
            }}
        """)
        lay.addWidget(self._content_display)

        return w

    def _ShowContent(self, title: str, text: str):
        """Slot — runs on Qt main thread. Updates and shows the content panel."""
        import time as _time
        self._content_title_lbl.setText(title.upper()[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setPlainText(text)
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start
        )
        first_show = not self._content_panel.isVisible()
        self._content_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 220, 120), 220])

    # ── document review ──────────────────────────────────────────────────────
    # Rendered as rich text into the content panel that already exists, rather
    # than into a panel of its own. A review is read, not clicked, so QTextEdit
    # gives scrolling, selection and copy for nothing, and the HUD gains no
    # widget it has to lay out. Severity decides colour and order here because
    # that is presentation; the plugin supplies no styling and knows no palette,
    # which is also what lets a re-theme repaint a review correctly.

    # Severity is marked by a symbol and a colour, not by a word. The findings
    # themselves are in the user's language, and "[SERIOUS]" sitting inside a
    # Turkish sentence is the kind of seam this project tries not to have —
    # while translating the tag would mean a table per language, which is worse.
    # A shape carries it in every language, and shape plus colour still reads
    # for someone who cannot separate red from amber. What the marks mean
    # arrives the way everything else does: ULTRON says it out loud.
    _REVIEW_MARKS = {"serious": ("RED", "▲"), "caution": ("ACC2", "●"), "note": ("PRI_DIM", "·")}

    @staticmethod
    def Esc(s) -> str:
        return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace("\n", "<br>"))

    def _ShowReview(self, title: str, summary: str, findings, unclear):
        """Slot — Qt main thread. Lays a document review into the content panel."""
        e = self.Esc
        parts = [f'<div style="color:{C.TEXT}; font-family:Segoe UI;">']

        if summary:
            parts.append(
                f'<div style="color:{C.WHITE}; border-left:2px solid {C.PRI};'
                f' padding-left:8px; margin-bottom:10px;">{e(summary)}</div>')

        for f in (findings or []):
            key, mark = self._REVIEW_MARKS.get(f.get("severity"), ("PRI_DIM", "·"))
            colour = getattr(C, key)
            parts.append(f'<div style="margin-bottom:11px;">')
            parts.append(
                f'<span style="color:{colour}; font-weight:bold;">{mark}</span> '
                f'<span style="color:{C.WHITE}; font-weight:bold;">'
                f'{e(f.get("heading"))}</span>')
            if f.get("detail"):
                parts.append(f'<div style="margin-left:12px;">{e(f["detail"])}</div>')
            if f.get("quote"):
                # The document's own wording, visually separated from the
                # explanation so the two are never mistaken for each other.
                parts.append(
                    f'<div style="margin-left:12px; color:{C.TEXT_DIM};'
                    f' border-left:1px solid {C.BORDER}; padding-left:7px;">'
                    f'&ldquo;{e(f["quote"])}&rdquo;</div>')
            if f.get("suggestion"):
                parts.append(
                    f'<div style="margin-left:12px; color:{C.PRI};">'
                    f'&rarr; {e(f["suggestion"])}</div>')
            parts.append('</div>')

        if unclear:
            parts.append(
                f'<div style="margin-top:6px; border-top:1px solid {C.BORDER};'
                f' padding-top:7px; color:{C.TEXT_MED};">'
                'The document does not settle:</div>')
            for u in unclear:
                parts.append(
                    f'<div style="margin-left:12px; color:{C.TEXT_MED};">'
                    f'&middot; {e(u)}</div>')
        parts.append('</div>')

        import time as _time
        # Left as written, not upper-cased. The other content-panel titles are
        # the app's own English labels, but this one is the document's name in
        # the user's language, and str.upper() applies English casing rules to
        # it: Turkish "Sözleşmesi" comes back "SÖZLEŞMESI", having lost the
        # dotted capital İ. Python has no locale-aware upper to reach for, and
        # imposing one language's rules on all of them is the bug, not the fix.
        self._content_title_lbl.setText((title or "Document")[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setHtml("".join(parts))
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start)
        first_show = not self._content_panel.isVisible()
        self._content_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 260, 120), 260, 0])

    # ── quiz panel ───────────────────────────────────────────────────────────
    # An interactive twin of the content panel. The plugin only ever hands over
    # questions; everything about asking, marking and reporting happens here,
    # and the finished result is pushed back into the conversation the same way
    # a dropped file is — as a message ULTRON reads and responds to. That keeps
    # the tool call short (it returns the moment the board is up) and leaves the
    # talking to the assistant, in the user's own language.

    def QuizBtn(self, text: str, primary: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setFont(QFont(UI_FONT, 9))
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setMinimumHeight(24)
        edge = C.BORDER_B if primary else C.BORDER
        col = C.PRI if primary else C.TEXT_MED
        b.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL2}; color: {col};
                border: 1px solid {edge}; border-radius: 2px;
                padding: 3px 9px; text-align: left;
            }}
            QPushButton:hover {{ color: {C.WHITE}; border-color: {C.PRI_DIM}; }}
            QPushButton:disabled {{ color: {C.TEXT_DIM}; border-color: {C.BORDER}; }}
        """)
        return b

    def BuildQuizPanel(self) -> QWidget:
        w = QWidget()
        w.setObjectName("QuizPanel")
        w.setStyleSheet(f"""
            QWidget#QuizPanel {{
                background: {C.PANEL};
                border-top: 1px solid {C.BORDER_B};
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 7, 12, 8)
        lay.setSpacing(6)

        hdr = QHBoxLayout(); hdr.setSpacing(6)
        dot = QLabel("◈")
        dot.setFont(QFont(UI_FONT, 10, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(dot)

        self._quiz_title_lbl = QLabel("QUIZ")
        self._quiz_title_lbl.setFont(QFont(UI_FONT, 9, QFont.Weight.Bold))
        self._quiz_title_lbl.setStyleSheet(
            f"color: {C.PRI}; background: transparent; letter-spacing: 1px;")
        hdr.addWidget(self._quiz_title_lbl)
        hdr.addStretch()

        self._quiz_count_lbl = QLabel("")
        self._quiz_count_lbl.setFont(QFont(UI_FONT, 8))
        self._quiz_count_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._quiz_count_lbl)

        quit_btn = QPushButton("DISMISS  ✕")
        quit_btn.setFont(QFont(UI_FONT, 8))
        quit_btn.setFixedHeight(18)
        quit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        quit_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        quit_btn.clicked.connect(self._HideQuiz)
        hdr.addWidget(quit_btn)
        lay.addLayout(hdr)

        rule = QFrame(); rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {C.BORDER};")
        lay.addWidget(rule)

        self._quiz_q_lbl = QLabel("")
        self._quiz_q_lbl.setWordWrap(True)
        self._quiz_q_lbl.setFont(QFont(UI_FONT, 10))
        self._quiz_q_lbl.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        lay.addWidget(self._quiz_q_lbl)

        self._quiz_answers = QWidget()
        self._quiz_answers.setStyleSheet("background: transparent;")
        self._quiz_answers_lay = QVBoxLayout(self._quiz_answers)
        self._quiz_answers_lay.setContentsMargins(0, 2, 0, 0)
        self._quiz_answers_lay.setSpacing(4)
        lay.addWidget(self._quiz_answers)

        self._quiz_note_lbl = QLabel("")
        self._quiz_note_lbl.setWordWrap(True)
        self._quiz_note_lbl.setFont(QFont(UI_FONT, 9))
        self._quiz_note_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._quiz_note_lbl.hide()
        lay.addWidget(self._quiz_note_lbl)

        foot = QHBoxLayout()
        foot.addStretch()
        self._quiz_next_btn = self.QuizBtn("NEXT  →", primary=True)
        self._quiz_next_btn.setFixedWidth(110)
        self._quiz_next_btn.clicked.connect(self.QuizNext)
        self._quiz_next_btn.hide()
        foot.addWidget(self._quiz_next_btn)
        lay.addLayout(foot)

        self._quiz = None
        return w

    def _ShowQuiz(self, topic: str, questions, grader=None):
        """Slot — Qt main thread. Puts a fresh quiz on the board."""
        if not questions:
            return
        self._quiz = {
            "topic": topic or "",
            "questions": list(questions),
            "grader": grader,
            "i": 0,
            "results": [],
            "answered": False,
        }
        self._quiz_title_lbl.setText((topic or "quiz").upper()[:48])
        first_show = not self._quiz_panel.isVisible()
        self._quiz_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 250, 120), 0, 250])
        self.QuizRender()

    def _HideQuiz(self):
        self._quiz = None
        self._quiz_panel.hide()

    def QuizClearAnswers(self):
        while self._quiz_answers_lay.count():
            item = self._quiz_answers_lay.takeAt(0)
            child = item.widget()
            if child is not None:
                child.setParent(None)
                child.deleteLater()

    def QuizRender(self):
        q = self._quiz["questions"][self._quiz["i"]]
        n, total = self._quiz["i"] + 1, len(self._quiz["questions"])
        self._quiz_count_lbl.setText(f"{n} / {total}")
        self._quiz_q_lbl.setText(q.get("question", ""))
        self._quiz_note_lbl.hide()
        self._quiz_next_btn.hide()
        self._quiz["answered"] = False
        self.QuizClearAnswers()

        opts = q.get("options") or []
        if opts:
            for text in opts:
                b = self.QuizBtn("   " + text)
                b.clicked.connect(lambda _=False, t=text: self.QuizSubmit(t))
                self._quiz_answers_lay.addWidget(b)
        else:
            row = QWidget(); row.setStyleSheet("background: transparent;")
            h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)
            field = QLineEdit()
            field.setFont(QFont(UI_FONT, 10))
            field.setPlaceholderText("your answer")
            field.setStyleSheet(f"""
                QLineEdit {{
                    background: {C.PANEL2}; color: {C.WHITE};
                    border: 1px solid {C.BORDER}; border-radius: 2px; padding: 4px 7px;
                }}
                QLineEdit:focus {{ border-color: {C.PRI_DIM}; }}
            """)
            send = self.QuizBtn("ANSWER", primary=True)
            send.setFixedWidth(90)
            field.returnPressed.connect(lambda: self.QuizSubmit(field.text()))
            send.clicked.connect(lambda: self.QuizSubmit(field.text()))
            h.addWidget(field, stretch=1)
            h.addWidget(send)
            self._quiz_answers_lay.addWidget(row)
            field.setFocus()

    def QuizSubmit(self, given: str):
        if self._quiz is None or self._quiz["answered"]:
            return
        self._quiz["answered"] = True
        q = self._quiz["questions"][self._quiz["i"]]
        grader = self._quiz.get("grader")
        verdict = None
        if callable(grader):
            try:
                verdict = grader(q, given)
            except Exception:
                verdict = None
        self._quiz["results"].append({
            "question": q.get("question", ""),
            "type": q.get("type", ""),
            "given": str(given or "").strip(),
            "answer": q.get("answer", ""),
            "correct": verdict,
        })

        for i in range(self._quiz_answers_lay.count()):
            wdg = self._quiz_answers_lay.itemAt(i).widget()
            if wdg is not None:
                wdg.setEnabled(False)

        if verdict is True:
            mark, colour = "✓  correct", C.GREEN
        elif verdict is False:
            mark, colour = "✕  " + str(q.get("answer", "")), C.RED
        else:
            # Open answers and near-miss gap-fills are ULTRON's to judge. Saying
            # so is honest; marking it wrong here would be a guess.
            mark, colour = "…  noted — I'll go over this one with you", C.ACC2
        note = q.get("note") or ""
        self._quiz_note_lbl.setText(mark + (("\n" + note) if note else ""))
        self._quiz_note_lbl.setStyleSheet(f"color: {colour}; background: transparent;")
        self._quiz_note_lbl.show()

        last = self._quiz["i"] >= len(self._quiz["questions"]) - 1
        self._quiz_next_btn.setText("FINISH  →" if last else "NEXT  →")
        self._quiz_next_btn.show()
        self._quiz_next_btn.setFocus()

    def QuizNext(self):
        if self._quiz is None:
            return
        if self._quiz["i"] >= len(self._quiz["questions"]) - 1:
            self.QuizFinish()
        else:
            self._quiz["i"] += 1
            self.QuizRender()

    def QuizFinish(self):
        if self._quiz is None:
            return
        topic = self._quiz["topic"]
        results = self._quiz["results"]
        right = sum(1 for r in results if r["correct"] is True)
        unsure = sum(1 for r in results if r["correct"] is None)
        total = len(results)
        self._quiz_panel.hide()
        self._quiz = None

        self._log.AppendLog(f"QUIZ: {topic or 'quiz'} — {right}/{total} correct")

        # Hand it back to ULTRON as a message, not as a tool return: the tool
        # call ended minutes ago. This is the same channel a dropped file uses.
        lines = [f"[QUIZ_DONE] topic={topic or 'general'} | "
                 f"auto-marked {right}/{total} correct"
                 + (f", {unsure} still need your marking" if unsure else "")]
        for i, r in enumerate(results, 1):
            state = ("correct" if r["correct"] is True
                     else "wrong" if r["correct"] is False else "NEEDS MARKING")
            lines.append(
                f"{i}. [{r['type']}] {r['question']} | they answered: "
                f"{r['given'] or '(blank)'} | expected: {r['answer']} | {state}")
        lines.append(
            "Mark every question flagged NEEDS MARKING yourself — accept an answer "
            "that means the same thing. Then tell them how they did in their own "
            "language: the score, what they got wrong and why, in a couple of "
            "sentences. Offer another round only if it fits. "
            "Remember something only if it would still matter next week — that they "
            "are working through a subject, or keep missing the same thing. A score "
            "from one session is not worth a memory, and a memory per quiz would "
            "bury the things that are.")
        msg = "\n".join(lines)
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def BuildFooter(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(22)
        w.setStyleSheet(f"background: {C.DARK}; border-top: 1px solid {C.BORDER};")
        lay = QHBoxLayout(w); lay.setContentsMargins(14, 0, 14, 0)

        def Fl(txt, color=C.TEXT_MED):
            l = QLabel(txt); l.setFont(QFont(UI_FONT, 8))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        lay.addWidget(Fl("[F4] Mute  ·  [F11] Fullscreen"))
        lay.addStretch()
        lay.addWidget(Fl("By Abhinav Viswam", C.PRI_DIM))
        return w

    def OnFileSelected(self, path: str):
        self._current_file = path
        p    = Path(path)
        cat  = FileCategory(p)
        icon, _ = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size = FmtSize(p.stat().st_size)
        self._file_hint.setText(f"{icon}  {p.name}  ·  {size}  ·  Tell {self._assistant_name} what to do with it")
        self._log.AppendLog(f"FILE: {p.name} ({size}) loaded")
        if self.on_text_command:
            msg = (
                f"[FILE_UPLOADED] path={path} | name={p.name} | "
                f"type={p.suffix.lstrip('.')} | size={size} | "
                f"Briefly tell the user you can see the file '{p.name}' "
                f"({size}) has been uploaded and ask what they'd like to do with it."
            )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    # ── Auto-start ──────────────────────────────────────────────────────────────

    def CheckAutostart(self) -> bool:
        """Returns True if auto-start is currently registered on this OS."""
        try:
            if _OS == "Windows":
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
                try:
                    winreg.QueryValueEx(key, "ULTRON_AI")
                    return True
                except FileNotFoundError:
                    return False
                finally:
                    winreg.CloseKey(key)
            elif _OS == "Darwin":
                return (Path.home() / "Library" / "LaunchAgents"
                        / "com.ultron.assistant.plist").exists()
            else:
                return (Path.home() / ".config" / "autostart" / "ultron.desktop").exists()
        except Exception:
            return False

    def ToggleAutostart(self):
        currently_on = self.CheckAutostart()
        try:
            script = str(Path(__file__).resolve().parent.parent / "Main.py")
            if _OS == "Windows":
                import winreg
                reg = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS)
                if currently_on:
                    winreg.DeleteValue(reg, "ULTRON_AI")
                else:
                    pythonw = Path(sys.executable).parent / "pythonw.exe"
                    exe = str(pythonw if pythonw.exists() else sys.executable)
                    winreg.SetValueEx(reg, "ULTRON_AI", 0, winreg.REG_SZ,
                                      f'"{exe}" "{script}"')
                winreg.CloseKey(reg)
            elif _OS == "Darwin":
                plist_dir = Path.home() / "Library" / "LaunchAgents"
                plist_dir.mkdir(parents=True, exist_ok=True)
                plist = plist_dir / "com.ultron.assistant.plist"
                if currently_on:
                    plist.unlink(missing_ok=True)
                else:
                    plist.write_text(
                        '<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                        '<plist version="1.0"><dict>\n'
                        '  <key>Label</key><string>com.ultron.assistant</string>\n'
                        '  <key>ProgramArguments</key><array>\n'
                        f'    <string>{sys.executable}</string>\n'
                        f'    <string>{script}</string>\n'
                        '  </array>\n'
                        '  <key>RunAtLoad</key><true/>\n'
                        '</dict></plist>\n'
                    )
            else:
                desk_dir = Path.home() / ".config" / "autostart"
                desk_dir.mkdir(parents=True, exist_ok=True)
                desk = desk_dir / "ultron.desktop"
                if currently_on:
                    desk.unlink(missing_ok=True)
                else:
                    desk.write_text(
                        "[Desktop Entry]\n"
                        f"Name={self._assistant_name}\n"
                        f"Exec={sys.executable} {script}\n"
                        "Type=Application\nTerminal=false\n"
                        "X-GNOME-Autostart-enabled=true\n"
                    )
            enabled = not currently_on
            self._log.AppendLog(
                f"SYS: Auto-start {'enabled' if enabled else 'disabled'}.")
        except Exception as e:
            self._log.AppendLog(f"ERR: Auto-start failed — {e}")

    def ToggleBrief(self):
        from Brain.Settings import GetBriefEnabled, SaveBriefEnabled
        new_val = not GetBriefEnabled()
        SaveBriefEnabled(new_val)

    def TogglePtt(self):
        from Brain.Settings import GetPushToTalkEnabled, SavePushToTalkEnabled
        want = not GetPushToTalkEnabled()
        SavePushToTalkEnabled(want)
        scope = None
        if self.on_push_to_talk:
            try:
                scope = self.on_push_to_talk(want)
            except Exception as e:
                self._log.AppendLog(f"ERR: Push-to-talk failed — {e}")
                SavePushToTalkEnabled(False)
                want = False
        self.ApplyPttShortcut(want and scope != "global")

    def ApplyPttShortcut(self, needed: bool):
        """Bind the chord inside the window when no global hook is available.

        On macOS and Linux there is no dependency-free way to read global key
        state, so the chord is at least live whenever this window has focus.
        Qt gives no key-release for a QShortcut, so a press latches the mic open
        and a short timer closes it; held down, auto-repeat keeps pushing that
        timer out, which behaves like holding a key.
        """
        from PyQt6.QtGui import QKeySequence, QShortcut
        from Interface.Hotkey import QtSequence

        if not needed:
            sc = getattr(self, "_ptt_sc", None)
            if sc is not None:
                sc.setEnabled(False)
                self._ptt_sc = None
            self.PttHold(False)
            return
        if getattr(self, "_ptt_sc", None) is not None:
            return

        self._ptt_release = QTimer(self)
        self._ptt_release.setSingleShot(True)
        self._ptt_release.setInterval(420)
        self._ptt_release.timeout.connect(lambda: self.PttHold(False))

        def _Press():
            self.PttHold(True)
            self._ptt_release.start()

        self._ptt_sc = QShortcut(QKeySequence(QtSequence()), self)
        self._ptt_sc.setAutoRepeat(True)
        self._ptt_sc.activated.connect(_Press)

    def PttHold(self, held: bool):
        """Report a windowed press/release to whoever owns the microphone."""
        cb = getattr(self, "ptt_hold", None)
        if cb:
            try:
                cb(bool(held))
            except Exception:
                pass

    # ── Customization ────────────────────────────────────────────────────────────

    def OpenCustomize(self):
        cfg = ReadFullConfig()
        if self._customize_overlay:
            self._customize_overlay.hide()
        cw = self.centralWidget()
        ov = CustomizeOverlay(
            cfg.get("assistant_name", "ULTRON") or "ULTRON",
            cfg.get("user_name", ""),
            cfg.get("ui_color", "") or DEFAULT_UI_COLOR,
            cfg.get("voice_name", ""),
            parent=cw,
        )
        ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
        oh = min(oh, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.on_preview = self.PreviewUiColor
        ov.saved.connect(self.ApplyNameUpdate)
        ov.show()
        self._customize_overlay = ov

    def PreviewUiColor(self, hex_color: str):
        """Live preview — paints the whole interface the new colour (does NOT write to config)."""
        old = CurrentPalette()
        if ApplyUiAccent(hex_color):
            RethemeAllWidgets(old, CurrentPalette())

    def ApplyNameUpdate(self, name: str, user_name: str, ui_color: str = "",
                           voice: str = ""):
        """Update all name/theme-dependent UI elements and persist to config."""
        self._assistant_name = name.strip() or "ULTRON"
        display = self._assistant_name.upper()
        self.setWindowTitle(f"{display} — {APP_VERSION}")
        self._title_lbl.setText(display)
        self._log._ai_name_lc = self._assistant_name.lower()
        self.hud._assistant_name = display

        color_changed = False
        if ui_color:
            old = CurrentPalette()
            if ApplyUiAccent(ui_color):
                # Live-paint the whole interface (panels, buttons, borders, HUD)
                RethemeAllWidgets(old, CurrentPalette())
                color_changed = old["PRI"] != C.PRI

        # Voice change → persist and, if it actually changed, rebuild the Live
        # session so the new voice takes effect (it's fixed at connect time).
        voice_changed = False
        if voice:
            from Brain.Settings import GetVoice, SaveVoice
            if voice != GetVoice():
                SaveVoice(voice)
                voice_changed = True

        try:
            data = ReadFullConfig()
            data["assistant_name"] = self._assistant_name
            data["user_name"] = user_name.strip()
            if ui_color:
                data["ui_color"] = ui_color.strip().lower()
            API_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
            self._log.AppendLog(f"SYS: Identity updated — {display}")
            if color_changed:
                self._log.AppendLog(f"SYS: UI colour applied — {ui_color}")
            if voice_changed:
                self._log.AppendLog(f"SYS: Voice set — {voice}")
        except Exception as e:
            self._log.AppendLog(f"ERR: Config save failed — {e}")

        if voice_changed and self.on_voice_change:
            self.on_voice_change()

    def CentreOverlay(self, ov) -> None:
        """Place a floating overlay in the middle of the HUD and show it."""
        cw = self.centralWidget()
        ov.adjustSize()
        ov.setGeometry(
            max(0, (cw.width()  - ov.width())  // 2),
            max(0, (cw.height() - ov.height()) // 2),
            ov.width(), ov.height(),
        )
        ov.show()
        ov.raise_()

    # ── Settings window ──────────────────────────────────────────────────────

    def OpenSettings(self, section: str = "") -> None:
        old = getattr(self, "_settings_overlay", None)
        if old is not None and old.isVisible():
            if section:
                old.Show(section)
            old.raise_()
            return
        sections = [
            ("general", "◎", "General", self.BuildGeneralPage),
            ("brain", "✦", "Brain", self.BuildBrainPage),
            ("voice", "♪", "Voice & audio", lambda p: self._Hosted(AudioDeviceOverlay(parent=p), "picked", self.OnAudioDevicesApplied)),
            ("look", "◐", "Look & name", self.BuildLookPage),
            ("memory", "◉", "Memory", lambda p: MemoryOverlay(parent=p)),
            ("plugins", "⊞", "Plugins", self.BuildPluginsPage),
            ("usage", "▤", "Usage", lambda p: UsagePage(p)),
            ("system", "▣", "System", lambda p: SystemPage(p)),
            ("about", "ⓘ", "About", self.BuildAboutPage),
        ]
        ov = SettingsOverlay(sections, start=section, parent=self.centralWidget())
        ov.closed.connect(lambda: setattr(self, "_settings_overlay", None))
        self._settings_overlay = ov
        self.PlaceSettings(ov)
        ov.show()
        ov.raise_()
        ov.setFocus()

    def PlaceSettings(self, ov) -> None:
        cw = self.centralWidget()
        w = min(1040, cw.width() - 40)
        h = min(700, cw.height() - _TOP_H - 20)
        ov.setGeometry((cw.width() - w) // 2, _TOP_H + max(0, (cw.height() - _TOP_H - h) // 2 - 10), w, h)

    @staticmethod
    def _Hosted(panel, signal: str, slot):
        getattr(panel, signal).connect(slot)
        return panel

    def BuildGeneralPage(self, parent):
        from Brain.Settings import GetBriefEnabled, GetPushToTalkEnabled
        from Interface.Hotkey import ChordLabel
        page = QWidget(parent)
        page.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        card, c = _Card()
        c.addWidget(_Caption("Talking"))
        ptt = ToggleSwitch(GetPushToTalkEnabled())
        def _Ptt(on):
            if on != GetPushToTalkEnabled():
                self.TogglePtt()
            ptt.setChecked(GetPushToTalkEnabled())
        ptt.toggled.connect(_Ptt)
        c.addWidget(_SettingRow("Push-to-talk", f"Hold {ChordLabel()} to talk. The microphone stays "
                                "closed the rest of the time.", ptt))
        brief = ToggleSwitch(GetBriefEnabled())
        brief.toggled.connect(lambda on: self.ToggleBrief() if on != GetBriefEnabled() else None)
        c.addWidget(_SettingRow("Morning brief", "A short summary of your day when you first start "
                                "talking in the morning.", brief))
        lay.addWidget(card)

        card, c = _Card()
        c.addWidget(_Caption("Window"))
        fs = ToggleSwitch(self.isFullScreen())
        fs.toggled.connect(lambda on: self.ToggleFullscreen() if on != self.isFullScreen() else None)
        c.addWidget(_SettingRow("Full screen", "Also F11.", fs))
        lay.addWidget(card)

        card, c = _Card()
        c.addWidget(_Caption("This computer"))
        auto = ToggleSwitch(self.CheckAutostart())
        def _Auto(on):
            if on != self.CheckAutostart():
                self.ToggleAutostart()
            auto.setChecked(self.CheckAutostart())
        auto.toggled.connect(_Auto)
        c.addWidget(_SettingRow("Start with the computer",
                                f"Open {self._assistant_name} when you sign in.", auto))
        sc = _GhostButton("Create")
        sc.clicked.connect(self.CreateDesktopShortcut)
        c.addWidget(_SettingRow("Desktop shortcut", "An icon on your desktop to open it.", sc))
        lay.addWidget(card)
        return page

    def BuildBrainPage(self, parent):
        try:
            ov = ProvidersOverlay(parent=parent)
        except Exception as e:
            return _Text(f"The Brain settings could not open: {e}", 10, C.TEXT_MED, wrap=True)
        ov.changed.connect(self.OnProvidersApplied)
        return ov

    def BuildLookPage(self, parent):
        cfg = ReadFullConfig()
        ov = CustomizeOverlay(
            cfg.get("assistant_name", "ULTRON") or "ULTRON",
            cfg.get("user_name", ""),
            cfg.get("ui_color", "") or DEFAULT_UI_COLOR,
            cfg.get("voice_name", ""),
            parent=parent,
        )
        ov.on_preview = self.PreviewUiColor
        ov.saved.connect(self.ApplyNameUpdate)
        return ov

    def BuildPluginsPage(self, parent):
        plugins = self.get_plugins() if self.get_plugins else []
        sections = self.get_plugin_settings() if self.get_plugin_settings else []
        out = [PluginManagerOverlay(plugins, parent=parent)]
        if sections:
            out.append(PluginSettingsOverlay(sections, parent=parent))
        return out

    def BuildAboutPage(self, parent):
        page = QWidget(parent)
        page.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        card, c = _Card()
        c.addWidget(_Text(self._assistant_name.upper(), 20, C.WHITE, QFont.Weight.Light))
        c.addWidget(_Text(f"Version {APP_VERSION}", 9, C.TEXT_DIM))
        c.addWidget(_Text("Made by Abhinav Viswam", 10, C.TEXT))
        c.addWidget(_Text("A voice assistant for your computer. It talks with you in real time, "
                          "and can use your apps, files, the web and your screen.", 9,
                          C.TEXT_MED, wrap=True))
        lay.addWidget(card)
        card, c = _Card()
        c.addWidget(_Caption("Shortcuts"))
        for k, v in (("F4", "Mute or unmute"), ("Esc", "Stop talking / close a panel"),
                     ("F11", "Full screen")):
            row = QHBoxLayout()
            row.addWidget(_Text(k, 10, C.WHITE, QFont.Weight.DemiBold))
            row.addSpacing(20)
            row.addWidget(_Text(v, 10, C.TEXT_MED), 1)
            c.addLayout(row)
        lay.addWidget(card)
        return page

    # ── Audio devices ────────────────────────────────────────────────────────

    def OpenAudioDevices(self):
        ov = AudioDeviceOverlay(parent=self.centralWidget())
        ov.picked.connect(self.OnAudioDevicesApplied)
        self.CentreOverlay(ov)
        self._audio_overlay = ov            # keep a reference so it isn't GC'd

    def OnAudioDevicesApplied(self):
        self._log.AppendLog("SYS: Audio devices updated.")
        if self.on_audio_device_change:
            self.on_audio_device_change()

    # ── Engine & providers ───────────────────────────────────────────────────

    def OpenProviders(self):
        """Open the engine panel. Everything it needs is imported on this call.

        A failure here must not take the drawer with it — the folder can be
        deleted, which is the point of it being a folder, and the honest answer
        to that is a line in the log rather than a traceback.
        """
        try:
            ov = ProvidersOverlay(parent=self.centralWidget())
        except ImportError:
            self._log.AppendLog(
                "SYS: The Brain settings are not installed — "
                "the assistant runs on Gemini Live.")
            return
        except Exception as e:
            self._log.AppendLog(f"SYS: Provider settings unavailable: {e}")
            return
        ov.changed.connect(self.OnProvidersApplied)
        self.CentreOverlay(ov)
        self._providers_overlay = ov        # keep a reference so it isn't GC'd

    def OnProvidersApplied(self):
        """Rebuild the session so the new engine takes over.

        Reuses the audio-device callback rather than adding a second one: both
        mean 'the conversation has to be rebuilt', Main.py already keeps the
        transcript across such a rebuild through session resumption, and one
        path that is known to work is better than two that look alike."""
        self._log.AppendLog("SYS: Brain settings saved — reconnecting.")
        if self.on_audio_device_change:
            self.on_audio_device_change()

    # ── Memory panel ─────────────────────────────────────────────────────────

    def OpenMemoryPanel(self):
        ov = MemoryOverlay(parent=self.centralWidget())
        self.CentreOverlay(ov)
        self._memory_overlay = ov

    # ── Irreversible-action confirmation ─────────────────────────────────────

    def ShowConfirmBanner(self, title: str, detail: str):
        self.HideConfirmBanner()
        ov = ConfirmBanner(title, detail, parent=self.centralWidget())
        ov.answered.connect(self.OnConfirmAnswered)
        self.CentreOverlay(ov)
        self._confirm_overlay = ov

    def HideConfirmBanner(self):
        ov = getattr(self, "_confirm_overlay", None)
        if ov is not None:
            ov.hide()
            ov.deleteLater()
            self._confirm_overlay = None

    def OnConfirmAnswered(self, accepted: bool):
        # Tear the banner down first: core.confirm.resolve() may be about to
        # shut the machine down, and a live widget mid-callback is not where you
        # want to be when that happens.
        self.HideConfirmBanner()
        try:
            from Brain.Confirm import resolve
            resolve(bool(accepted))
        except Exception as e:
            self._log.AppendLog(f"ERR: Confirmation failed — {e}")

    def OpenPluginManager(self):
        plugins = self.get_plugins() if self.get_plugins else []
        cw = self.centralWidget()
        ov = PluginManagerOverlay(plugins, parent=cw)
        ov.adjustSize()
        ov.setGeometry(
            (cw.width()  - ov.width())  // 2,
            (cw.height() - ov.height()) // 2,
            ov.width(), ov.height(),
        )
        ov.show()
        ov.raise_()
        self._plugin_manager_overlay = ov   # keep a reference so it isn't GC'd

    def OpenPluginSettings(self):
        sections = self.get_plugin_settings() if self.get_plugin_settings else []
        cw = self.centralWidget()
        ov = PluginSettingsOverlay(sections, parent=cw)
        ow = PluginSettingsOverlay._OW
        oh = min(560, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.show()
        ov.raise_()
        self._plugin_settings_overlay = ov   # keep a reference so it isn't GC'd

    # ── Clipboard intelligence ───────────────────────────────────────────────────

    def OnClipboardChanged(self):
        try:
            text = QApplication.clipboard().text().strip()
            if len(text) >= 10:
                self._clipboard_sig.emit(text)
        except Exception:
            pass

    def ShowClipboardPanel(self, text: str):
        self._clipboard_panel.ShowClipboard(text)
        self.PositionClipboardPanel()

    def PositionClipboardPanel(self):
        cw = self.centralWidget()
        pw = ClipboardPanel._W
        ph = self._clipboard_panel.sizeHint().height() or ClipboardPanel._H
        x = (cw.width() - pw) // 2
        y = cw.height() - ph - 6
        self._clipboard_panel.setGeometry(x, y, pw, ph)
        self._clipboard_panel.raise_()

    def OnClipboardAction(self, cmd: str):
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(cmd,), daemon=True).start()

    # ────────────────────────────────────────────────────────────────────────────

    def DoInterrupt(self):
        if self.on_interrupt:
            self.on_interrupt()

    def ToggleMute(self):
        # A deliberate press settles the question: whatever the video did, or is
        # about to do, the user has now said what they want.
        self._video_auto_muted = False
        self.SetMuted(not self._muted)

    def SetMuted(self, muted: bool, note: str = ""):
        muted = bool(muted)
        if muted == self._muted:
            return
        self._muted = muted
        self.hud.muted = muted
        self.StyleMuteBtn()
        if muted:
            self.ApplyState("MUTED")
            self._log.AppendLog("SYS: Microphone muted." + (f" {note}" if note else ""))
        else:
            self.ApplyState("LISTENING")
            self._log.AppendLog("SYS: Microphone active." + (f" {note}" if note else ""))

    def StyleMuteBtn(self):
        if self._muted:
            self._mute_btn.setText("")
            self._mute_btn.setIcon(MicIcon(C.MUTED_C, True))
            self._mute_btn.setIconSize(QSize(20, 20))
            self._mute_btn.setToolTip("Microphone off — click to turn it on  [F4]")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(255, 107, 129, 0.14); color: {C.MUTED_C};
                    border: 1px solid rgba(255, 107, 129, 0.45); border-radius: 19px;
                }}
                QPushButton:hover {{ background: rgba(255, 107, 129, 0.24); }}
            """)
        else:
            self._mute_btn.setText("")
            self._mute_btn.setIcon(MicIcon(C.WHITE, False))
            self._mute_btn.setIconSize(QSize(20, 20))
            self._mute_btn.setToolTip("Listening — click to mute  [F4]")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(220, 239, 255, 0.10); color: {C.WHITE};
                    border: 1px solid rgba(220, 239, 255, 0.35); border-radius: 19px;
                }}
                QPushButton:hover {{ background: rgba(220, 239, 255, 0.20); }}
            """)

    def Send(self):
        txt = self._input.text().strip()
        if not txt: return
        self._input.clear()
        self._log.AppendLog(f"You: {txt}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,), daemon=True).start()

    def ApplyState(self, state: str):
        self.hud.state    = state
        self.hud.speaking = (state == "SPEAKING")

    def CheckConfig(self) -> bool:
        if not API_FILE.exists(): return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            return bool(d.get("gemini_api_key")) and bool(d.get("os_system"))
        except Exception:
            return False

    def ShowSetup(self):
        ov = SetupOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 460, 390
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.done.connect(self.OnSetupDone)
        ov.show()
        self._overlay = ov

    def OnSetupDone(self, key: str, os_name: str):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        API_FILE.write_text(
            json.dumps({"gemini_api_key": key, "os_system": os_name}, indent=4),
            encoding="utf-8",
        )
        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self.ApplyState("LISTENING")
        self._assistant_name = ReadFullConfig().get("assistant_name", "ULTRON") or "ULTRON"
        self._log.AppendLog(f"SYS: Initialised. OS={os_name.upper()}. {self._assistant_name} online.")


class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app
    def mainloop(self):
        self._app.exec()
    def protocol(self, *_):
        pass


class UltronUI:
    def __init__(self, size=None):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        self._win = MainWindow()
        self.root = _RootShim(self._app)
        self._win.show()

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        if v != self._win._muted:
            self._win.ToggleMute()

    @property
    def CurrentFile(self) -> str | None:
        return self._win._drop_zone.CurrentFile()

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    @property
    def on_interrupt(self):
        return self._win.on_interrupt

    @on_interrupt.setter
    def on_interrupt(self, cb):
        self._win.on_interrupt = cb

    @property
    def on_voice_change(self):
        return self._win.on_voice_change

    @on_voice_change.setter
    def on_voice_change(self, cb):
        self._win.on_voice_change = cb

    @property
    def on_audio_device_change(self):
        return self._win.on_audio_device_change

    @on_audio_device_change.setter
    def on_audio_device_change(self, cb):
        self._win.on_audio_device_change = cb

    def ShowConfirm(self, title: str, detail: str) -> None:
        """Thread-safe: raise the irreversible-action gate. Called from action
        handlers running in executor threads, so it goes through a signal."""
        self._win._confirm_sig.emit(str(title)[:120], str(detail)[:300])

    def HideConfirm(self) -> None:
        """Thread-safe: take the gate down."""
        self._win._confirm_hide_sig.emit()

    @property
    def get_plugins(self):
        return self._win.get_plugins

    @get_plugins.setter
    def get_plugins(self, cb):
        self._win.get_plugins = cb

    @property
    def get_plugin_settings(self):
        return self._win.get_plugin_settings

    @get_plugin_settings.setter
    def get_plugin_settings(self, cb):
        self._win.get_plugin_settings = cb

    def SetAudioLevel(self, level: float) -> None:
        """Thread-safe: feed a 0.0–1.0 live audio level to the HUD waveform.
        Called from the audio threads; a plain float store is atomic under the
        GIL, so no signal/lock is needed for this cosmetic value."""
        try:
            self._win.hud.SetAudioLevel(level)
        except Exception:
            pass

    @property
    def ptt_hold(self):
        return self._win.ptt_hold

    @ptt_hold.setter
    def ptt_hold(self, cb):
        self._win.ptt_hold = cb

    @property
    def on_push_to_talk(self):
        return self._win.on_push_to_talk

    @on_push_to_talk.setter
    def on_push_to_talk(self, cb):
        self._win.on_push_to_talk = cb

    def PushVisemes(self, frames, hop: float, at: float) -> None:
        """Thread-safe: post a schedule of 20 ms voice-level frames for
        ULTRON's own speech, so the reactor moves in step with the audio. `at` is the wall-clock time the batch begins to
        sound, not the time of the call. See HudCanvas.push_visemes()."""
        try:
            self._win.hud.PushVisemes(frames, hop, at)
        except Exception:
            pass

    def SetState(self, state: str):
        self._win._state_sig.emit(state)

    def WriteLog(self, text: str):
        self._win._log_sig.emit(text)

    def WaitForApiKey(self):
        while not self._win._ready:
            time.sleep(0.1)

    def ShowContent(self, title: str, text: str):
        """Thread-safe: display content in the panel below the HUD."""
        self._win._content_sig.emit(title[:48], text[:4000])

    def ShowQuiz(self, topic: str, questions, grade=None) -> None:
        """Thread-safe: put an interactive quiz on the board.

        `grade(question, given)` decides each answer — the plugin supplies it so
        the marking rules live with the questions rather than being duplicated
        here. Returning None from it means "ULTRON should judge this one", which
        is how open answers and near-miss gap-fills are handled.

        Returns immediately: the user answers at their own pace and the finished
        result is delivered back through on_text_command.
        """
        self._win._quiz_sig.emit(str(topic or ""), list(questions or []), grade)

    def HideQuiz(self) -> None:
        """Thread-safe: clear any quiz currently on the board."""
        self._win._quiz_hide_sig.emit()

    def ShowReview(self, title: str, summary: str, findings, unclear=None) -> None:
        """Thread-safe: lay a document review into the panel below the HUD.

        `findings` is a list of {heading, detail, severity, quote, suggestion};
        severity is one of 'serious' / 'caution' / 'note' and decides colour and
        order here, so the caller supplies no styling of its own.
        """
        self._win._review_sig.emit(str(title or ""), str(summary or ""),
                                   list(findings or []), list(unclear or []))

    def PromptReconfig(self):
        """Thread-safe: show the API key setup overlay (e.g. after an auth error)."""
        self._win._ready = False
        self._win._reconfig_sig.emit()

    def ShowCameraFrame(self, img_bytes: bytes):
        """Thread-safe: show a webcam frame in the small overlay (screen captures)."""
        self._win._camera_sig.emit(img_bytes)

    def ShowVideo(self, source: str, title: str = "", muted: bool = True,
                   audio_source: str = "") -> None:
        """Thread-safe: play a video where the reactor normally is.

        `source` is a local path or a direct media URL. `audio_source` is for
        the case where the sound arrives as its own stream — YouTube serves no
        combined format any more — and when it is given the two are played
        together and kept in step.

        Muted by default, and that is a decision rather than a default: a
        soundtrack talking over ULTRON is the one way this could make the
        assistant worse. The user turns sound on from the header button or by
        asking, and closes it the same two ways.
        """
        self._win._video_open_sig.emit(str(source or ""), str(title or ""),
                                       bool(muted), str(audio_source or ""))

    def StopVideo(self) -> None:
        """Thread-safe: close the video and give the HUD back to the reactor."""
        self._win._video_close_sig.emit()

    def SetVideoMuted(self, muted: bool) -> None:
        """Thread-safe: silence or unsilence whatever is playing."""
        self._win._video_mute_sig.emit(bool(muted))

    def VideoIsPlaying(self) -> bool:
        return bool(self._win.VideoIsPlaying())

    def StartCameraStream(self) -> None:
        """Thread-safe: start live camera feed in the full HUD area."""
        self._win.StartCameraStream()

    def StopCameraStream(self) -> None:
        """Thread-safe: stop the live camera feed."""
        self._win.StopCameraStream()

    @property
    def AssistantName(self) -> str:
        return self._win._assistant_name

    def StartSpeaking(self):
        self.SetState("SPEAKING")

    def StopSpeaking(self):
        if not self.muted:
            self.SetState("LISTENING")