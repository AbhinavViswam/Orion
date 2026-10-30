"""
core/reactor.py — Orion's reactor: a 3D shell of twisting glowing rings
round a white core, with orbit rings.

It is rendered like a tiny software renderer, not drawn with lines:

  1. Every strand, ring and glint is a dense run of points in 3D. numpy turns,
     tilts and projects them all at once.
  2. Each point adds its light into an image buffer (split across the four
     pixels it lands between, so edges stay smooth). Light adds up where strands
     cross, exactly like light does.
  3. A quarter-size copy of that buffer is blurred and becomes the bloom.
  4. The buffer is tone-mapped (bright parts roll off to white instead of
     clipping) and handed to Qt as an image.

All of it runs on its own thread (ReactorThread) at up to 60 frames a second,
so the window never waits for it and it never waits for the window.

States never cut from one to the next: each has a weight that eases toward 1
or 0, and the motion is the weighted blend. The voice rises fast and settles
slowly. Both are what keep it smooth.
"""
from __future__ import annotations

import math
import threading
import time

import numpy as np
from PyQt6.QtCore import QThread
from PyQt6.QtGui import QColor, QImage

# ── States ────────────────────────────────────────────────────────────────────
IDLE, LISTENING, THINKING, SPEAKING, MUTED, ASLEEP = range(6)
STATE_NAMES = ("IDLE", "LISTENING", "THINKING", "SPEAKING", "MUTED", "ASLEEP")

# Knobs per state:
#   spin   how fast the shell turns (rad/s)
#   lobe   how far the waves push the shell in and out
#   ripple waves running down the shell (listening), scaled by your voice
#   pulse  a heartbeat pulling it in (thinking)
#   flow   how fast the light runs round the strands
#   swell  how much the voice inflates it
#   alpha  overall brightness
#   close  how far it shrinks in (muted, asleep)
#   grey   how far its colour fades to grey
_K = ("spin", "lobe", "ripple", "pulse", "flow", "swell", "alpha", "close", "grey")
_TABLE = np.array([
    # spin  lobe  ripple pulse flow  swell alpha close grey
    [0.10, 0.21, 0.0,   0.0,  0.40, 0.04, 0.85, 0.00, 0.0],   # IDLE
    [0.16, 0.18, 1.0,   0.0,  0.70, 0.10, 1.00, 0.00, 0.0],   # LISTENING
    [0.36, 0.15, 0.0,   1.0,  1.50, 0.00, 0.95, 0.04, 0.0],   # THINKING
    [0.15, 0.24, 0.0,   0.0,  1.00, 0.22, 1.05, 0.00, 0.0],   # SPEAKING
    [0.10, 0.21, 0.0,   0.0,  0.40, 0.04, 0.40, 0.00, 1.0],   # MUTED: moves like IDLE, colour faded
    [0.04, 0.10, 0.0,   0.0,  0.14, 0.00, 0.32, 0.10, 0.6],   # ASLEEP
], dtype=np.float32)
_IDX = {k: i for i, k in enumerate(_K)}

ENTER_RATE = 8.0       # a new state is reached this fast (per second)
SETTLE_RATE = 3.5      # going quiet settles more slowly
LEVEL_ATTACK = 14.0    # the voice swells fast...
LEVEL_RELEASE = 4.0    # ...and settles gently
MAX_DT = 0.1           # a stalled frame doesn't make it leap

_ICE = np.array([0.86, 0.94, 1.00], dtype=np.float32)     # white-ice
_GREY = np.array([0.55, 0.58, 0.63], dtype=np.float32)
_HAZE = np.array([0.62, 0.90, 1.10], dtype=np.float32)   # the bloom leans blue


def Approach(cur: float, target: float, rate: float, dt: float) -> float:
    """Ease `cur` toward `target` at `rate` per second; the same at any frame rate."""
    return cur + (target - cur) * (1.0 - math.exp(-rate * dt))


def StateOf(name: str, speaking: bool, muted: bool) -> int:
    """Orion's UI state (and flags) as a reactor state."""
    if muted:
        return MUTED
    if speaking or name == "SPEAKING":
        return SPEAKING
    if name in ("THINKING", "PROCESSING"):
        return THINKING
    if name == "LISTENING":
        return LISTENING
    if name in ("SLEEPING", "STANDBY", "OFFLINE", "INITIALISING"):
        return ASLEEP
    return IDLE


def _Rgb(c: QColor) -> np.ndarray:
    return np.array([c.redF(), c.greenF(), c.blueF()], dtype=np.float32)


