"""
Interface/Accents.py: the colours you can give Ultron, the same set as JARVIS
on the phone (jarvis-android ui/theme/Accent.kt and Living.kt).

Three kinds:
  * Solid     one colour
  * Gradient  two colours that blend across the reactor (crisp light in the
              first, glow in the second); the interface uses the first
  * Living    colours that move by themselves: the rainbow, the arc reactor's
              glow, the suit's red and gold, the time of day, the wallpaper

Saved by name ("accent": "BLUE"), so the order here is free to change. Pure
Python (no Qt) apart from Chameleon, which reads the wallpaper through Qt.
"""
from __future__ import annotations

import math
import time
from datetime import datetime

SOLID, GRADIENT, LIVING = "Solid", "Gradient", "Living"
GROUPS = (SOLID, GRADIENT, LIVING)

# key, label, group, colour, second colour (gradients only)
_TABLE = (
    ("WHITE", "White", SOLID, "#dcefff", None),        # Ultron's original ice
    ("BLUE", "Blue", SOLID, "#29d8ff", None),
    ("PURPLE", "Violet", SOLID, "#a66bff", None),
    ("MAGENTA", "Magenta", SOLID, "#ff4fd8", None),
    ("PINK", "Pink", SOLID, "#ff5c9e", None),
    ("RED", "Red", SOLID, "#ff3b4e", None),
    ("ORANGE", "Orange", SOLID, "#ff8a1f", None),
    ("GOLD", "Gold", SOLID, "#ffd60a", None),
    ("LIME", "Lime", SOLID, "#b6ff3b", None),
    ("GREEN", "Green", SOLID, "#2ee88a", None),
    ("AURORA", "Aurora", GRADIENT, "#29d8ff", "#a66bff"),
    ("SUNSET", "Sunset", GRADIENT, "#ff8a1f", "#ff5c9e"),
    ("OCEAN", "Ocean", GRADIENT, "#2f7bff", "#14f0c8"),
    ("NEON", "Neon", GRADIENT, "#b6ff3b", "#29e6ff"),
    ("GALAXY", "Galaxy", GRADIENT, "#6b6bff", "#ff4fd8"),
    ("PRISM", "Prism", LIVING, "#29d8ff", None),
    ("ARC_REACTOR", "Arc Reactor", LIVING, "#29d8ff", None),
    ("MARK", "Mark", LIVING, "#ff4040", None),
    ("DAYLIGHT", "Daylight", LIVING, "#29d8ff", None),
    ("CHAMELEON", "Chameleon", LIVING, "#29d8ff", None),
)

DEFAULT = "GOLD"            # what a fresh install (or no choice yet) shows
# Names the phone has retired, loaded as the one nearest them.
_RETIRED = {"SKY": "BLUE", "INDIGO": "PURPLE", "ROSE": "PINK", "AMBER": "ORANGE", "TEAL": "GREEN",
            "SILVER": "WHITE", "RED_GOLD": "MARK", "BOREALIS": "ARC_REACTOR", "CANDY": "AURORA"}

HINT = ("Gradients blend two colours across the reactor. Prism sweeps the rainbow, red to violet "
        "and back; Arc Reactor glows from electric cyan to ice white; Mark turns between the suit's "
        "red and gold; Daylight follows the day, from dawn to night indigo; Chameleon takes its "
        "colour from your desktop wallpaper.")


class Accent:
    __slots__ = ("key", "label", "group", "color", "second")

    def __init__(self, key, label, group, color, second):
        self.key, self.label, self.group, self.color, self.second = key, label, group, color, second

    @property
    def living(self) -> bool:
        return self.group == LIVING

    @property
    def blends(self) -> bool:
        return self.second is not None or self.key in ("PRISM", "ARC_REACTOR", "MARK", "DAYLIGHT")


ALL = [Accent(*row) for row in _TABLE]
_BY_KEY = {a.key: a for a in ALL}


def Of(name: str | None, legacy_hex: str = "") -> Accent:
    """Tolerant of anything ever saved, including nothing. An older config
    only has a hex colour (`ui_color`); a hex that matches a swatch picks it."""
    n = (name or "").strip().upper()
    if n in _BY_KEY:
        return _BY_KEY[n]
    if n in _RETIRED:
        return _BY_KEY[_RETIRED[n]]
    hx = (legacy_hex or "").strip().lower()
    if len(hx) == 7 and hx.startswith("#") and Vividness(hx) > 0.35:
        # A colour from the old wheel: the nearest solid swatch.
        try:
            p = Lab(hx)
        except ValueError:
            return _BY_KEY[DEFAULT]
        return min(InGroup(SOLID), key=lambda a: sum((x - y) ** 2 for x, y in zip(p, Lab(a.color))))
    return _BY_KEY[DEFAULT]


