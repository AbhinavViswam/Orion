# Reactor maths ported from a Three.js scene to numpy so it runs without a web view.
# Personal use only: keep this file out of anything shared.
"""
core/reactor_core.py: Ultron's main reactor, drawn by the software renderer in core/reactor.py.

The scene:
  - a shell of 36 latitude courses whose lobes drift and curl over time
  - 1200 sparkle points riding the same surface
  - 24 spiral arms round the core, slowly turning
  - a glass pearl core with a hot inner bead and a soft glow
  - 8 tilted orbit rings, each carrying two travelling nodes, and one wide faint ring

It is white-ice instead of state colours (Ultron's
theme), and the voice pushes the shell's "charge" so talking visibly gathers it.
"""
from __future__ import annotations

import math

import numpy as np
from PyQt6.QtGui import QColor, QImage

from Interface.Reactor import Reactor as _BaseReactor, _BoxBlur, _Rgb, _ICE, _GREY, _HAZE, _IDX, SPEAKING, THINKING, MUTED, ASLEEP, Approach

# Orbit rings: semi-axes a, b and Euler rotation x, y, z.
_RINGS = (
    (5.35, 4.86, 0.42, 0.36, 0.20), (5.58, 4.78, -0.57, 0.74, -0.40),
    (5.08, 5.07, 0.94, -0.40, 0.60), (5.74, 4.62, -0.73, -0.62, 0.26),
    (5.39, 4.97, 0.16, -1.10, -0.74), (5.01, 5.03, -0.26, 1.30, 0.80),
    (5.80, 4.56, 1.13, 0.58, -0.46), (5.37, 5.14, -0.93, 0.28, -0.85),
)
_HALF = 6.45            # scene units from the centre to the image edge
_CAM = 25.0             # camera distance
_LIGHT = np.array([-0.6, 1.0, 1.0], dtype=np.float32) / math.sqrt(2.36)


def Smooth(e0: float, e1: float, x):
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def Euler(x: float, y: float, z: float) -> np.ndarray:
    """Three.js 'XYZ' Euler rotation matrix (Rx · Ry · Rz)."""
    cx, sx, cy, sy, cz, sz = (math.cos(x), math.sin(x), math.cos(y), math.sin(y),
                              math.cos(z), math.sin(z))
    rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]], dtype=np.float32)
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], dtype=np.float32)
    rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]], dtype=np.float32)
    return rx @ ry @ rz