def _BoxBlur(a: np.ndarray, r: int) -> np.ndarray:
    """A box blur of radius r along both axes (cumulative sums: cost doesn't grow with r)."""
    if r < 1:
        return a
    for axis in (0, 1):
        pad = [(0, 0), (0, 0)]
        pad[axis] = (r + 1, r)
        c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis, dtype=np.float32)
        if axis == 0:
            a = (c[2 * r + 1:] - c[:-2 * r - 1]) / (2 * r + 1)
        else:
            a = (c[:, 2 * r + 1:] - c[:, :-2 * r - 1]) / (2 * r + 1)
    return a


class Reactor:
    # The shell is a stack of closed rings round one axis, each pushed into
    # petals that twist a little from ring to ring. Seen tipped toward you the
    # rings circle the core and the petals read as a slow spiral; where rings
    # bunch up at the folds the light adds into bright ridges.
    RINGS_N = 72
    SAMPLES = 700          # points on the widest ring: dense enough to read as a line
    PETALS = 5
    TWIST = 3.8            # how far the petals turn from the first ring to the last
    RINGS = 7              # orbit rings
    RING_SAMPLES = 1000
    TILT = 0.95            # the axis tipped toward you

    def __init__(self, seed: int = 7):
        rng = np.random.default_rng(seed)
        rings = np.linspace(0.05 * math.pi, 0.95 * math.pi, self.RINGS_N)
        # Small rings near the axis are short, so they get fewer points: the
        # same density everywhere for a lot less work.
        th, ph, off = [], [], []
        for t_i in rings:
            n = max(120, int(self.SAMPLES * math.sin(t_i)))
            th.append(np.full(n, t_i))
            ph.append(np.linspace(0.0, 2 * math.pi, n, endpoint=False))
            # Each ring's own phase for the faint light running round it.
            off.append(np.full(n, rng.uniform(0, 2 * math.pi)))
        self._th = np.concatenate(th).astype(np.float32)
        self._ph = np.concatenate(ph).astype(np.float32)
        self._off = np.concatenate(off).astype(np.float32)
        self._sin_th = np.sin(self._th)
        self._cos_th = np.cos(self._th)
        # Orbit rings: radius (of the shell's), tilt, yaw, precession, glint speed.
        # Spread evenly in tilt and yaw so they cross at many angles; one
        # wide faint ring sits outside the rest.
        self._rings = [
            (float(rng.uniform(1.12, 1.36)) if i < self.RINGS - 1 else 1.52,
             0.30 + 1.15 * (i / max(1, self.RINGS - 1)) + float(rng.uniform(-0.12, 0.12)),
             (i * 2.399) % math.pi, float(rng.uniform(-0.08, 0.08)),
             float(rng.choice((-1, 1)) * rng.uniform(0.30, 0.70)))
            for i in range(self.RINGS)
        ]
        self._ring_u = np.linspace(0, 2 * math.pi, self.RING_SAMPLES, endpoint=False,
                                   dtype=np.float32)
        # Stars for the backdrop: x, y (0..1), size, twinkle phase.
        self.stars = np.column_stack((rng.random(170), rng.random(170),
                                      rng.uniform(0.6, 1.5, 170),
                                      rng.uniform(0, 6.3, 170))).astype(np.float32)

        self.weights = np.zeros(len(STATE_NAMES), dtype=np.float32)
        self.weights[ASLEEP] = 1.0
        self.k = _TABLE[ASLEEP].copy()
        self.level = 0.0
        self.t = 0.0            # the shape's own clock
        self.spin = 0.0         # integrated, so a speed change never jumps it
        self.flow = 0.0
        self.beat = 0.0
        self._stamps: dict = {}
        self._luts: dict = {}

    # ── motion ────────────────────────────────────────────────────────────────

    def step(self, dt: float, state: int, level: float) -> None:
        dt = max(0.0, min(MAX_DT, dt))
        rate = SETTLE_RATE if state in (IDLE, MUTED, ASLEEP) else ENTER_RATE
        f = 1.0 - math.exp(-rate * dt)
        target = np.zeros_like(self.weights)
        target[state] = 1.0
        self.weights += (target - self.weights) * f
        self.weights /= max(1e-6, float(self.weights.sum()))
        self.k = self.weights @ _TABLE

        live = state in (LISTENING, SPEAKING)
        want = max(0.0, min(1.0, level)) if live else 0.0
        self.level = Approach(self.level, want,
                              LEVEL_ATTACK if want > self.level else LEVEL_RELEASE, dt)
        self.t += dt
        self.spin += dt * float(self.k[_IDX["spin"]]) * (1.0 + 0.7 * self.level)
        self.flow += dt * float(self.k[_IDX["flow"]])
        self.beat += dt * (1.0 + 1.2 * float(self.k[_IDX["pulse"]]))

    def brightness(self) -> float:
        return float(self.k[_IDX["alpha"]])

    # ── geometry ──────────────────────────────────────────────────────────────

    def View(self, x, y, z):
        """Turn round the axis by the spin, tip toward the viewer (with a slow
        sway so it never sits dead still). Returns x, y, depth."""
        ca, sa = math.cos(self.spin), math.sin(self.spin)
        x1 = x * ca - z * sa
        z1 = x * sa + z * ca
        tilt = self.TILT + 0.10 * math.sin(self.t * 0.21)
        ct, st = math.cos(tilt), math.sin(tilt)
        y2, z2 = y * ct - z1 * st, y * st + z1 * ct
        roll = 0.18 * math.sin(self.t * 0.13)
        cr, sr = math.cos(roll), math.sin(roll)
        return x1 * cr - y2 * sr, x1 * sr + y2 * cr, z2

    def Shell(self):
        k, t = self.k, self.t
        th, ph = self._th, self._ph
        st = self._sin_th
        lobe = float(k[_IDX["lobe"]]) + float(k[_IDX["swell"]]) * self.level
        ripple = float(k[_IDX["ripple"]]) * (0.03 + 0.16 * self.level)
        beat = (0.5 + 0.5 * math.sin(self.beat * 2.6)) ** 3
        pulse = float(k[_IDX["pulse"]]) * (0.04 + 0.08 * beat)
        breathe = 0.025 * math.sin(t * 1.1)
        # The petals: their phase shifts with the ring, so they twist into a
        # spiral, and a second slower wave keeps them from looking stamped out.
        a = self.PETALS * ph + self.TWIST * th + t * 0.50
        d = (0.78 * np.sin(a) * st ** 0.8
             + 0.22 * np.sin(3.0 * ph - 1.9 * th - t * 0.37))
        rad = 1.0 + breathe + lobe * d - pulse
        if ripple > 0.002:
            rad = rad * (1.0 + ripple * (0.5 + 0.5 * np.sin(th * 8.0 - t * 6.5)))
        # Each ring also folds up and down with its petals: that fold is what
        # makes the bright ridges.
        fold = 0.55 * lobe * np.cos(a) * st
        x, y, z = self.View(rad * st * np.cos(ph), rad * self._cos_th + fold,
                             rad * st * np.sin(ph))
        # Faint light running round each ring (the lines stay unbroken).
        on = 0.78 + 0.22 * np.sin(ph * 3.0 + self._off - self.flow * 2.4)
        return x, y, z, on

    # ── rendering ─────────────────────────────────────────────────────────────

    def render(self, size: int, tint: QColor, tint2: QColor | None = None,
               mix: float = 0.18) -> tuple[QImage, QImage]:
        """The reactor as two images of `size` square: the crisp light, and a
        small bloom image to be drawn scaled up over it (both additive)."""
        S = int(max(32, size))
        k = self.k
        alpha = float(k[_IDX["alpha"]])
        grey = float(k[_IDX["grey"]])
        col = _ICE * (1 - mix) + _Rgb(tint) * mix              # white-ice, tinted by the accent
        col = col * (1 - grey) + _GREY * grey
        col2 = col if tint2 is None else (_ICE * (1 - mix) + _Rgb(tint2) * mix) * (1 - grey) + _GREY * grey

        c = S / 2.0
        R = S * 0.29 * (1.0 - float(k[_IDX["close"]]))           # shell radius in px
        pts_x, pts_y, pts_w = [], [], []

        def Splat(px, py, w):
            pts_x.append(px)
            pts_y.append(py)
            pts_w.append(w)

        # The shell.
        x, y, z, on = self.Shell()
        persp = 1.0 / (1.0 - 0.2 * z)
        depth = np.clip((z + 1.3) / 2.6, 0.0, 1.0)
        w = (0.22 + 0.78 * depth ** 1.5) * on * 0.62
        Splat((c + x * R * persp).astype(np.float32), (c + y * R * persp).astype(np.float32),
              w.astype(np.float32))

        # Orbit rings and their glints.
        glints = []
        u = self._ring_u
        for i, (rr, tilt, yaw, prec, speed) in enumerate(self._rings):
            yw = yaw + self.t * prec
            cu, su = np.cos(u) * rr, np.sin(u) * rr
            ct, st = math.cos(tilt), math.sin(tilt)
            gx, gy, gz = cu, su * st, su * ct
            cyw, syw = math.cos(yw), math.sin(yw)
            gx, gz = gx * cyw - gz * syw, gx * syw + gz * cyw
            rx, ry, rz = self.View(gx, gy, gz)
            pp = 1.0 / (1.0 - 0.2 * rz / rr)
            rd = np.clip((rz / rr + 1.0) * 0.5, 0.0, 1.0)
            Splat((c + rx * R * pp).astype(np.float32), (c + ry * R * pp).astype(np.float32),
                  ((0.10 + 0.32 * rd) * (0.45 if i == len(self._rings) - 1 else 0.8))
                  .astype(np.float32))
            g = (self.t * speed + i * 1.3) % (2 * math.pi)
            j = int(g / (2 * math.pi) * len(u)) % len(u)
            glints.append((c + float(rx[j] * R * pp[j]), c + float(ry[j] * R * pp[j]),
                           0.35 + 0.65 * float(rd[j])))

        img = self.SplatAll(S, np.concatenate(pts_x), np.concatenate(pts_y),
                              np.concatenate(pts_w))
        # Glints and the core: a small pearl, solid white in the middle.
        for gx, gy, gb in glints:
            self.Stamp(img, gx, gy, max(1.5, R * 0.014), 2.6 * gb)
        lift = 1.0 + 0.35 * self.level
        self.Stamp(img, c, c, max(2.5, R * 0.050 * lift), 7.0, flat=True)
        self.Stamp(img, c, c, max(4.0, R * 0.10 * lift), 0.45)

        # Bloom: only a faint haze; the lines carry the light. A quarter-size
        # copy, blurred, plus soft halos round the core and each glint (the
        # haze leans a little blue).
        q = S // 4
        small = img[:q * 4, :q * 4].reshape(q, 4, q, 4).mean(axis=(1, 3))
        glow = _BoxBlur(_BoxBlur(small, max(1, q // 50)), max(1, q // 26))
        yy, xx = np.mgrid[0:q, 0:q].astype(np.float32)
        rr2 = (xx - q / 2) ** 2 + (yy - q / 2) ** 2
        d2 = rr2 / max(1.0, (R / 4 * 0.42 * lift)) ** 2
        edge = np.clip(1.0 - rr2 / (q / 2) ** 2, 0.0, 1.0) ** 2   # nothing at the border
        glow = (glow * 1.1 + 0.12 * np.exp(-d2)) * edge
        for gx, gy, gb in glints:
            self.Stamp(glow, gx / 4, gy / 4, max(1.0, R * 0.012), 0.55 * gb)

        crisp = self.ToImage(img, col, alpha * 1.25)
        haze = np.clip(col2 * (1 - grey) * _HAZE + col2 * grey, 0.0, 1.0)
        bloom = self.ToImage(glow, haze, alpha * 0.9)
        return crisp, bloom

    @staticmethod
    def SplatAll(S: int, px, py, w) -> np.ndarray:
        """Every point's light, shared bilinearly over the 4 pixels round it, in
        one pass."""
        x0 = np.floor(px).astype(np.int32)
        y0 = np.floor(py).astype(np.int32)
        ok = (x0 >= 0) & (y0 >= 0) & (x0 < S - 1) & (y0 < S - 1)
        px, py, w, x0, y0 = px[ok], py[ok], w[ok], x0[ok], y0[ok]
        fx = px - x0
        fy = py - y0
        i = y0 * S + x0
        idx = np.concatenate((i, i + 1, i + S, i + S + 1))
        a = w * (1 - fy)
        b = w * fy
        ww = np.concatenate((a * (1 - fx), a * fx, b * (1 - fx), b * fx))
        return np.bincount(idx, weights=ww, minlength=S * S).astype(np.float32).reshape(S, S)

    def Stamp(self, img: np.ndarray, x: float, y: float, r: float, gain: float,
               flat: bool = False) -> None:
        """Add a soft round dot of radius r at (x, y); `flat` gives a solid disc
        with a soft rim instead of a peak."""
        key = (int(r * 4), flat)
        st = self._stamps.get(key)
        if st is None:
            rr = max(0.5, key[0] / 4.0)
            n = int(rr * 6) | 1
            yy, xx = np.mgrid[0:n, 0:n].astype(np.float32) - n // 2
            d2 = (xx ** 2 + yy ** 2) / rr ** 2
            st = (np.exp(-d2 ** 3) if flat else np.exp(-d2)).astype(np.float32)
            self._stamps[key] = st
        n = st.shape[0]
        x0, y0 = int(round(x)) - n // 2, int(round(y)) - n // 2
        S = img.shape[0]
        ax0, ay0 = max(0, x0), max(0, y0)
        ax1, ay1 = min(S, x0 + n), min(S, y0 + n)
        if ax0 >= ax1 or ay0 >= ay1:
            return
        img[ay0:ay1, ax0:ax1] += st[ay0 - y0:ay1 - y0, ax0 - x0:ax1 - x0] * gain

    _LUT_N = 2048
    _LUT_MAX = 6.0          # light beyond this is fully white anyway

    def ToImage(self, a: np.ndarray, col: np.ndarray, gain: float) -> QImage:
        """Tone-map light to colour (bright parts roll off to white, never clip
        hard) as a premultiplied ARGB image whose alpha is its brightness.
        Through a lookup table: one index per pixel instead of maths per pixel."""
        key = (round(float(gain), 3), tuple(np.round(col, 3)))
        lut = self._luts.get(key)
        if lut is None:
            x = np.linspace(0.0, self._LUT_MAX, self._LUT_N, dtype=np.float32)
            v = 1.0 - np.exp(-x)
            hot = np.clip(v * 1.6 - 0.9, 0.0, 1.0)            # the brightest go white
            lut = np.empty((self._LUT_N, 4), dtype=np.uint8)
            for ch, ci in ((0, 2), (1, 1), (2, 0)):           # BGRA in memory
                lut[:, ch] = np.clip((v * col[ci] * (1 - hot) + hot * v) * 255.0, 0, 255)
            lut[:, 3] = np.clip(v * 255.0, 0, 255)
            lut = lut.view(np.uint32).ravel()        # one 32-bit pixel per entry
            if len(self._luts) > 64:
                self._luts.clear()
            self._luts[key] = lut
        scale = gain * (self._LUT_N - 1) / self._LUT_MAX
        idx = np.clip(a * scale, 0, self._LUT_N - 1).astype(np.uint16)
        out = np.take(lut, idx)                            # (h, w) of 32-bit pixels
        h, w = a.shape
        img = QImage(out.data, w, h, w * 4, QImage.Format.Format_ARGB32_Premultiplied)
        return img.copy()                                  # own the pixels


def MakeReactor() -> Reactor:
    """The main reactor when its file is here (it is kept out of git),
    otherwise Orion's own."""
    try:
        from Interface.ReactorCore import Reactor as CoreReactor
        return CoreReactor()
    except Exception as e:
        print(f"[Reactor] using the built-in look ({e.__class__.__name__})")
        return Reactor()


class ReactorThread(QThread):
    """Renders the reactor on its own thread at up to 60 fps. The window only
    ever picks up the newest finished frame, so neither waits on the other."""

    FPS = 60          # while it listens, thinks or talks
    IDLE_FPS = 40     # while quiet

    def __init__(self, parent=None):
        super().__init__(parent)
        self.reactor = MakeReactor()
        self._lock = threading.Lock()
        self._frame: tuple[QImage, QImage] | None = None
        self._frame_id = 0
        self._running = True
        # Written by the window, read here (plain values: no locking needed).
        self.state = ASLEEP
        self.level = 0.0
        self.size = 400
        self.tint = QColor("#00d4ff")
        self.tint2 = None           # a gradient's second colour: the glow
        self.mix = 0.18             # how much of the colour shows over the ice
        self.visible = True

    def frame(self):
        """(frame_id, (crisp, bloom)) of the newest finished frame, or (0, None)."""
        with self._lock:
            return self._frame_id, self._frame

    def stop(self) -> None:
        self._running = False
        self.wait(500)

    def run(self) -> None:
        last = time.perf_counter()
        while self._running:
            start = time.perf_counter()
            dt = start - last
            last = start
            try:
                self.reactor.step(dt, self.state, self.level)
                if self.visible:
                    try:
                        frame = self.reactor.render(self.size, self.tint, self.tint2, self.mix)
                    except TypeError:       # a reactor that only takes one colour
                        frame = self.reactor.render(self.size, self.tint)
                    with self._lock:
                        self._frame = frame
                        self._frame_id += 1
            except Exception:
                pass
            spent = time.perf_counter() - start
            busy = self.state in (LISTENING, THINKING, SPEAKING)
            period = 1.0 / (self.FPS if busy else self.IDLE_FPS)
            # Hidden: keep the motion clock going, but at a trickle.
            time.sleep(max(0.002, (period if self.visible else 0.2) - spent))
