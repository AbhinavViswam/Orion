"""
The laptop's own brain: Gemma 4 E2B (4-bit) through Ollama, like JARVIS's on-device brain.

ORION_LOCAL_BRAIN_PLAN.md has the why. In short:
    * Brain modes (⚙ › Brain): Auto (Gemini, and this when Gemini fails), On-device ("local":
      nothing goes to any cloud AI) and Gemini only. "My own setup" still overrides all three.
    * Ollama is started by Orion when the local brain is needed and stopped when Gemini is
      back, so the model holds no memory while Gemini is doing the work.
    * Measured on this laptop (P0, 2026-10-30): the whole model fits the 4 GB graphics card
      only with num_gpu 99 (Ollama's own guess put half of it in RAM); thinking must be off;
      first word 0.5 s, ~42 words a second, 12 of 12 tool tests right.
    * Fast commands answer first, then the "limited tool access" check, then Gemma with a
      small tool set and a short prompt.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

HOST = "http://127.0.0.1:11434"
BASE_MODEL = "hf.co/unsloth/gemma-4-E2B-it-GGUF:Q4_K_M"   # what is downloaded (~3.1 GB)
MODEL = "orion-local"                                      # BASE_MODEL with Orion's settings saved
# Saved into MODEL so they reach Ollama through the OpenAI-style API, which has no "options".
PARAMS = {"num_gpu": 99, "num_ctx": 4096, "temperature": 0.3}
DOWNLOAD_GB = 3.1
LLM_ID = "openai_compat"
STT_ID = "whisper"
TTS_ID = "piper"
VOICE = "en_US-ryan-high"       # used if nothing else decides; ~60-110 MB, downloaded once
# The Piper voice nearest each Gemini voice, used until one is picked in ⚙ › Voice.
LIKE_GEMINI = {
    "Charon": "en_US-ryan-high",      # deep, calm man
    "Puck": "en_US-joe-medium",       # upbeat man
    "Fenrir": "en_US-bryce-medium",   # younger, livelier man
    "Kore": "en_US-lessac-high",      # firm, clear woman
    "Aoede": "en_US-amy-medium",      # light, breezy woman
}
# The English voices offered in ⚙ › Voice (Piper has 38; these are the good ones).
LOCAL_VOICES = (
    ("en_US-ryan-high", "Ryan", "American man · deep, clear · best quality"),
    ("en_US-joe-medium", "Joe", "American man · relaxed"),
    ("en_US-bryce-medium", "Bryce", "American man · younger"),
    ("en_US-john-medium", "John", "American man"),
    ("en_US-norman-medium", "Norman", "American man · narrator"),
    ("en_GB-alan-medium", "Alan", "British man"),
    ("en_GB-northern_english_male-medium", "Northern", "Northern English man"),
    ("en_US-lessac-high", "Lessac", "American woman · clear · best quality"),
    ("en_US-amy-medium", "Amy", "American woman · bright"),
    ("en_US-kristin-medium", "Kristin", "American woman · warm"),
    ("en_GB-cori-high", "Cori", "British woman · best quality"),
    ("en_GB-jenny_dioco-medium", "Jenny", "British woman"),
)


def LocalVoice() -> str:
    """The Piper voice the laptop speaks with: the one picked in ⚙ › Voice, else
    one chosen under My own setup, else the nearest to the Gemini voice."""
    from Brain.Settings import GetLocalVoice, GetProviderConfig, GetVoice
    picked = GetLocalVoice()
    if picked:
        return picked
    own = (GetProviderConfig(TTS_ID).get("voice") or "").strip()
    return own or LIKE_GEMINI.get(GetVoice(), VOICE)
LIMITED = "I have limited tool access right now, so I can't do that."
ON_LOCAL = "I'm on the laptop's own brain for now."

# What the local brain is given, each cut to a short description (plan §5). Everything else
# (browser, dev agent, flights, code, screen, files processing, messages...) gets LIMITED.
TOOLS: dict[str, str] = {
    "open_app": "Open an app, website or folder on the laptop.",
    "computer_settings": "Change the laptop: volume, mute, brightness, pause or play media, windows, "
                         "screenshots, power and similar. Name the action in 'action'.",
    "reminder": "Set a reminder at a date and time.",
    "video_player": "Play a video on Orion's own screen (a YouTube link, a file or a search), or stop it.",
    "save_memory": "Remember a fact about the user.",
    "undo": "Undo the last thing done.",
    "web_search": "Search the web for current facts, news or prices.",
    "weather_report": "The weather for a city.",
    "file_controller": "Find, open, list, create, move or delete files and folders.",
    "ask_phone": "The user's PHONE (not this laptop): its battery, ring it, find where the phone is "
                 "(action where), its messages.",
    "phone_link": "Send a link, text or file to the user's phone.",
    "system_status": "This laptop's own CPU, memory, disk and battery. For the phone, use ask_phone.",
}

# Asks the local brain cannot serve: answered with LIMITED at once rather than letting a small
# model guess. Kept narrow on purpose; anything else goes to the model.
_CANT = re.compile(
    r"\b((what'?s|what is|read|see) on (my|the) screen|look at (my|the|this)|camera|webcam"
    r"|what (do|can) you see"
    r"|flights?|book (a|me)|write (a |some )?(code|program|script)|build (an?|the) (app|website|project)"
    r"|dev agent|forge|repair yourself|fix yourself"
    r"|(summari[sz]e|read|translate|convert|edit) (this|that|the|my) (file|document|pdf|spreadsheet|image|video)"
    r"|send (a |an )?(whatsapp|telegram|message|email|text) to"
    r"|in (the )?browser|fill (in )?(the|this) form|click (on )?)\b", re.I)

ACTIVE = False                  # True while the local brain holds the conversation (top-bar badge)
_proc: Optional[subprocess.Popen] = None     # the Ollama server, when Orion started it
_lock = threading.Lock()
_installed: Optional[bool] = None             # cached answer of Installed()


class NotReady(RuntimeError):
    """The local brain can't start (not downloaded, Ollama missing...). The text is for the user."""


