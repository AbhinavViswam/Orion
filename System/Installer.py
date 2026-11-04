"""
MARK XL — Dependency auto-installer.

Called automatically on first launch and after engine reconfiguration.
Installs only the packages that are actually missing, then exits cleanly.
"""
from __future__ import annotations

import importlib.util
import platform
import subprocess
import sys
from typing import Callable

# ── Package lists ─────────────────────────────────────────────────────────
# Each entry: (import_name, pip_package_name)

_CORE: list[tuple[str, str]] = [
    ("psutil",             "psutil"),
    ("PIL",                "pillow"),
    ("sounddevice",        "sounddevice"),
    ("numpy",              "numpy"),
    ("requests",           "requests"),
    ("bs4",                "beautifulsoup4"),
    ("ddgs",               "ddgs"),
    ("pyautogui",          "pyautogui"),
    ("pyperclip",          "pyperclip"),
    ("pygetwindow",        "pygetwindow"),
    ("mss",                "mss"),
    ("cv2",                "opencv-python"),
    ("soundfile",          "soundfile"),
    ("miniaudio",          "miniaudio"),
    ("send2trash",         "send2trash"),
    ("pptx",               "python-pptx"),
    ("youtube_transcript_api", "youtube-transcript-api"),
]

# Windows-only (pywinauto, pycaw, comtypes)
_WINDOWS: list[tuple[str, str]] = [
    ("comtypes",   "comtypes"),
    ("pycaw",      "pycaw"),
    ("pywinauto",  "pywinauto"),
]

# STT engine packages
_STT: dict[str, list[tuple[str, str]]] = {
    "whisper": [("faster_whisper", "faster-whisper")],
    "vosk":    [("vosk",           "vosk")],
}

# TTS engine packages
_TTS: dict[str, list[tuple[str, str]]] = {
    "edgetts":    [("edge_tts", "edge-tts")],
    # kokoro>=0.9 dropped AlbertModel/AutoModel from transformers — version pin is critical
    "kokoro":     [("kokoro",   "kokoro>=0.9"), ("soundfile", "soundfile")],
    "elevenlabs": [],   # uses only requests, already in core
}


# ── Helpers ───────────────────────────────────────────────────────────────

PIP_TIMEOUT_S = 15 * 60          # one package (torch-sized ones are slow, but never forever)
BROWSER_TIMEOUT_S = 20 * 60      # the Chromium download

def _Available(module: str) -> bool:
    """Return True if the module can be imported (no actual import)."""
    return importlib.util.find_spec(module) is not None


def Pip(package: str, log: Callable | None = None) -> bool:
    if log:
        log(f"SYS: pip install {package} …")
    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "pip", "install", package,
                "--quiet", "--disable-pip-version-check",
            ],
            capture_output=True, timeout=PIP_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        if log:
            log(f"ERR: {package} install took over {PIP_TIMEOUT_S // 60} minutes, so I stopped it.")
        return False
    ok = result.returncode == 0
    if not ok and log:
        stderr = result.stderr.decode(errors="replace").strip()
        log(f"ERR: {package} install failed — {stderr[:140]}")
    return ok


# ── Public API ────────────────────────────────────────────────────────────

def InstallForConfig(config: dict, log: Callable | None = None) -> None:
    """
    Install all missing packages required by *config*.

    Blocking — always call from a background thread.
    Progress is reported via the optional *log* callback (receives a str).
    """
    stt = config.get("stt_engine", "whisper").lower()
    tts = config.get("tts_engine", "edgetts").lower()

    needed: list[tuple[str, str]] = list(_CORE)
    needed += _STT.get(stt, [])
    needed += _TTS.get(tts, [])
    if platform.system() == "Windows":
        needed += _WINDOWS

    # Deduplicate (preserve order, key = pip name)
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for mod, pkg in needed:
        if pkg not in seen:
            seen.add(pkg)
            unique.append((mod, pkg))

    missing = [(mod, pkg) for mod, pkg in unique if not _Available(mod)]

    if not missing:
        if log:
            log("SYS: All dependencies already installed ✓")
        return

    pkg_names = ", ".join(p for _, p in missing)
    if log:
        log(f"SYS: Installing {len(missing)} package(s): {pkg_names}")

    for _mod, pkg in missing:
        Pip(pkg, log)

    # Playwright: install the package + download Chromium browser
    if not _Available("playwright"):
        Pip("playwright", log)
        if log:
            log("SYS: Downloading Playwright browser (Chromium, ~150 MB — one-time)…")
        try:
            r = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                capture_output=True, timeout=BROWSER_TIMEOUT_S,
            )
            ready = r.returncode == 0
        except subprocess.TimeoutExpired:
            ready = False
        if log:
            log("SYS: Playwright browser ready." if ready else
                "ERR: The Playwright browser download didn't finish. It's tried again next time.")

    if log:
        log("SYS: All dependencies ready ✓")
