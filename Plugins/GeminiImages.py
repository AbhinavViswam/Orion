"""
Make a batch of images in the Gemini web app (gemini.google.com), using the
user's own Gemini subscription instead of an API key.

Reads <folder>/image_jobs.json — a list of {"id", "name", "prompt"} — and for
each job with no image yet: opens a new tab, sends the prompt, waits for the
picture, downloads it, saves <folder>/images/<name>.png at 1080x1080, and
closes the tab.

WHY CHROME IS STARTED BY HAND AND NOT BY PLAYWRIGHT
    Google refuses sign-in in a browser Playwright launched ("this browser may
    not be secure"). So Chrome is started as a normal program with a debug port
    and its own profile folder, and Playwright only attaches to it. Signing in
    there is an ordinary sign-in, and it is remembered for the next run. The
    profile is separate from the user's everyday Chrome, so both can be open.

It runs in a background thread: "start" returns straight away, "status" says
how far it got, "stop" ends it after the current image.

Can also be run without Orion, from the Orion folder:
    .venv\\Scripts\\python.exe -m Plugins.GeminiImages "C:\\...\\puttu-project"
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

DEFAULT_FOLDER = r"C:\DILSHA\ABHI\AI ASSISTANTS\JARVIS\puttu-project"
PROFILE_DIR = Path.home() / ".orion_profiles" / "gemini_images"
GEMINI_URL = "https://gemini.google.com/app"
DEBUG_PORT = 9333
SIZE = 1080
IMAGE_WAIT_S = 240          # Gemini can take a while on a busy day
SIGN_IN_WAIT_S = 600        # time the user gets to sign in on the first run
MAX_FAILS_IN_A_ROW = 3      # usually a daily limit or a changed page; stop and say so
BATCH_SIZE = 10             # images per run; run again for the next batch

# Gemini's page changes now and then; each list is tried in order.
INPUT_SELECTORS = [
    'rich-textarea div[contenteditable="true"]',
    'div.ql-editor[contenteditable="true"]',
    '[role="textbox"][contenteditable="true"]',
]
IMAGE_SELECTORS = [
    "model-response generated-image img",
    "model-response single-image img",
    "model-response img.image",
    "generated-image img",
]
DOWNLOAD_SELECTORS = [
    '[data-test-id="download-generated-image-button"]',
    'button[aria-label*="Download full size" i]',
    'button[aria-label*="Download" i]',
]
LIMIT_WORDS = ("reached your limit", "limit for today", "try again tomorrow", "usage limit")

PLUGIN = {
    "name": "gemini_images",
    "description": (
        "Makes a batch of images in the Gemini web app with the user's own Gemini "
        "subscription (no API key), one browser tab per image, saved under each "
        "item's name. Use for 'make the product images', 'generate the Puttu "
        "catalog photos', 'how are the images going', 'stop the images'. "
        "action: start | status | stop. Not for a single quick image or for "
        "opening a website - use browser_control for that."
    ),
    "brief": "Batch-makes images in the Gemini web app: start, status, stop.",
    "behavior": "NON_BLOCKING",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "start | status | stop (default start)"},
            "folder": {"type": "STRING",
                       "description": "Folder holding image_jobs.json. Default: the Puttu Project folder."},
            "only": {"type": "STRING",
                     "description": "Optional space-separated job IDs to make, e.g. 'PP-LAY-01 PP-SWT-02'"},
            "redo": {"type": "BOOLEAN", "description": "Remake images that already exist"},
            "subfolder": {"type": "STRING",
                          "description": "Optional folder name inside images/ for this batch, e.g. 'batch 3'"},
            "count": {"type": "INTEGER",
                      "description": "How many images to make this run (default 10). "
                                     "Each run continues with the next missing ones."},
        },
        "required": [],
    },
}


# ── shared state for status / stop ───────────────────────────────────────────
_state = {"running": False, "stop": False, "done": 0, "total": 0, "failed": [],
          "current": "", "note": "", "out_dir": ""}
_lock = threading.Lock()


def _log(player, text):
    print(f"[GeminiImages] {text}")
    _state["note"] = text
    if player:
        try:
            player.WriteLog(f"ORION: {text}")
        except Exception:
            pass


# ── files ────────────────────────────────────────────────────────────────────
def _existing_names(images_dir: Path) -> set[str]:
    """Names already made, in images/ or any batch subfolder (not _backup/_debug ones)."""
    if not images_dir.exists():
        return set()
    return {p.stem for p in images_dir.rglob("*.png")
            if not any(part.startswith("_") for part in p.relative_to(images_dir).parts[:-1])}


def _load_jobs(folder: Path, only: str, redo: bool, subfolder: str = ""):
    jobs_file = folder / "image_jobs.json"
    if not jobs_file.exists():
        raise FileNotFoundError(
            f"No image_jobs.json in {folder}. In that folder run: python make_images.py --export-jobs")
    jobs = json.loads(jobs_file.read_text(encoding="utf-8"))
    if only:
        wanted = {w.strip().upper() for w in only.replace(",", " ").split() if w.strip()}
        jobs = [j for j in jobs if str(j.get("id", "")).upper() in wanted]
    images_dir = folder / "images"
    out_dir = images_dir
    if subfolder:
        # Stays inside images/: no "..", no drive or absolute path.
        out_dir = (images_dir / subfolder).resolve()
        if not out_dir.is_relative_to(images_dir.resolve()) or ".." in Path(subfolder).parts:
            raise ValueError(f"The subfolder {subfolder!r} must be a folder inside images/.")
    for j in jobs:
        j["name"] = _SafeName(j.get("name"))
    jobs = [j for j in jobs if j["name"]]
    if not redo:
        done = _existing_names(images_dir)
        jobs = [j for j in jobs if j["name"] not in done]
    return jobs, out_dir


def _SafeName(name) -> str:
    """A job's name as a plain file name: no folders, no characters Windows refuses."""
    name = Path(str(name or "")).name.strip()
    for bad in '<>:"/\\|?*':
        name = name.replace(bad, "_")
    return "" if name in ("", ".", "..") else name