# ── modes ────────────────────────────────────────────────────────────────────

def Mode() -> str:
    """auto | local | gemini, or "pipeline" while My own setup is chosen."""
    try:
        from Brain.Settings import GetBrainMode, GetProviderMode
        if GetProviderMode() == "pipeline":
            return "pipeline"
        return GetBrainMode()
    except Exception:
        return "auto"


def UseLocal(fallback: bool) -> bool:
    """Should the local brain answer now? `fallback`: Auto mode has switched to it."""
    mode = Mode()
    return mode == "local" or (mode == "auto" and fallback)


def CanFallBack() -> bool:
    """Auto mode, and the model is downloaded, so a Gemini failure can switch to it."""
    return Mode() == "auto" and Installed()


# ── Ollama ───────────────────────────────────────────────────────────────────

def OllamaExe() -> str:
    found = shutil.which("ollama")
    if found:
        return found
    guess = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return str(guess) if guess.is_file() else ""


# Plain urllib with no proxy, not httpx: these run on the interface thread too (the Brain page
# asks whether the model is downloaded), and an httpx client there crashed the app in tests.
_NO_PROXY = None


def _Open(req, timeout: float):
    global _NO_PROXY
    import urllib.request
    if _NO_PROXY is None:
        _NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _NO_PROXY.open(req, timeout=timeout)


def _Get(path: str, timeout: float = 2.0):
    with _Open(HOST + path, timeout) as r:
        return json.loads(r.read() or b"{}")