class Reactor(_BaseReactor):
    COURSES = 36
    COURSE_SAMPLES = 820       # on the widest course; fewer near the poles
    SPARKS = 1200
    ARMS = 24
    ARM_SAMPLES = 180
    RING_SAMPLES = 900

    def __init__(self, seed: int = 7):
        super().__init__(seed)
        # Shell courses: theta round the course, phi its latitude.
        th, ph = [], []
        for u in range(self.COURSES):
            phi = math.asin((u + 0.5) / self.COURSES * 2 - 1)
            n = max(220, int(self.COURSE_SAMPLES * math.cos(phi)))
            th.append(np.linspace(0, 2 * math.pi, n, endpoint=False))
            ph.append(np.full(n, phi))
        self._bth = np.concatenate(th).astype(np.float32)
        self._bph = np.concatenate(ph).astype(np.float32)
        # Sparkles (a fixed seeded generator, so they always sit in the same places).
        y = 23991

        def Rnd():
            nonlocal y
            y = (y * 1664525 + 1013904223) & 0xFFFFFFFF
            return y / 4294967296
        sp = [(Rnd() * 2 * math.pi, math.asin(Rnd() * 2 - 1), Rnd()) for _ in range(self.SPARKS)]
        self._sth = np.array([s[0] for s in sp], dtype=np.float32)
        self._sph = np.array([s[1] for s in sp], dtype=np.float32)
        self._ssz = np.array([0.6 + Rnd() * 1.1 for _ in sp], dtype=np.float32)
        # Spiral arms round the core, in their own turning group.
        wt = np.linspace(0, 1, self.ARM_SAMPLES, dtype=np.float32)
        arms = []
        for a in range(self.ARMS):
            r = 0.18 + wt ** 1.5 * 1.63
            ang = a / self.ARMS * 2 * math.pi + wt * 4.7
            arms.append(np.stack((r * np.cos(ang), r * np.sin(ang),
                                  np.sin(ang * 2 + wt * 3) * 0.4 * wt), axis=1))
        self._arms = np.concatenate(arms).astype(np.float32)
        frac = np.tile(wt, self.ARMS)
        self._arm_gap = (Smooth(0.08, 0.3, frac) * (1.0 - Smooth(0.65, 1.0, frac) * 0.35)).astype(np.float32)
        # Orbit rings and their nodes.
        u = np.linspace(0, 2 * math.pi, self.RING_SAMPLES, endpoint=False, dtype=np.float32)
        self._ring_cos, self._ring_sin = np.cos(u), np.sin(u)
        rng = np.random.default_rng(seed)
        self._node_phase = rng.uniform(0, 6.28, len(_RINGS))
        self._node_speed = [(-0.06 if i % 2 else 0.05) * (1 + i * 0.08) for i in range(len(_RINGS))]
        self._sim = 0.0        # the scene's clock (runs faster when thinking)
        self._breath_ph = 0.0  # the speaking breath's own clock
        self.breath = 0.0      # how much it breathes right now (0 → 1)
        self._speed = 1.0
        self._scale = 1.0
        self._glow_stamps: dict = {}

    # ── motion ────────────────────────────────────────────────────────────────

    def step(self, dt: float, state: int, level: float) -> None:
        super().step(dt, state, level)
        dt = max(0.0, min(0.1, dt))
        want_speed = 1.8 if state == THINKING else 0.25 if state in (MUTED, ASLEEP) else 1.0
        want_scale = 0.9 if state in (MUTED, ASLEEP) else 1.05 if state == SPEAKING else 1.0
        self._speed = Approach(self._speed, want_speed, 9.0, dt)
        self._scale = Approach(self._scale, want_scale, 9.0, dt)
        self._sim += dt * self._speed
        # Breathing while it speaks: eases in as it starts, out as it stops.
        self.breath = Approach(self.breath, 1.0 if state == SPEAKING else 0.0, 3.0, dt)
        self._breath_ph += dt * 2 * math.pi * 0.48       # about one breath every 2 s

    # ── geometry ──────────────────────────────────────────────────────────────

    def Surface(self, theta, phi, charge: float):
        t = self._sim
        current = t - 0.72 * np.sin(phi)
        fphi = phi + 0.055 * np.cos(phi) * np.sin(theta * 2.0 - current * 0.22)
        c = np.cos(fphi)
        w = np.sin(theta * 4.0 + current * 0.49) * np.cos(fphi * 3.0)
        cross = np.sin(theta * 8.0 - current * 0.28) * 0.25
        radius = (3.65 + (w + cross) * 0.98 * (1.0 - charge * 0.18)) * (1.0 - charge * 0.15)
        valley = 1.0 - radius / 4.12
        curl = 0.7 * (valley + np.sqrt(valley * valley + 0.0025))
        twist = theta + curl * np.sin(current * 0.35) + charge * 0.24 * np.sin(phi * 3.0)
        p = np.stack((c * np.cos(twist), np.sin(fphi) * 0.88, c * np.sin(twist)), axis=1)
        return p * radius[:, None], current

    def Project(self, pts, S: float):
        """World points (after the group turn) to pixels; also returns depth z."""
        ppu = S / 2.0 / _HALF
        z = pts[:, 2]
        persp = _CAM / (_CAM - z)
        px = S / 2.0 + pts[:, 0] * persp * ppu
        py = S / 2.0 - pts[:, 1] * persp * ppu
        return px.astype(np.float32), py.astype(np.float32), z

    # ── rendering ─────────────────────────────────────────────────────────────

    def render(self, size: int, tint: QColor, tint2: QColor | None = None,
               mix: float = 0.18) -> tuple[QImage, QImage]:
        S = int(max(32, size))
        t = self._sim
        k = self.k
        alpha = float(k[_IDX["alpha"]])
        grey = float(k[_IDX["grey"]])
        col = _ICE * (1 - mix) + _Rgb(tint) * mix
        col = col * (1 - grey) + _GREY * grey
        # A gradient accent: the crisp light in the first colour, the glow in the second.
        col2 = col if tint2 is None else (_ICE * (1 - mix) + _Rgb(tint2) * mix) * (1 - grey) + _GREY * grey
        charge = 0.2 * self.level
        # The breath: a slow swell in and out, plus the voice pushing it
        # outward on each word (the level already rises fast, falls slowly).
        slow = 0.5 - 0.5 * math.cos(self._breath_ph)
        swell = self.breath * (0.045 * slow + 0.075 * self.level)
        sc = self._scale * (1.0 + swell)
        charge = charge * (1.0 - 0.6 * self.breath)       # speak outward, not inward

        # The whole reactor's turn, and the shell's own fixed tilt inside it.
        rd = Euler(0.08 + math.sin(t * 0.12) * 0.04, t * 0.025, 0.0) * sc
        rf = rd @ Euler(0.1, 0.0, -0.08)
        xs, ys, ws = [], [], []

        # Shell courses.
        p, current = self.Surface(self._bth, self._bph, charge)
        p = p @ rf.T
        px, py, z = self.Project(p, S)
        n = p / np.maximum(1e-4, np.linalg.norm(p, axis=1))[:, None]
        sheen = 0.45 + 0.55 * np.abs(n @ _LIGHT)
        front = Smooth(-2.8, 2.7, z)
        flow = (0.5 + 0.5 * np.sin(self._bth * 4.0 + current * 0.49)) ** 10
        dcore = np.hypot(p[:, 0], p[:, 1])
        mix = 0.22 + 0.50 * front
        gap = mix + (1.0 - mix) * Smooth(0.42, 1.55, dcore)
        energy = (0.14 + front * 1.10 + flow * 0.10) * sheen * gap * 0.8
        lum = 0.26 + 0.66 * front                     # back courses are dim blue
        xs.append(px); ys.append(py); ws.append((energy * lum * 0.72).astype(np.float32))

        # Sparkles riding the surface.
        sp, scur = self.Surface(self._sth + t * 0.014, self._sph, charge)
        sp = sp @ rf.T
        spx, spy, sz = self.Project(sp, S)
        sf = Smooth(-4.0, 4.0, sz)
        sa = (0.055 + sf * 0.40) * (0.45 + 0.55 * (0.5 + 0.5 * np.sin(self._sth * 4.0 + scur * 0.49)) ** 3)
        xs.append(spx); ys.append(spy); ws.append((sa * self._ssz * 1.6).astype(np.float32))

        # Spiral arms (their own turn inside the reactor group).
        ra = rd @ Euler(0.0, math.sin(t * 0.13) * 0.27, t * 0.19)
        ap = self._arms @ ra.T
        apx, apy, az = self.Project(ap, S)
        aw = 0.30 * self._arm_gap * (0.52 + 0.48 * Smooth(-0.5, 0.5, az))
        xs.append(apx); ys.append(apy); ws.append((aw * 0.55).astype(np.float32))

        # Orbit rings and their nodes.
        nodes = []
        for i, (a, b, ex, ey, ez) in enumerate(_RINGS):
            rr = Euler(ex + math.sin(t * (0.035 + i * 0.002) + i) * 0.055,
                        ey + math.sin(t * (0.029 + i * 0.003) + i * 0.7) * 0.065,
                        ez + math.sin(t * 0.045 + i) * 0.08)
            m = rd @ rr
            loc = np.stack((a * self._ring_cos, b * self._ring_sin,
                            np.zeros_like(self._ring_cos)), axis=1)
            rp = loc @ m.T
            rpx, rpy, rz = self.Project(rp, S)
            # Every ring equally bright (rather than two bright and six faint),
            # with a gentle breathing of its own.
            op = 0.13 + 0.025 * math.sin(t * 0.08 + i * 1.1)
            fr = Smooth(-5.0, 5.0, rz)
            xs.append(rpx); ys.append(rpy)
            ws.append((op * (0.24 + fr * 0.76) * (0.55 + 0.40 * fr) * 1.1).astype(np.float32))
            for j in range(2):
                ang = self._node_phase[i] + t * self._node_speed[i] + j * math.pi * 1.24
                q = m @ np.array([a * math.cos(ang), b * math.sin(ang), 0.0], dtype=np.float32)
                nx, ny, nz = self.Project(q[None, :], S)
                nop = 0.34 * (0.55 + 0.45 * (float(nz[0]) / 6 * 0.5 + 0.5))
                nodes.append((float(nx[0]), float(ny[0]), 0.38 if j == 0 else 0.22, nop))

        # The wide faint ring.
        wr = rd @ Euler(0.22, -0.12, 0.0)
        wl = np.stack((6.13 * self._ring_cos, 6.13 * self._ring_sin,
                       np.zeros_like(self._ring_cos)), axis=1) @ wr.T
        wpx, wpy, _ = self.Project(wl, S)
        xs.append(wpx); ys.append(wpy); ws.append(np.full(len(wpx), 0.055 * 0.55, np.float32))

        img = self.SplatAll(S, np.concatenate(xs), np.concatenate(ys), np.concatenate(ws))

        ppu = S / 2.0 / _HALF * sc
        c = S / 2.0
        # Nodes: soft sprites (a radial gradient).
        for nx, ny, scale, nop in nodes:
            self.Glow(img, nx, ny, scale * 0.5 * ppu, nop * 2.4)
        # Core: glow sprite, glass pearl, hot inner bead.
        self.Glow(img, c, c, 1.25 * 0.5 * ppu * (1.0 + 2.5 * swell), 0.32 * 1.4 * (1.0 + 4.0 * swell))
        self.Stamp(img, c, c, max(2.0, 0.26 * ppu), 1.5, flat=True)
        self.Stamp(img, c, c, max(1.5, 0.10 * ppu * (1 + self.level * 0.25)), 5.0, flat=True)

        # Bloom (strength .42, threshold .85): only the bright parts glow.
        q = S // 4
        small = img[:q * 4, :q * 4].reshape(q, 4, q, 4).mean(axis=(1, 3))
        bright = np.maximum(small - 0.35, 0.0)
        glow = _BoxBlur(_BoxBlur(bright, max(1, q // 40)), max(1, q // 18))
        yy, xx = np.mgrid[0:q, 0:q].astype(np.float32)
        rr2 = (xx - q / 2) ** 2 + (yy - q / 2) ** 2
        edge = np.clip(1.0 - rr2 / (q / 2) ** 2, 0.0, 1.0) ** 2
        glow = glow * 1.6 * edge
        for nx, ny, scale, nop in nodes:
            self.Glow(glow, nx / 4, ny / 4, scale * 0.5 * ppu / 4 * 1.6, nop * 0.9)

        crisp = self.ToImage(img, col, alpha * 1.25)
        haze = np.clip(col2 * (1 - grey) * _HAZE + col2 * grey, 0.0, 1.0)
        bloom = self.ToImage(glow, haze, alpha * 0.9)
        return crisp, bloom

    def Glow(self, img: np.ndarray, x: float, y: float, r: float, gain: float) -> None:
        """The sprite: a radial gradient (1 → .75 at 13% → .12 at 40% → 0)."""
        key = max(2, int(r * 2))
        st = self._glow_stamps.get(key)
        if st is None:
            rr = key / 2.0
            n = int(rr * 2) | 1
            yy, xx = np.mgrid[0:n, 0:n].astype(np.float32) - n // 2
            d = np.sqrt(xx ** 2 + yy ** 2) / max(0.5, rr)
            st = np.interp(d, [0.0, 0.13, 0.40, 1.0], [1.0, 0.75, 0.12, 0.0]).astype(np.float32)
            self._glow_stamps[key] = st
        n = st.shape[0]
        x0, y0 = int(round(x)) - n // 2, int(round(y)) - n // 2
        S = img.shape[0]
        ax0, ay0 = max(0, x0), max(0, y0)
        ax1, ay1 = min(S, x0 + n), min(S, y0 + n)
        if ax0 >= ax1 or ay0 >= ay1:
            return
        img[ay0:ay1, ax0:ax1] += st[ay0 - y0:ay1 - y0, ax0 - x0:ax1 - x0] * gain