def _save_square(img_bytes: bytes, path: Path):
    from PIL import Image
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((SIZE, SIZE), Image.LANCZOS)
    # The Gemini app puts a small sparkle mark in the bottom-right corner
    # (about 995-1047 px on a 1080 image). Trim a thin border, centred, to cut it off.
    img = img.crop((40, 40, 990, 990)).resize((SIZE, SIZE), Image.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG", optimize=True)


# ── Chrome ───────────────────────────────────────────────────────────────────
def _find_chrome() -> str | None:
    try:
        from Actions.BrowserControl import FindExeWindows   # Orion's own lookup
        exe = FindExeWindows("chrome")
        if exe:
            return exe
    except Exception:
        pass
    for p in (Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
              Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
              Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"):
        if p.exists():
            return str(p)
    return shutil.which("chrome")


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _start_chrome() -> subprocess.Popen | None:
    """Start Chrome with the debug port, unless one from an earlier run is still up."""
    if _port_open(DEBUG_PORT):
        return None
    exe = _find_chrome()
    if not exe:
        raise RuntimeError("Google Chrome was not found on this PC")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([
        exe, f"--remote-debugging-port={DEBUG_PORT}", f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run", "--no-default-browser-check", "--start-maximized", GEMINI_URL,
    ])
    for _ in range(60):
        if _port_open(DEBUG_PORT):
            return proc
        time.sleep(0.5)
    raise RuntimeError("Chrome started but did not open its debug port")


# ── page helpers ─────────────────────────────────────────────────────────────
async def _first_visible(page, selectors, timeout_s):
    end = time.time() + timeout_s
    while time.time() < end:
        for sel in selectors:
            loc = page.locator(sel)
            try:
                n = await loc.count()
                if n and await loc.nth(n - 1).is_visible():
                    return loc.nth(n - 1)
            except Exception:
                pass
        await asyncio.sleep(1)
    return None


async def _wait_for_input(page, player, first: bool):
    box = await _first_visible(page, INPUT_SELECTORS, 20)
    if box or not first:
        return box
    _log(player, "Please sign in to Google in the Chrome window that opened. I'll wait.")
    end = time.time() + SIGN_IN_WAIT_S
    while time.time() < end and not _state["stop"]:
        if "gemini.google.com" not in page.url:
            await asyncio.sleep(3)
            continue
        box = await _first_visible(page, INPUT_SELECTORS, 5)
        if box:
            return box
    return None


async def _wait_for_image(page):
    """Wait for a finished generated image. Returns the locator, 'limit', or None."""
    end = time.time() + IMAGE_WAIT_S
    while time.time() < end and not _state["stop"]:
        img = await _first_visible(page, IMAGE_SELECTORS, 2)
        if img:
            try:
                ok = await img.evaluate("el => el.complete && el.naturalWidth >= 256")
                if ok:
                    await asyncio.sleep(1.5)   # let the full-size version swap in
                    return img
            except Exception:
                pass
        try:
            last = page.locator("model-response").last
            if await last.count():
                text = (await last.inner_text()).lower()
                if any(w in text for w in LIMIT_WORDS):
                    return "limit"
        except Exception:
            pass
        await asyncio.sleep(2)
    return None


async def _grab_image(page, img) -> bytes:
    """Full-size download first; then the image's own bytes; last a screenshot."""
    try:
        await img.hover()
        await asyncio.sleep(0.5)
        btn = await _first_visible(page, DOWNLOAD_SELECTORS, 5)
        if btn:
            async with page.expect_download(timeout=60_000) as dl_info:
                await btn.click()
            dl = await dl_info.value
            path = await dl.path()
            if path:
                data = Path(path).read_bytes()
                if data:
                    return data
    except Exception as e:
        print(f"[GeminiImages] download button failed, using the image itself: {e}")
    try:
        data_url = await img.evaluate("""async el => {
            const r = await fetch(el.currentSrc || el.src);
            const b = await r.blob();
            return await new Promise(res => { const f = new FileReader();
                f.onload = () => res(f.result); f.readAsDataURL(b); });
        }""")
        return base64.b64decode(data_url.split(",", 1)[1])
    except Exception as e:
        print(f"[GeminiImages] fetching the image failed, taking a screenshot: {e}")
    return await img.screenshot()


# ── the batch ────────────────────────────────────────────────────────────────
async def _run_batch(jobs, out_dir: Path, player):
    from playwright.async_api import async_playwright

    proc = _start_chrome()
    try:
        await _run_jobs(jobs, out_dir, player, async_playwright)
    finally:
        # The Chrome this run started (with its debug port) is closed whatever happened.
        if proc and proc.poll() is None:
            proc.terminate()


async def _run_jobs(jobs, out_dir: Path, player, async_playwright):
    fails_in_a_row = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        prev = ctx.pages[0] if ctx.pages else None

        for i, job in enumerate(jobs):
            if _state["stop"]:
                _log(player, "Stopped as asked.")
                break
            name = job["name"]
            _state["current"] = name
            _log(player, f"[{i + 1}/{len(jobs)}] {name}")

            # New tab first, then close the old one, so the window never closes.
            page = await ctx.new_page()
            if prev and not prev.is_closed():
                await prev.close()
            prev = page

            try:
                await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=60_000)
                box = await _wait_for_input(page, player, first=(i == 0))
                if not box:
                    raise RuntimeError("could not find Gemini's message box (signed in?)")
                await box.click()
                await page.keyboard.insert_text("Create an image. " + job["prompt"])
                await asyncio.sleep(0.5)
                await page.keyboard.press("Enter")

                img = await _wait_for_image(page)
                if img == "limit":
                    _log(player, "Gemini says the daily image limit is reached. "
                                 "Run it again tomorrow and it will carry on.")
                    _state["failed"].append(name)
                    break
                if img is None:
                    raise RuntimeError("no image appeared in time")

                _save_square(await _grab_image(page, img), out_dir / f"{name}.png")
                _state["done"] += 1
                fails_in_a_row = 0
            except Exception as e:
                fails_in_a_row += 1
                _state["failed"].append(name)
                _log(player, f"  {name} failed: {e}")
                try:
                    dbg = out_dir / "_debug"
                    dbg.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(dbg / f"{name}.png"))
                except Exception:
                    pass
                if fails_in_a_row >= MAX_FAILS_IN_A_ROW:
                    _log(player, f"{MAX_FAILS_IN_A_ROW} failures in a row, stopping. "
                                 f"Screenshots are in {out_dir / '_debug'}")
                    break
            await asyncio.sleep(3)   # don't hammer Gemini

        try:
            await browser.close()
        except Exception:
            pass