def _Post(path: str, body: dict, timeout: float = 30.0):
    import urllib.request
    req = urllib.request.Request(HOST + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with _Open(req, timeout) as r:
        data = r.read()
        return json.loads(data) if data.strip() else {}


def Running() -> bool:
    try:
        _Get("/api/version", 1.0)
        return True
    except Exception:
        return False


def StartServer(wait_s: float = 20.0) -> None:
    """Start Ollama if it isn't running. Raises NotReady when it can't be."""
    global _proc
    if Running():
        return
    exe = OllamaExe()
    if not exe:
        raise NotReady("Ollama isn't installed, so the laptop's own brain can't run. "
                       "Get it from ollama.com, then try again.")
    env = dict(os.environ)
    env.setdefault("OLLAMA_KEEP_ALIVE", "-1")   # stays loaded until Release(); no 23 s reload mid-chat
    with _lock:
        if _proc is None or _proc.poll() is not None:
            _proc = subprocess.Popen([exe, "serve"], env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if Running():
            return
        time.sleep(0.3)
    raise NotReady("Ollama didn't start. Try again in a minute.")


def ModelNames() -> set[str]:
    try:
        return {str(m.get("name") or m.get("model") or "") for m in _Get("/api/tags").get("models", [])}
    except Exception:
        return set()


def _Has(names: set[str], model: str) -> bool:
    return model in names or f"{model}:latest" in names


def Installed(refresh: bool = False) -> bool:
    """Is the model downloaded? Read from Ollama's manifest folder on disk, never by asking the server:
    the Brain page asks this on the interface thread, and a connection attempt to a stopped Ollama
    from there crashed the app in tests (2026-10-30)."""
    global _installed
    if _installed is not None and not refresh:
        return _installed
    root = Path(os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama" / "models") / "manifests"
    repo, _, tag = BASE_MODEL.partition(":")
    _installed = (root / Path(*repo.split("/")) / tag).is_file() \
        or (root / "registry.ollama.ai" / "library" / MODEL / "latest").is_file()
    return _installed


def EnsureModel() -> None:
    """Make MODEL if it isn't there yet: BASE_MODEL's text part only, with PARAMS saved.

    The download also carries a ~1 GB picture-and-sound part that Orion never uses. Left in, it was
    loaded anyway: measured 2026-10-30, 3.4 GB of RAM reserved and 2.7 GB on the graphics card, against
    1.9 GB and 1.6 GB without it (and a 9-12 s load instead of 17 s)."""
    names = ModelNames()
    if _Has(names, MODEL) and "vision" not in _Show(MODEL).get("capabilities", []):
        return
    if not _Has(names, BASE_MODEL):
        raise NotReady("The laptop's own brain isn't downloaded yet. "
                       "⚙ › Brain › On-device has the download.")
    # Through `ollama create -f` with a recipe file: the API's own create refuses this template.
    import tempfile
    recipe = Path(tempfile.gettempdir()) / "orion-local.Modelfile"
    recipe.write_text(TextOnlyRecipe(_Show(BASE_MODEL)), encoding="utf-8")
    done = subprocess.run([OllamaExe() or "ollama", "create", MODEL, "-f", str(recipe)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=300,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if done.returncode != 0:
        raise NotReady("Setting up the laptop's own brain failed: " + (done.stderr or done.stdout).strip()[-160:])


def _Show(model: str) -> dict:
    try:
        return _Post("/api/show", {"model": model}, timeout=30)
    except Exception:
        return {}


def TextOnlyRecipe(show: dict) -> str:
    """MODEL's recipe (Modelfile) from BASE_MODEL's /api/show answer: everything in it except the
    smaller FROM file (the picture-and-sound part; the largest is the language model), plus PARAMS."""
    lines = str(show.get("modelfile") or "").splitlines()
    froms = []
    for i, line in enumerate(lines):
        if line.startswith("FROM ") and "sha256-" in line:
            path = Path(line[5:].strip())
            froms.append((path.stat().st_size if path.is_file() else 0, i))
    if not froms:
        raise NotReady("The laptop's own brain looks damaged. Download it again in ⚙ › Brain.")
    keep = max(froms)[1]
    drop = {i for _, i in froms if i != keep}
    out = [line for i, line in enumerate(lines) if i not in drop
           and not any(line.startswith(f"PARAMETER {k} ") for k in PARAMS)]
    out += [f"PARAMETER {k} {v}" for k, v in PARAMS.items()]
    return "\n".join(out) + "\n"


def Download(progress: Callable[[str], None]) -> None:
    """Download the model (~3.1 GB) with progress lines. Raises on failure."""
    import urllib.request
    global _installed
    StartServer()
    last = 0.0
    req = urllib.request.Request(HOST + "/api/pull", json.dumps({"model": BASE_MODEL, "stream": True}).encode(),
                                 {"Content-Type": "application/json"})
    with _Open(req, 3600) as r:
        for line in r:
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get("error"):
                raise RuntimeError(d["error"])
            total, done = d.get("total") or 0, d.get("completed") or 0
            if total > 50_000_000 and time.monotonic() - last > 0.5:
                last = time.monotonic()
                progress(f"Downloading… {done / 1e9:.1f} of {total / 1e9:.1f} GB "
                         f"({done * 100 // total}%)")
    progress("Setting it up…")
    EnsureModel()
    _installed = True


def Prepare() -> None:
    """Get everything ready for a local session. Blocking; run it off the event loop.
    Raises NotReady with a sentence for the user."""
    from importlib.util import find_spec
    if not Installed(refresh=True) and not Running():
        raise NotReady("The laptop's own brain isn't downloaded yet. "
                       "⚙ › Brain › On-device has the download.")
    StartServer()
    EnsureModel()
    missing = [p for p, mod in (("faster-whisper", "faster_whisper"), ("piper-tts", "piper"))
               if find_spec(mod) is None]
    if missing:
        raise NotReady("The laptop's own brain needs " + " and ".join(missing)
                       + " (pip install " + " ".join(missing) + ").")


def Release() -> None:
    """Free the graphics card: unload the model, and stop Ollama if Orion started it."""
    global _proc
    try:
        if Running():
            _Post("/api/generate", {"model": MODEL, "keep_alive": 0}, timeout=10)
    except Exception:
        pass
    with _lock:
        if _proc is not None and _proc.poll() is None:
            try:
                if os.name == "nt":
                    # `ollama serve` runs the model in a child process: end the whole tree.
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(_proc.pid)], capture_output=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                else:
                    _proc.terminate()
                _proc.wait(5)
            except Exception:
                pass
        _proc = None


# ── what the engine is built from ────────────────────────────────────────────

def LlmConfig() -> dict:
    return {
        "base_url": HOST + "/v1", "model": MODEL, "api_key": "",
        "max_tools": len(TOOLS), "max_tool_chars": 9000,
        "max_tokens": 200, "timeout": 90, "warm": True,
        "extra_body": {"reasoning_effort": "none"},
        # Text only: stored as already measured, so no "can it see?" check is run.
        "vision": False, "vision_model": MODEL,
    }


def SttConfig() -> dict:
    from Brain.Settings import GetProviderConfig
    cfg = {"backend": "local", "model": "base", "device": "cpu", "language": "",
           "base_url": "", "api_key": ""}
    own = GetProviderConfig(STT_ID)        # a model or language chosen under My own setup is kept
    if own.get("backend") == "local":
        cfg.update({k: own[k] for k in ("model", "language", "download_root") if own.get(k)})
    return cfg


def TtsConfig() -> dict:
    from Brain.Settings import GetProviderConfig
    own = GetProviderConfig(TTS_ID)
    return {"voice": LocalVoice(), "follow_language": own.get("follow_language", "1"),
            "speed": own.get("speed") or 1.0}


def ShortTools(decls: list) -> list:
    """The small tool set, in TOOLS order, each with its short description and
    shortened argument notes."""
    by_name = {}
    for d in decls:
        name = d.get("name") if isinstance(d, dict) else getattr(d, "name", "")
        if name in TOOLS and isinstance(d, dict):
            by_name[name] = d
    out = []
    for name, desc in TOOLS.items():
        d = by_name.get(name)
        if d is None:
            continue
        slim = {k: v for k, v in d.items() if k not in ("brief", "tier", "behavior", "scheduling")}
        slim["description"] = desc
        slim["parameters"] = _ShortParams(d.get("parameters") or {})
        out.append(slim)
    return out


def _ShortParams(schema: dict) -> dict:
    out = dict(schema)
    props = {}
    for key, p in (schema.get("properties") or {}).items():
        p = dict(p)
        text = " ".join(str(p.get("description") or "").split())
        if " | " in text:
            # A list of choices ("volume_up | volume_down | ..."): the list is the useful part.
            start = text.rfind(":", 0, text.find(" | "))
            first = text[start + 1:].strip() if start >= 0 else text
            limit = 420
        else:
            first = re.split(r"(?<=[.;])\s", text, maxsplit=1)[0]
            limit = 160
        if len(first) > limit:
            first = first[:limit - 3].rstrip() + "…"
        if first:
            p["description"] = first
        props[key] = p
    if props:
        out["properties"] = props
    return out


# A few phone asks answered without the model, like the phone's own rule matcher: the small model
# sometimes gave "where is my phone" the limited-tool line in a running conversation.
_PHONE_RULES = (
    (re.compile(r"\b(where('?s| is| did i (leave|put))|find|locate) my (phone|mobile)\b", re.I), "where"),
    (re.compile(r"\b(ring|call) my (phone|mobile)\b", re.I), "ring"),
    (re.compile(r"\bbattery\b.*\bmy (phone|mobile)\b|\bmy (phone|mobile)\b.*\bbattery\b", re.I), "battery"),
)


def PhoneRule(text: str) -> Optional[dict]:
    """ask_phone's arguments when `text` is one of the plain phone asks above, else None."""
    for pattern, action in _PHONE_RULES:
        if text and pattern.search(text):
            return {"action": action}
    return None


def PhoneReply(action: str, result: str) -> str:
    """What to say for a PhoneRule answer: ask_phone's own words ("Ringing your phone", the
    phone's location...), the battery as a sentence, or why it failed."""
    result = " ".join(str(result or "").split())
    if result.startswith("status="):
        m = re.search(r'reason="([^"]+)"', result)
        return f"That didn't work: {m.group(1)}." if m else "That didn't work."
    if action == "battery" and re.match(r"^\d{1,3}%", result):
        return f"Your phone is at {result}."
    return result or "Done."


def LimitedReply(text: str) -> Optional[str]:
    """LIMITED when `text` asks for something only Gemini's tools can do, else None."""
    return LIMITED if text and _CANT.search(text) else None


def Prompt(name: str, creator: str, user_name: str, memory: dict | None, now: datetime | None = None) -> str:
    """The local brain's short system prompt (plan §5: about 700 tokens). The parts that
    change (memory, then the time) come last, so the server can keep the rest cached."""
    who = f"Call the user {user_name}." if user_name else 'Call the user "sir".'
    lines = [
        f"You are {name}, a voice assistant on the user's laptop, built by {creator}. "
        f"Right now you run on the laptop's own small model, not Gemini.",
        "Speak in one to three short sentences, plainly, with no lists or markdown, "
        "in the user's language. Answer questions from your own knowledge.",
        "Use a tool only when the user asks you to do something. If none of your tools can do it, "
        f'say exactly: "{LIMITED}"',
        who,
    ]
    facts = _NewestFacts(memory, 6)
    if facts:
        lines.append("What you know about the user:\n" + "\n".join(f"- {f}" for f in facts))
    rules = _NewestRules(memory, 3)
    if rules:
        lines.append("Rules the user taught you (follow them):\n" + "\n".join(f"- {r}" for r in rules))
    now = now or datetime.now()
    lines.append("It is " + now.strftime("%A, %d %B %Y, %I:%M %p").replace(" 0", " ") + ".")
    return "\n\n".join(lines)


def _NewestFacts(memory: dict | None, n: int) -> list[str]:
    if not memory:
        return []
    rows = []
    for cat, items in memory.items():
        if cat == "rules" or not isinstance(items, dict):
            continue
        for key, e in items.items():
            if isinstance(e, dict) and e.get("value") and not e.get("deleted"):
                rows.append((str(e.get("updated") or ""), cat == "identity",
                             f"{key.replace('_', ' ')}: {str(e['value'])[:120]}"))
    rows.sort(key=lambda r: (r[1], r[0]), reverse=True)    # who they are first, then newest
    return [r[2] for r in rows[:n]]


def _NewestRules(memory: dict | None, n: int) -> list[str]:
    try:
        from Brain import Rules
        return [str(r["value"]) for r in Rules.All(memory)[:n]]
    except Exception:
        return []