def InGroup(group: str) -> list[Accent]:
    return [a for a in ALL if a.group == group]


# ── colour maths (OKLab / OKLCH, as on the phone) ───────────────────────────

def _Lin(v: float) -> float:
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _Gamma(x: float) -> float:
    v = min(1.0, max(0.0, x))
    return 12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055


def _Hex(r: float, g: float, b: float) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(int(round(min(1, max(0, c)) * 255)) for c in (r, g, b)))


def Lab(hx: str) -> tuple[float, float, float]:
    r, g, b = (_Lin(int(hx[i:i + 2], 16) / 255) for i in (1, 3, 5))
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def _LabToLinear(l, a, b):
    l1 = (l + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m1 = (l - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s1 = (l - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (4.0767416621 * l1 - 3.3077115913 * m1 + 0.2309699292 * s1,
            -1.2684380046 * l1 + 2.6097574011 * m1 - 0.3413193965 * s1,
            -0.0041960863 * l1 - 0.7034186147 * m1 + 1.7076147010 * s1)


def Oklch(l: float, c: float, hue: float) -> str:
    """OKLCH to hex; too colourful for the screen, it gives up colour until it fits."""
    h = math.radians(hue)
    while True:
        rgb = _LabToLinear(l, c * math.cos(h), c * math.sin(h))
        if c <= 0 or all(-0.0001 <= v <= 1.0001 for v in rgb):
            return _Hex(*(_Gamma(v) for v in rgb))
        c -= 0.004


_RED_HUE, _GOLD_HUE, _FULL = 29.0, 85.0, 0.17


def _Bright(hue: float) -> float:
    h = hue % 360
    if h > _GOLD_HUE:
        return 0.80
    k = min(1, max(0, (h - _RED_HUE) / (_GOLD_HUE - _RED_HUE)))
    return 0.66 + (0.82 - 0.66) * k


def _Chroma(hue: float) -> float:
    h = hue % 360
    if h > _GOLD_HUE:
        return _FULL
    k = min(1, max(0, (h - _RED_HUE) / (_GOLD_HUE - _RED_HUE)))
    return 0.21 + (_FULL - 0.21) * k


def _ToneAt(hue: float, chroma: float | None = None, bright: float | None = None) -> str:
    return Oklch(_Bright(hue) if bright is None else bright,
                 _Chroma(hue) if chroma is None else chroma, hue)


def _Smooth(x, a, b):
    k = min(1.0, max(0.0, (x - a) / (b - a)))
    return k * k * (3 - 2 * k)


# ── the living ones ─────────────────────────────────────────────────────────

_PRISM_CYCLE = 4 * 60.0
_PRISM_PAIR = 0.08


def _Prism(t: float) -> float:
    p = (t % _PRISM_CYCLE) / _PRISM_CYCLE
    return _RED_HUE + (305.0 - _RED_HUE) * (1 - abs(2 * p - 1))


def _ArcReactor(t: float, second: bool) -> str:
    if second:
        hue = 232 + 8 * math.sin(2 * math.pi * t / 29.0)
        chroma = 0.045 + 0.03 * math.sin(2 * math.pi * t / 31.0 + 2.0)
        return _ToneAt(hue, chroma, 0.88)
    hue = 215 + 12 * math.sin(2 * math.pi * t / 37.0)
    chroma = 0.095 + 0.055 * math.sin(2 * math.pi * t / 23.0 + 1.0)
    return _ToneAt(hue, chroma, 0.80 + 0.08 * (0.15 - chroma) / 0.11)


_MARK_MID, _MARK_REACH = (_RED_HUE + 85) / 2, (85 - _RED_HUE) / 2
_METAL_HUE, _METAL_CHROMA, _METAL_BRIGHT = 250.0, 0.04, 0.74


def _Mark(t: float, second: bool) -> str:
    swing = math.sin(2 * math.pi * t / 60.0)
    hue = _MARK_MID + _MARK_REACH * (-swing if second else swing)
    cool = _Smooth(math.sin(2 * math.pi * t / 180.0 + 0.5), 0.7, 0.95)
    if cool <= 0:
        return _ToneAt(hue)
    c1 = _Chroma(hue)
    a = c1 * math.cos(math.radians(hue)) * (1 - cool) + _METAL_CHROMA * math.cos(math.radians(_METAL_HUE)) * cool
    b = c1 * math.sin(math.radians(hue)) * (1 - cool) + _METAL_CHROMA * math.sin(math.radians(_METAL_HUE)) * cool
    return _ToneAt(math.degrees(math.atan2(b, a)), math.hypot(a, b),
                   _Bright(hue) * (1 - cool) + _METAL_BRIGHT * cool)


_NIGHT, _DAWN, _MIDDAY, _MIDDAY2 = "#5148b2", "#f0b478", "#e1f8ff", "#c9ebff"
_GOLDEN, _GOLDEN2, _DUSK, _DUSK2 = "#fe914c", "#c09c7c", "#abb5ff", "#7475b6"
_DAY = ((0, _NIGHT, _NIGHT), (300, _NIGHT, _NIGHT), (390, _DAWN, _DAWN), (420, _DAWN, _DAWN),
        (660, _MIDDAY, _MIDDAY2), (900, _MIDDAY, _MIDDAY2), (1020, _GOLDEN, _GOLDEN2),
        (1080, _GOLDEN, _GOLDEN2), (1170, _DUSK, _DUSK2), (1290, _NIGHT, _NIGHT), (1440, _NIGHT, _NIGHT))


def _Daylight(t: float, second: bool) -> str:
    now = datetime.fromtimestamp(t)
    minute = now.hour * 60 + now.minute + now.second / 60
    i = max(0, min(len(_DAY) - 2, max(j for j, d in enumerate(_DAY) if d[0] <= minute)))
    (m0, a0, b0), (m1, a1, b1) = _DAY[i], _DAY[i + 1]
    k = min(1.0, max(0.0, (minute - m0) / (m1 - m0)))
    p, q = Lab(b0 if second else a0), Lab(b1 if second else a1)
    l, a, b = (p[n] + (q[n] - p[n]) * k for n in range(3))
    half = (math.degrees(math.atan2(p[2] + q[2], p[1] + q[1])) + 360) % 360
    if half >= 300 or half <= 20:                 # never pass through pink
        dip = 1 - 0.8 * math.sin(math.pi * k)
        a, b = a * dip, b * dip
    return Oklch(l, math.hypot(a, b), math.degrees(math.atan2(b, a)))


def At(accent: Accent, t: float | None = None, wallpaper: str = "") -> tuple[str, str]:
    """The colour pair (main, second) of an accent at time t (seconds)."""
    t = time.time() if t is None else t
    k = accent.key
    if k == "PRISM":
        return _ToneAt(_Prism(t)), _ToneAt(_Prism(t + _PRISM_PAIR * _PRISM_CYCLE))
    if k == "ARC_REACTOR":
        return _ArcReactor(t, False), _ArcReactor(t, True)
    if k == "MARK":
        return _Mark(t, False), _Mark(t, True)
    if k == "DAYLIGHT":
        return _Daylight(t, False), _Daylight(t, True)
    if k == "CHAMELEON":
        c = wallpaper or accent.color
        return c, c
    return accent.color, accent.second or accent.color


def Swatch(accent: Accent) -> list[str]:
    """The colours a swatch shows: one, the two of a pair, or the colours a
    living one goes through (round the dot)."""
    k = accent.key
    if k == "PRISM":
        return [_ToneAt(h) for h in (_RED_HUE, 85, 145, 200, 260, 305, 200, 85, _RED_HUE)]
    if k == "ARC_REACTOR":
        return [_ToneAt(205), _ToneAt(225, 0.04, 0.9), _ToneAt(228), _ToneAt(232, 0.05, 0.88), _ToneAt(205)]
    if k == "MARK":
        return [_ToneAt(_RED_HUE), _ToneAt(85), _ToneAt(_METAL_HUE, _METAL_CHROMA, _METAL_BRIGHT), _ToneAt(_RED_HUE)]
    if k == "DAYLIGHT":
        return [_NIGHT, _DAWN, _MIDDAY, _GOLDEN, _DUSK, _NIGHT]
    if k == "CHAMELEON":
        return ["#29d8ff", "#2ee88a", "#ffd60a", "#ff5c9e", "#a66bff", "#29d8ff"]
    return [accent.color] + ([accent.second] if accent.second else [])


def Vividness(hx: str) -> float:
    """0 for a grey or ice white, 1 for a full colour (HSV saturation)."""
    r, g, b = (int(hx[i:i + 2], 16) / 255 for i in (1, 3, 5))
    hi, lo = max(r, g, b), min(r, g, b)
    return 0.0 if hi <= 0 else (hi - lo) / hi