def _worker(jobs, out_dir, player):
    try:
        asyncio.run(_run_batch(jobs, out_dir, player))
    except Exception as e:
        _log(player, f"Image batch crashed: {e}")
    finally:
        failed = _state["failed"]
        msg = f"Images finished: {_state['done']} made, {len(failed)} failed. Saved in {out_dir}."
        if failed:
            msg += " Failed: " + ", ".join(failed)
        if _state.get("left_after"):
            msg += f" {_state['left_after']} more still to make - run it again for the next batch."
        _log(player, msg)
        _state["running"] = False
        _state["current"] = ""


def _start(folder: str, only: str, redo: bool, player, background=True,
           count: int = BATCH_SIZE, subfolder: str = "") -> str:
    with _lock:
        if _state["running"]:
            return "The image batch is already running. " + _status()
        try:
            jobs, out_dir = _load_jobs(Path(folder), only, redo, subfolder)
        except Exception as e:
            return f"Sir, I couldn't start the images: {e}"
        if not jobs:
            return f"Every image already exists in {out_dir}. Nothing to make."
        left_after = max(0, len(jobs) - count) if count > 0 else 0
        if count > 0:
            jobs = jobs[:count]
        _state["left_after"] = left_after
        _state.update(running=True, stop=False, done=0, total=len(jobs), failed=[],
                      current="", note="", out_dir=str(out_dir))
    if not background:
        _worker(jobs, out_dir, player)
        return _state["note"]
    threading.Thread(target=_worker, args=(jobs, out_dir, player), daemon=True,
                     name="GeminiImages").start()
    return (f"Started making {len(jobs)} images in Gemini. A Chrome window will open; "
            f"if it asks, sign in to Google there once. They'll be saved in {out_dir}.")


def _status() -> str:
    s = _state
    if not s["running"]:
        return (f"No image batch running. Last run: {s['done']} made, "
                f"{len(s['failed'])} failed." if s["total"] else "No image batch has run yet.")
    return (f"{s['done']} of {s['total']} images done, {len(s['failed'])} failed. "
            f"Working on: {s['current'] or 'starting up'}.")


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "start").lower().strip()
    try:
        if action == "status":
            return _status()
        if action == "stop":
            if not _state["running"]:
                return "No image batch is running."
            _state["stop"] = True
            return "Stopping after the current image."
        try:
            count = int(p.get("count") or BATCH_SIZE)
        except (TypeError, ValueError):
            count = BATCH_SIZE
        return _start(p.get("folder") or DEFAULT_FOLDER, p.get("only") or "",
                      bool(p.get("redo")), player, count=count,
                      subfolder=str(p.get("subfolder") or "").strip())
    except Exception as e:
        return f"Sir, gemini_images failed: {e}"


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Make images in the Gemini web app")
    ap.add_argument("folder", nargs="?", default=DEFAULT_FOLDER)
    ap.add_argument("--only", nargs="+", default=[])
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--count", type=int, default=BATCH_SIZE,
                    help="images this run (default 10, 0 = all)")
    ap.add_argument("--subfolder", default="", help="folder inside images/ for this batch")
    a = ap.parse_args()
    print(_start(a.folder, " ".join(a.only), a.redo, None, background=False, count=a.count,
                 subfolder=a.subfolder))
