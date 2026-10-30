import os as _os

# ── RAM: one thread for numpy's maths library ────────────────────────────────
# OpenBLAS reserves a buffer per CPU thread the moment numpy is imported:
# 363 MB on this 12-thread laptop, measured, for sums small enough that one
# thread does them just as fast (10 MB). Must be set before numpy loads.
_os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import platform as _platform
import subprocess as _subprocess

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **                       kw)

    _subprocess.Popen = _Popen


# ── Console must survive non-UTF-8 code pages ────────────────────────────────
# Every status line in this file carries an emoji, and on a legacy Windows
# console the active code page is the system one — cp1254 in Turkey, cp1251 in
# Russia, cp932 in Japan. Printing an emoji there raises UnicodeEncodeError, and
# because most of these prints sit inside the receive loop it takes the session
# down on startup. Reconfiguring to UTF-8 with a replacement fallback costs
# nothing and makes the app launch the same way in every locale.
import sys as _sys

for _stream in ("stdout", "stderr"):
    try:
        _s = getattr(_sys, _stream, None)
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass          # pythonw / redirected pipes / anything exotic — never fatal

# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import re
import threading
import time
import json
import sys
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import sounddevice as sd
import numpy as np
from google import genai
from google.genai import types
from Interface.Window import OrionUI
from Brain.Memory import LoadMemory, UpdateMemory, FormatMemoryForPrompt, SaveSessionSummary, PopLastSession, SearchMemory, SetTrimNotifier

# The file-backed tools (open_app, web_search, browser_control, …) are no longer
# imported or declared here — they self-describe via a TOOL dict in their own
# actions/*.py file and are auto-discovered by core.action_loader at startup.
# Only tools that are tied to live-session state stay inline in this file
# (screen_process, close_camera, save_memory, manage_monitor, shutdown_orion,
# system_status).
from Brain import Outcome
from Brain import Usage
from System.ActionLoader import StripMeta
from Actions.ScreenProcessor import CaptureCamera, CaptureScreen
from Actions.SystemMonitor import SystemMonitor, GetSystemStatus
from Actions.Proactive import ProactiveEngine
from Actions.BackgroundMonitor import AddMonitor, RemoveMonitor, ListMonitors, CheckAll as monitor_check_all
from Actions.WebSearch import News as _fetch_news_sync
from System import FastCommands, SelfRepair, SkillForge
from System import AlertBus, PhoneBridge
from System import CrashWatch
from System import LocalBrain
from Brain import Privacy
from System.ClipboardWatch import ClipboardWatch
from Brain.Settings import GetFastCommandsEnabled
from Brain.Settings import GetBriefEnabled, GetMediaResolution, GetProactiveAudioEnabled, GetPushToTalkEnabled, GetThinkingEnabled, GetTurnTuning, GetVoice, GetInputDevice, GetOutputDevice
from Brain import Gemini as _gemini
from System.PluginLoader import DiscoverPlugins
from Brain import Undo as undo_stack
from Brain import Confirm as confirm_gate
from Voice import AudioDevices
from System.ActionLoader import DiscoverActions
from Voice.Echo import EchoGuard

def GetBaseDir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR        = GetBaseDir()
API_CONFIG_PATH = BASE_DIR / "Config" / "ApiKeys.json"
PROMPT_PATH     = BASE_DIR / "Brain" / "Prompt.txt"

# Who built this assistant. Told to the model in [IDENTITY] and shown in the HUD footer.
CREATOR = "Abhinav Viswam"
# The conversation's model. A NAME, not a decision: the ladder lives in
# core/gemini.py and this is only whichever rung is currently in use, kept here
# as a module attribute because plugins read it (chat_takeover asks main for it
# so that upgrading the assistant upgrades the plugin too).
#
# It is reassigned on every connect, so a model that runs out of quota is
# stepped over and the assistant keeps talking instead of failing to start.
LIVE_MODEL          = "models/gemini-3.1-flash-live-preview"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000 
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

# RMS below which 16-bit PCM is treated as room silence; above _LEVEL_FULL it
# reads as a full-height waveform. Tuned so ordinary speech lands mid-range and
# the bars still move for a quiet talker — language- and device-independent.
_LEVEL_FLOOR = 60.0
_LEVEL_FULL  = 2600.0


def PcmLevel(samples) -> float:
    """Map a block of int16 PCM samples to a 0.0–1.0 loudness level for the HUD
    waveform. Returns 0.0 on empty/invalid input so it can never raise."""
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(x * x)))
    except Exception:
        return 0.0
    if rms <= _LEVEL_FLOOR:
        return 0.0
    return min(1.0, (rms - _LEVEL_FLOOR) / (_LEVEL_FULL - _LEVEL_FLOOR))


# ── Voice level schedule ──────────────────────────────────────────────────────
# Orion's own voice is handed to the HUD as a level per 20 ms slice, so the
# reactor moves in step with what is coming out of the speakers.
# Extra time beyond the device's reported output latency before the microphone
# is trusted again: covers room decay and the speaker's own settling.
_TAIL_MARGIN = 0.25

# How long a sent turn may go unanswered before the session is presumed dead.
#
# A Live session can go half-dead: the server stops answering but the socket is
# never closed, so send_client_content() still succeeds — it is a write into a
# buffer — and `session.receive()` simply awaits forever. Nothing raises. The
# user's message reaches the activity log, the HUD stays on LISTENING, the
# terminal prints nothing, and the assistant is gone until it is restarted.
# That was the single worst failure this app could have, because it looked
# exactly like the model choosing not to reply.
#
# The threshold is turn-scoped rather than idle-scoped, and measured rather than
# guessed. An idle session receives NOTHING: watched for 150 s after a reply,
# not one frame arrived unprompted, so "silence" cannot mean "dead". But an
# ANSWERED turn is fast — first audio at 1.2 s, turn_complete at 3.6 s. Thirty
# seconds is twenty-five times the measured first response, which leaves room
# for a slow tool call (those send their own traffic, which clears the deadline)
# while turning an unbounded hang into a blip.
_STALL_LIMIT = 30.0

_VIS_HOP = 480         # 20 ms between frames at 24 kHz

# Delay from handing the first bytes of a reply to an already-running output
# stream to hearing them: one callback period, plus whatever the DAC adds.
_FIRST_SOUND = CHUNK_SIZE / RECEIVE_SAMPLE_RATE      # ~43 ms
# How far past the device's own buffer the level timeline may drift before it
# is re-anchored. The buffer is the hard limit on how much audio can be queued
# ahead, so anything beyond it plus a margin for clock error is impossible.
_CURSOR_SLACK = 0.15

# Speech that no model turn ends (the private voice, announcements) counts as
# over this long after its last sound has played.
_QUIET_END = 1.2

# Erring early is the safe direction: motion slightly before the sound reads
# better than motion after it, so where this is uncertain it is biased to lead.


def PcmVisemes(samples, sr: int = 24000):
    """Slice a PCM block into (level, 0, 0) frames, one per 20 ms.

    Returns [] on anything unexpected; the caller then falls back to one level.
    """
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size < _VIS_HOP:
            return []
        return [(PcmLevel(x[i:i + _VIS_HOP]), 0.0, 0.0)
                for i in range(0, x.size, _VIS_HOP)]
    except Exception:
        return []


def DescribeTools(declarations) -> str:
    """One line per capability, straight from the live tool declarations.

    Derived rather than written down: the action and plugin registries are
    discovered at startup, so whatever the user has installed is what the model
    is told it can do. Adding a plugin extends this by itself, and removing one
    stops the model from claiming an ability it no longer has.
    """
    lines = []
    for d in declarations or ():
        try:
            name = d.get("name") if isinstance(d, dict) else getattr(d, "name", None)
            desc = (d.get("description") if isinstance(d, dict)
                    else getattr(d, "description", "")) or ""
        except Exception:
            continue
        if not name:
            continue
        desc = " ".join(str(desc).split())
        lines.append(f"- {name}: {desc[:150]}" if desc else f"- {name}")
    return "\n".join(lines)


def DescribeLimits(has_vision: bool, has_mic: bool) -> str:
    """The other half of self-knowledge: what is out of reach, and why.

    Derived from how the program is actually built, not from a list of refusals.
    A model that knows its boundaries stops improvising around them, and stating
    them as architecture rather than as rules keeps the answer honest in any
    language.
    """
    out = [
        "- Anything not listed above is outside your reach. Say so in one clause "
        "and offer the nearest thing you can actually do — never mime an action "
        "you cannot take, and never report a result you did not get.",
        "- You act on this machine only. You cannot reach the user's other "
        "devices, accounts or hardware except through the tools listed above.",
        "- You remember what is in the memory block and what has been said this "
        "session. Anything else you were told before is gone unless it was saved.",
    ]
    # What the PLATFORM forbids, before what the build lacks.
    #
    # On Wayland, and on a macOS without Accessibility, the keyboard, mouse and
    # screen are simply not available to any program — and the APIs report
    # success anyway. A model that does not know this reports clicks that never
    # happened, which is how "it does not work properly on Linux" was described
    # for a long time without anybody being able to name the failing part.
    try:
        from System import PlatformSupport
        out.extend(PlatformSupport.DescribeForModel(PlatformSupport.detect()))
    except Exception as _e:
        print(f"[Limits] platform capabilities unavailable: {_e}")

    if has_vision:
        out.append(
            "- Your sight is not continuous. You see nothing until you call a "
            "vision tool, and then only that single frame at that moment — you "
            "cannot watch, monitor or notice something changing on screen.")
    else:
        out.append("- You have no sight at all in this build.")
    if has_mic:
        out.append(
            "- You hear nothing while the microphone is muted, and you cannot "
            "unmute it yourself.")
    return "\n".join(out)


def RenderPrompt(template: str, values: dict) -> str:
    """Fill {tokens} in the prompt template.

    A plain replace rather than str.format: the file is meant to be edited by
    hand, and a stray brace in someone's own wording must never take the app
    down at startup.
    """
    out = template or ""
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    return out


def GetApiKey() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def LoadSystemPrompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are Orion, Abhinav Viswam's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

# Transcript chunks shorter than this may legitimately repeat ("evet, evet"),
# so only longer ones are treated as duplicates.
_REPEAT_MIN = 12


def IsRepeatChunk(txt: str, buf: list) -> bool:
    """True if this transcript chunk has already been seen this turn.

    Guards against the API re-sending the tail of a response across the several
    turn_completes a tool-using turn produces.
    """
    if len(txt) < _REPEAT_MIN:
        return bool(buf) and txt == buf[-1]
    joined = " ".join(buf)
    return txt in joined

def CleanTranscript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()

TOOL_DECLARATIONS = [
    # ── Inline tools ─────────────────────────────────────────────────────────
    # These stay here (rather than in an actions/*.py TOOL dict) because their
    # handling is woven into live-session state — vision capture/injection,
    # camera stream, memory writes, the monitor engine, and shutdown. All other
    # tools live in their own action file and are auto-discovered by
    # core.action_loader (see OrionLive.__init__).
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures the screen or webcam image and lets you analyze it. "
            "MUST be called when user asks what is on screen, what you see, "
            "look at camera, analyze my screen, etc. "
            "You have NO visual ability without this tool. "
            "After the image is captured it is sent directly to you — describe what you see and answer the user's question. "
            "When using camera: the live view stays open until user says close it or calls close_camera."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "close_camera",
        "description": (
            "Closes the live camera view shown on screen. "
            "Call when the user says (in ANY language): close camera, stop camera, "
            "turn off camera, that's creepy, etc."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "manage_monitor",
        "description": (
            "Add, remove, or list background monitoring topics. "
            "ORION checks these topics once a day and alerts the user when there is a new development. "
            "Use 'add' when the user says 'monitor X', 'track X', 'follow X'. "
            "Use 'remove' when the user says 'stop monitoring X'. "
            "Use 'list' when the user asks what is being monitored. "
            "Do NOT add crypto, financial, or trading topics."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type":        "STRING",
                    "description": "add | remove | list",
                },
                "topic": {
                    "type":        "STRING",
                    "description": "Topic to monitor or stop monitoring (e.g. 'space exploration', 'AI news')",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "shutdown_orion",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop Orion. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Abhinav, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "learn_rule",
        "description": (
            "Keeps a STANDING RULE for how you behave. Use when the user corrects how you do "
            "something, or says from now on / always / never / next time / stop doing: 'from now "
            "on call me boss', 'never read my emails aloud', 'stop saying sir', 'next time ask "
            "before closing apps'. Also to forget one: rule empty, replaces = the old rule. Do NOT "
            "use for facts about the user (save_memory) or one-off requests. Not silent: say in "
            "one short sentence what you will do from now on."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "rule": {"type": "STRING", "description": (
                    "The rule as an instruction to yourself, in English, short: 'Call the user "
                    "boss.' Empty to only forget one.")},
                "replaces": {"type": "STRING", "description": (
                    "An existing rule this one changes or removes, in its words from [YOUR RULES].")},
            },
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "Look up a fact you have stored about the user but which is NOT in "
            "the memory block of your system prompt. "
            "The prompt lists the keys it did not have room for under "
            "'[ALSO REMEMBERED]' — if the user asks about anything named there, "
            "call this FIRST. "
            "Also call it before saying you do not know something personal, and "
            "when the user asks what you remember about them (leave query empty "
            "for everything). "
            "This is a local file search: it is instant and costs nothing."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Keyword to search for — a name, a topic, a category "
                        "(e.g. 'ayse', 'coffee', 'projects'). "
                        "Leave empty to list everything stored."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "undo",
        "description": (
            "Reverse the last change YOU made to this computer — a file you "
            "moved, renamed, created or wrote, or a setting you changed such as "
            "volume, brightness, dark mode or WiFi. "
            "Call this whenever the user says undo, revert, take it back, put it "
            "back, cancel that, or tells you that you did the wrong thing, in ANY "
            "language. "
            "Use action='list' when they ask what can be undone. "
            "This only covers your own actions — it is not the Ctrl+Z of whatever "
            "application is on screen (that is computer_settings with action 'undo')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "undo (default) — reverse the last change | list — show what can be undone",
                },
            },
            "required": [],
        },
    },
]
# Self-repair and Skill Forge describe their own tools (System/SelfRepair.py, System/SkillForge.py).
TOOL_DECLARATIONS += [SelfRepair.TOOL, SkillForge.TOOL]

class _ReconnectSignal(Exception):
    """Raised inside the session TaskGroup to force a clean, voluntary reconnect
    (e.g. the user picked a new voice — the voice is fixed at connect time, so
    the session must be rebuilt).

    Carries `keep_context`: True for an ordinary rebuild, where the stored
    resumption handle is replayed and the conversation continues; False when the
    new session must genuinely start clean (see the voice-change note in
    _on_voice_change)."""

    def __init__(self, keep_context: bool = True):
        super().__init__()
        self.keep_context = keep_context


def IsReconnectSignal(exc: BaseException) -> bool:
    """True if `exc` is a _ReconnectSignal, or a(n) (Base)ExceptionGroup that
    wraps one — TaskGroup bundles child exceptions into a group."""
    if isinstance(exc, _ReconnectSignal):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(IsReconnectSignal(sub) for sub in exc.exceptions)
    return False


def KeepContextOf(exc: BaseException) -> bool:
    """Read `keep_context` off a reconnect signal, unwrapping the group the
    TaskGroup put it in. Defaults to True: an unexpected shape must not silently
    wipe the conversation."""
    if isinstance(exc, _ReconnectSignal):
        return getattr(exc, "keep_context", True)
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            if IsReconnectSignal(sub):
                return KeepContextOf(sub)
    return True


# Tools that can run for minutes: shown on the phone while they do (System/AlertBus.Work).
_PHONE_TASKS = {"dev_agent": "the dev agent", "browser_control": "a browser task",
                "flight_finder": "a flight search", "code_helper": "a coding task", "game_updater": "game updates"}


class OrionLive:
    def __init__(self, ui: OrionUI):
        # Platform limits are announced once per run, not once per
        # reconnect: a dropped connection is not news about Wayland.
        self._platform_notes_shown = False
        self.ui             = ui
        self._asst_name     = "JARVI    S"   # updated each session from config
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                     = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._interrupted          = False   # True while draining audio after user interrupt
        # Fast commands (System/FastCommands.py). Per Gemini Live turn: None
        # until the user's words have been checked, then "" (not a command) or
        # the reply of the command already run, so the model's own tool call
        # for it is answered without running it a second time.
        self._fast_turn: str | None = None
        self._fast_n               = 0
        # Which engine is holding the conversation, and what it can do.
        # None means Gemini Live — the default and, unless somebody has gone to
        # the providers settings and chosen otherwise, always the case. Every
        # branch below that reads it treats None as "everything is available",
        # so the Live path is exactly what it was.
        self._engine_caps          = None
        # Set only when a local pipeline is running, so push-to-talk can tell it
        # the turn is over. Live decides that on the server; nothing does here.
        self._engine               = None
        # The laptop's own brain (System/LocalBrain.py). `_local_fallback`: Auto mode switched
        # to it because Gemini failed; `_local_hello`: say so once the local session is up.
        self._local_fallback       = False
        self._local_hello          = False
        self._back_wait            = 60.0    # seconds between "is Gemini back?" checks
        self._back_at              = 0.0     # when Orion last switched back to Gemini
        # Deadline for "we sent a turn and the server has said nothing since".
        # None means nothing is outstanding. Armed by the send sites, cleared by
        # the first frame of ANY response, watched by _watch_stall().
        self._rx_deadline          = None
        self._last_out_logged      = ""      # de-dupes a re-sent transcript tail
        # Push-to-talk
        self._ptt_enabled          = False
        self._ptt_held             = False
        self._ptt                  = None    # core.hotkey.PushToTalk
        self._out_level            = 0.0     # level of the audio being played right now
        self._echo                 = EchoGuard()
        # `stream.write()` returns when the buffer accepts the audio, not when the
        # speaker has finished with it, so sound is still in the room after the
        # speaking flag drops. Streaming the microphone during that gap is how an
        # assistant ends up answering itself. Measured from the device rather than
        # guessed; see _play_audio.
        self._out_latency          = 0.20    # seconds, replaced with the real value
        self._tail_until           = 0.0     # monotonic time the echo tail expires
        # Wall-clock time at which the audio written next will begin to sound.
        # The level schedule is anchored to this, never to "now": batches are
        # handed to the device far faster than they play. 0 = nothing playing.
        self._play_cursor          = 0.0
        self.ui.on_push_to_talk   = self.SetPushToTalk
        self.ui.ptt_hold          = self.OnPtt
        self.ui.on_text_command   = self.OnTextCommand
        self.ui.on_interrupt      = self.interrupt
        self.ui.on_voice_change   = self.OnVoiceChange     # voice picker → rebuild session
        self.ui.on_audio_device_change = self.OnAudioDeviceChange
        self._reconnect_event: asyncio.Event | None = None
        self._reconnect_keep = True   # False → next rebuild drops the resumption handle

        # ── Session resumption ─────────────────────────────────────────
        # The server issues a resumption handle every few seconds and reissues
        # it as the conversation moves on. Before this, session_resumption was
        # switched ON in the config and the update was never read, so the handle
        # was thrown away and EVERY reconnect — a dropped packet, a voice change,
        # switching microphone — started an empty session. "Unlimited sessions"
        # leaked through exactly this hole.
        #
        # Deliberately in RAM only, never written to disk. Persisting it would
        # make a fresh launch continue yesterday's conversation, which sounds
        # appealing but breaks the session-summary flow: _save_session_summary
        # runs at shutdown and the morning briefing pops it the next day. A
        # conversation that never ends never produces a summary, and the
        # "yesterday we talked about…" line silently disappears.
        self._resume_handle: str | None = None
        self._turn_done_event: asyncio.Event | None = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._together         = None           # System/Together.py, once the phone bridge is up
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self._session_log: list[str] = []          # conversation turns for end-of-session summary

        self._enhanced_live = True  # proactive audio; auto-disabled if the server rejects it
        self._tuned_live    = True  # turn-taking / media / thinking knobs; same fallback

        _base_dir = Path(__file__).resolve().parent
        _inline_names = {t["name"] for t in TOOL_DECLARATIONS}

        # File-backed tools: every actions/*.py with a TOOL dict, discovered the
        # same way plugins are. Reserved names = the inline tools above, so an
        # action can never shadow one.
        self._action_registry = DiscoverActions(
            actions_dir=_base_dir / "Actions",
            reserved_names=_inline_names,
            logger=lambda msg: print(f"[Actions] {msg}"),
        )

        # Plugins must not collide with either an inline tool or a discovered action.
        _core_names = _inline_names | self._action_registry.names()
        self._plugin_registry = DiscoverPlugins(
            plugins_dir=_base_dir / "Plugins",
            core_tool_names=_core_names,
            # Console gets the full boot transcript; the activity log gets only
            # what the user has to know about. Every plugin loading correctly is
            # the expected case and does not belong in their conversation.
            logger=lambda msg: print(f"[Plugins] {msg}"),
            notify=lambda msg: self.ui.WriteLog(f"SYS: {msg}"),
        )
        self.ui.get_plugins = self._plugin_registry.ListForUi
        self.ui.get_plugin_settings = self._plugin_registry.SettingsSchemas  # ⚙ settings tab
        self.ui.request_say = self.PluginSay   # plugins: mid-task speech channel

        # JARVIS Link (phone): connects in the background if Orion has been paired; never blocks or fails startup.
        try:
            from System.PhoneTasks import PhoneTasks
            from System.Together import Together
            PhoneBridge.BRIDGE.tasks = PhoneTasks(self, PhoneBridge.Answer)
            self._together = Together(self)
            PhoneBridge.BRIDGE.together = self._together
            self._together.Start()
            PhoneBridge.BRIDGE.Start(lambda: {"muted": self.ui.muted, "speaking": self._is_speaking})
        except Exception as e:
            print(f"[PhoneBridge] not started: {e}")

        # Restore the saved push-to-talk preference. Doing it here rather than
        # in __init__ means the hotkey thread only exists once there is a
        # session to talk to.
        if GetPushToTalkEnabled():
            try:
                self.SetPushToTalk(True)
            except Exception as e:
                print(f"[ORION] ⚠ Push-to-talk unavailable: {e}")

        # Background watchers (System/ClipboardWatch.py, System/CrashWatch.py).
        # Their offers are said by the brain, like any other alert.
        from Brain.Settings import GetClipboardHelpEnabled, GetCrashAlertsEnabled
        self._clipboard = ClipboardWatch(
            on_found=lambda kind, text, spoken, note: self.SayAside(spoken, note, "CLIPBOARD"),
            enabled=GetClipboardHelpEnabled,
        )
        self._clipboard.Start()
        CrashWatch.SetAnnouncer(
            lambda spoken, note: self.SayAside(spoken, note, "CRASH") if GetCrashAlertsEnabled() else None)

    def SayAside(self, spoken: str, note: str = "", tag: str = "NOTE") -> None:
        """A watcher's offer, handed to the brain (Gemini Live or the local
        engine) to say in its own words and the user's language.

        `spoken` is what to get across; `note` is what the brain should do if
        the user says yes. Waits up to 20 s for a quiet moment (nobody talking)
        so it never cuts in; if none comes, the offer is dropped. Never blocks.
        """
        def Work():
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                with self._speaking_lock:
                    busy = self._is_speaking
                if not busy and not self.TailActive() and time.monotonic() - self._last_user_speech >= 4:
                    self.PluginSay(f"[{tag}] Tell the user this in one short sentence, in their language, "
                                   f"and stop: {spoken} {note}".strip())
                    return
                time.sleep(0.5)
            print(f"[{tag}] skipped, never quiet: {spoken}")

        threading.Thread(target=Work, daemon=True, name=f"say-aside-{tag.lower()}").start()

    def PhoneTurn(self) -> bool:
        """True while "Hey JARVIS…" said at the desk is the phone's turn (System/Together.py, D1)."""
        return self._together is not None and self._together.PhoneTurn()

    def GiveBriefingNow(self) -> None:
        """The phone handed today's briefing to Orion (you're at the laptop). Thread-safe."""
        loop = self._loop
        if self._briefing_sent or not self.session or loop is None:
            return
        self._briefing_sent = True
        asyncio.run_coroutine_threadsafe(self.SendStartupBriefing(), loop)
        if self._together is not None:
            self._together.Briefed()

    def ToolDecls(self) -> list:
        """Every tool the voice session is offered (the phone's desk runner uses the same list)."""
        return (list(TOOL_DECLARATIONS) + self._action_registry.GetToolDeclarations()
                + self._plugin_registry.GetToolDeclarations())

    def OnSkillsChanged(self) -> None:
        """A forged skill was added or removed. Reconnect so the model's tool
        list matches, after a moment so it can say so first."""
        threading.Timer(4.0, lambda: self.RequestReconnect(keep_context=True, reason="new skill list")).start()

    def PluginSay(self, instruction: str) -> None:
        """
        Thread-safe speech channel for plugins: lets a plugin ask ORION to
        say something short WHILE its run() is still executing (plugins block
        their executor thread, so they can't speak through the tool response
        until they finish). The instruction is injected into the Live session
        exactly like a proactive check-in; Gemini phrases it naturally in the
        user's language. Silently a no-op when no session is connected.
        """
        loop = getattr(self, "_loop", None)
        if not loop or not self.session:
            return

        async def Say():
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": instruction}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[PluginSay] {e}")

        try:
            asyncio.run_coroutine_threadsafe(Say(), loop)
        except Exception as e:
            print(f"[PluginSay] {e}")

    # ── stall watchdog ───────────────────────────────────────────────────────

    def ArmStall(self) -> None:
        """A user turn has gone out; start the clock if one is not already running.

        Deliberately does NOT push an existing deadline forward. The question is
        "how long since the server last said anything while we were waiting",
        not "how long since the most recent send" — otherwise a burst of sends
        into a dead socket would keep resetting the very timer meant to catch it.
        """
        if self._rx_deadline is None:
            self._rx_deadline = time.monotonic() + _STALL_LIMIT

    def ClearStall(self) -> None:
        """The server spoke, so the return path is alive."""
        self._rx_deadline = None

    async def WatchStall(self) -> None:
        """Rebuild the session when a sent turn goes unanswered past the limit.

        The reconnect keeps the resumption handle, so the conversation survives:
        this costs a couple of seconds and a line in the log, where before it
        cost the rest of the session with no indication anything was wrong.
        """
        while True:
            await asyncio.sleep(2)
            deadline = self._rx_deadline
            if deadline is None or time.monotonic() < deadline:
                continue
            self.ClearStall()
            print(f"[ORION] ⚠️  No response for {_STALL_LIMIT:.0f}s — "
                  f"the live session is not answering. Rebuilding it.")
            self.ui.WriteLog("SYS: The model stopped answering — reconnecting.")
            self.RequestReconnect(keep_context=True, reason="no response")
            return          # this session is going away; the new one gets its own watcher

    def RequestReconnect(self, keep_context: bool = True, reason: str = ""):
        """Thread-safe: ask the run loop to tear down and rebuild the Live
        session. Called from the Qt thread. No-op until the async loop and
        reconnect event exist.

        `keep_context=False` drops the resumption handle so the new session
        starts empty — only for changes the server cannot apply to a resumed
        session."""
        loop = getattr(self, "_loop", None)
        ev   = self._reconnect_event
        self._reconnect_keep   = keep_context
        self._reconnect_reason = reason
        if loop and ev is not None:
            loop.call_soon_threadsafe(ev.set)

    def OnVoiceChange(self):
        """Voice picker applied.

        The voice is baked into the session at connect time, so a rebuild is
        required. It is rebuilt WITHOUT the resumption handle on purpose:
        resuming restores the server's own session state, and the safe reading
        is that it restores the voice with it — which would make the picker
        appear to do nothing. Losing context here is acceptable because changing
        voice is a deliberate, rare act; losing it on a dropped packet was not."""
        self.RequestReconnect(keep_context=False, reason="new voice")

    def OnAudioDeviceChange(self, reason: str = "audio device"):
        """A setting that is fixed at connect time changed: the microphone or
        speakers, the Brain, a Gemini Live option or a plugin. Both audio
        streams and the tool list live inside the session TaskGroup, so they
        can only be re-opened by rebuilding it, but the conversation is kept,
        which is the whole reason resumption landed before this feature did."""
        self.RequestReconnect(keep_context=True, reason=reason or "settings")

    async def WatchReconnect(self):
        """Session-scoped task: when a voluntary reconnect is requested, raise a
        signal that unwinds the TaskGroup so the run loop rebuilds the session."""
        assert self._reconnect_event is not None
        await self._reconnect_event.wait()
        self._reconnect_event.clear()
        keep   = self._reconnect_keep
        reason = getattr(self, "_reconnect_reason", "") or "settings"
        self.ui.WriteLog(
            f"SYS: Applying {reason} — reconnecting"
            + ("..." if keep else " (starting a fresh conversation)...")
        )
        raise _ReconnectSignal(keep_context=keep)

    def OnTextCommand(self, text: str):
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self.TypedCommand(text), self._loop)

    async def TypedCommand(self, text: str) -> None:
        """Something typed in the box: a fast command runs here and now, with
        no model and no tokens; anything else goes to the conversation."""
        try:
            reply = await (self.LocalFast(text) if self.LocalSession() else self.RunFastCommand(text))
        except Exception as e:
            print(f"[ORION] fast command failed: {e}")
            reply = None
        if reply is not None:
            self.ui.WriteLog(f"{self._asst_name}: {reply}")
            self._session_log.append(f"User: {text}")
            self._session_log.append(f"{self._asst_name}: {reply}")
            return
        if self.session:
            await self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True)

    async def RunFastCommand(self, text: str, live: bool = False) -> str | None:
        """Run `text` as a fast command if it is one, and return what to say;
        None when it is not (or fast commands are switched off).

        The tool runs through ExecuteTool like any of the model's calls, so undo,
        confirmations and Settings > Usage all see it. `live`: the words came
        from Gemini Live's own transcript, where the model is answering anyway,
        so only commands that DO something are worth taking over."""
        if not GetFastCommandsEnabled():
            return None
        m = FastCommands.Find(text, (self._asst_name.lower(), "orion"))
        if m is None or (live and m.tool is None):
            return None
        result = ""
        if m.tool:
            self._fast_n += 1
            fc = types.FunctionCall(id=f"fast-{self._fast_n}", name=m.tool, args=dict(m.args))
            fr = await self.ExecuteTool(fc)
            result = str((fr.response or {}).get("result", ""))
        reply = m.reply(result)
        print(f"[ORION] fast command: {m.label or text} -> {reply}")
        return reply

    async def CheckFastTurn(self, heard: str) -> None:
        """Gemini Live: the model has started answering, so the user's turn is
        over. If what they said is a fast command, run it now rather than
        waiting for the model to get round to its tool call."""
        if self._fast_turn is not None or self._engine is not None or self._interrupted:
            return
        self._fast_turn = ""
        if not heard.strip():
            return
        try:
            reply = await self.RunFastCommand(heard, live=True)
        except Exception as e:
            print(f"[ORION] fast command failed: {e}")
            reply = None
        if reply is not None:
            self._fast_turn = reply

    def TailActive(self) -> bool:
        """True while the speakers may still be finishing our last sentence."""
        return time.monotonic() < self._tail_until

    def SetSpeaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self._tail_until = 0.0
        else:
            # Hold the guard open across the device's own output latency plus a
            # margin for the room. The microphone is NOT muted during it — the
            # guard still lets a genuine reply through, so answering instantly
            # still works. Only our own echo is dropped.
            self._tail_until = time.monotonic() + self._out_latency + _TAIL_MARGIN
        if not value:
            # The echo history is deliberately NOT cleared here: the tail above
            # still needs it to recognise our own voice. It is dropped when the
            # tail expires. What the guard learned about the room always stays.
            self._out_level = 0.0
        if value:
            self.ui.SetState("SPEAKING")
        else:
            # Muted too (hold-to-speak rests muted): leaving it alone kept the
            # status on SPEAKING after the reply ended.
            self.ui.SetState("MUTED" if self.ui.muted else "LISTENING")

    def SetPushToTalk(self, enabled: bool) -> str:
        """Turn hold-to-talk on or off. Returns the scope actually achieved."""
        from Interface.Hotkey import PushToTalk

        self._ptt_enabled = bool(enabled)
        self._ptt_held = False
        if not enabled:
            if self._ptt is not None:
                self._ptt.stop()
                self._ptt = None
            # Back to the open mic: unmute and drop the "HOLD ..." label.
            try:
                self.ui.SetHoldHint("")
                self.ui.SetMicMuted(False, "Hold to speak off.")
            except Exception:
                pass
            return "off"

        if self._ptt is None:
            self._ptt = PushToTalk(self.OnPtt)
        scope = self._ptt.start()
        # Hold to speak: muted is the resting state. The mic is really closed
        # (not just ignored) until the keys go down.
        try:
            self.ui.SetHoldHint(self._ptt.label)
            self.ui.SetMicHeld(True)
        except Exception:
            pass
        # A window-scoped chord is a real limitation, not a detail — say it once
        # in the log so nobody wonders why it does nothing while another app is
        # focused. Reporting it must never be able to undo the thing it reports.
        try:
            self.ui.WriteLog(
                f"SYS: Hold to speak on — Orion stays muted, hold {self._ptt.label} to talk"
                + ("." if scope == "global"
                   else " (works while this window is focused)."))
        except Exception:
            pass
        return scope

    def OnPtt(self, held: bool) -> None:
        """Chord pressed or released — may arrive on the hotkey thread."""
        self._ptt_held = held
        # Keys down = unmute, keys up = mute again. Quiet: no log line per press.
        try:
            self.ui.SetMicHeld(not held)
        except Exception:
            pass
        if held or self._loop is None:
            return
        # Releasing the key ends the turn. A local pipeline is told directly.
        # Live is told the audio stream ended, so it answers straight away
        # instead of waiting for silence that will never arrive (the mic is
        # closed now). The marker goes through the same queue as the audio so
        # it lands after the last chunk. Scheduled onto the loop because this
        # may arrive on the hotkey thread.
        if self._engine is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._engine.FlushMicrophone(SEND_SAMPLE_RATE), self._loop)
            except Exception as e:
                print(f"[ORION] PTT flush failed: {e}")
        elif self.out_queue is not None:
            try:
                self._loop.call_soon_threadsafe(
                    self.out_queue.put_nowait, {"stream_end": True})
            except Exception as e:
                print(f"[ORION] PTT stream end failed: {e}")

    def interrupt(self) -> None:
        """Stop ORION mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[ORION] ✋ Interrupted — {drained} audio chunks discarded")
        self.SetSpeaking(False)
        self._play_cursor = 0.0     # next batch starts a fresh timeline
        if self._turn_done_event:
            self._turn_done_event.clear()
        # Draining the queue stops the speakers, but a local engine is still
        # generating the rest of the answer and would keep filling it back up.
        # Live has nothing to cancel here — the server stops on its own.
        if self._engine is not None:
            try:
                self._engine.interrupt()
            except Exception:
                pass
        self.ui.WriteLog("SYS: Interrupted — listening...")

    def Speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def SpeakError(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.WriteLog(f"ERR: {tool_name} — {short}")
        self.Speak(f"Sir, {tool_name} encountered an error. {short}")

    def BuildConfig(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # Load customization from config
        try:
            _cfg = json.loads(open(API_CONFIG_PATH, encoding="utf-8").read())
            self._asst_name = (_cfg.get("assistant_name") or "ORION").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "ORION"
            _user_name = ""

        memory     = LoadMemory()
        # Private mode (Brain/Privacy.py): the private kinds never go into Gemini's instructions.
        mem_str    = FormatMemoryForPrompt(Privacy.ForGemini(memory))
        if Privacy.Active():
            mem_str = (mem_str + "\n\n" if mem_str else "") + (
                "[PRIVATE MODE] Some of what you know about the user is kept on the laptop, away from you. "
                "When a tool says something was read to the user privately, you do not have it: never guess, "
                "repeat or sum it up.\n")
        sys_prompt = LoadSystemPrompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        # Identity injection — overrides any hardcoded name in prompt.txt
        # Address form is a property of the language being spoken, so it is
        # stated as a principle rather than a two-language lookup — the model
        # already knows the respectful register of whatever language it is in.
        _addr = (f"ADDRESS: Always call the user '{_user_name}'."
                 if _user_name
                 else 'ADDRESS: Address the user with the ordinary respectful form '
                      'for a superior in the language you are currently speaking — '
                      '"sir" in English, its everyday equivalent in any other '
                      'language. Never an archaic or aristocratic form, and never '
                      'the form from a different language than the one you are '
                      'speaking in this sentence.')
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"{_addr}\n"
            # Who built it is fixed; the name above is the user's choice. Stated
            # apart so a renamed assistant still credits its maker, and honest
            # about the model underneath.
            f"You were built by {CREATOR}. When asked who made, built, created or "
            f"developed you, say it was {CREATOR}. If asked what powers you, say "
            f"you run on a large language model (Google's Gemini by default) — but "
            f"Google did not build you, {CREATOR} did.\n\n"
        )

        # Everything the model is told about *itself* is derived here, not
        # written into prompt.txt: the name comes from config, the platform from
        # the host, the capability list from the registries that were just
        # discovered. Rename the assistant, add a plugin or move to another OS
        # and this follows without anyone editing a prompt.
        _all_decls = (TOOL_DECLARATIONS
                      + self._action_registry.GetToolDeclarations()
                      + self._plugin_registry.GetToolDeclarations())

        # An engine that cannot do something is not offered the tool for it.
        #
        # This is why capabilities are data rather than a pile of conditions: the
        # SAME filtering feeds `_describe_limits` two lines below, through
        # `_names`, so an engine with no eyes ends up with the model being TOLD
        # it cannot see. Without that it would be handed a tool it cannot serve,
        # call it, get nothing back, and describe a screen it never received.
        #
        # `self._engine_caps is None` is Gemini Live and skips all of it, so the
        # declaration list is byte-for-byte what it has always been.
        if self._engine_caps is not None:
            _caps = self._engine_caps
            _blocked = set()
            if not _caps.get("vision"):
                _blocked |= {"screen_process", "close_camera"}
            if not _caps.get("tools"):
                _blocked = {(d.get("name") if isinstance(d, dict)
                             else getattr(d, "name", "")) for d in _all_decls}
            if _blocked:
                _all_decls = [d for d in _all_decls
                              if (d.get("name") if isinstance(d, dict)
                                  else getattr(d, "name", "")) not in _blocked]
                print(f"[ORION] {len(_blocked)} tool(s) withheld — "
                      f"this engine cannot serve them")

        # The laptop's own brain: a short prompt and the small tool set (plan §5). Reading
        # the prompt is the slow part on a small model, so it gets none of the above.
        if self.LocalSession():
            return types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                system_instruction=LocalBrain.Prompt(self._asst_name, CREATOR, _user_name, memory),
                tools=[{"function_declarations": StripMeta(LocalBrain.ShortTools(_all_decls))}],
            )

        _names ={(d.get("name") if isinstance(d, dict) else getattr(d, "name", ""))
                  for d in _all_decls}
        sys_prompt = RenderPrompt(sys_prompt, {
            "assistant_name": self._asst_name,
            "platform": f"{_platform.system()} {_platform.release()}".strip(),
            "capabilities": DescribeTools(_all_decls),
            "limits": DescribeLimits(
                has_vision="screen_process" in _names,
                has_mic=True,
            ),
        })

        parts = [time_ctx, identity_ctx]
        if mem_str:
            parts.append(mem_str)
        # Corrections the user taught, as standing rules (Brain/Rules.py).
        from Brain import Rules
        _rules = Rules.PromptBlock(memory)
        if _rules:
            parts.append(_rules)
        parts.append(sys_prompt)

        cfg = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            # strip_meta drops this project's own `tier` / `brief` keys, which
            # exist for the local-engine tool budget and are not part of the
            # Gemini function-declaration schema — an unknown field there is a
            # validation error, not something the API ignores. A declaration
            # that carries neither comes back as the same object, so what
            # Gemini receives is unchanged from the previous release.
            tools=[{"function_declarations": StripMeta(_all_decls)}],
            # Asks the server to issue session-resumption handles. Enabled but
            # NOT yet used: no handle is captured from the resumption update and
            # none is passed back here, so a reconnect starts a fresh session
            # rather than continuing the old one. Left on because the handles
            # cost nothing and are the prerequisite for doing it properly.
            # Hand back the handle captured from the last session_resumption
            # update. `handle=None` is exactly the old behaviour (ask for
            # handles, start fresh), so the first connect of a run is unchanged.
            session_resumption=types.SessionResumptionConfig(
                handle=self._resume_handle
            ),
            # Sliding-window compression: session never dies from a full context
            # window — ORION can stay in one conversation for hours
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=GetVoice()
                    )
                )
            ),
        )
        if self._enhanced_live:
            # Proactive audio: ORION stays silent when speech isn't addressed
            # to it (background chatter, talking to someone else in the room).
            # (Affective dialog was dropped: gemini-3.1-flash-live does not
            #  support it, and it never reliably detected tone in practice.
            #  To restore it on a 2.5 native-audio model, add back:
            #  cfg["enable_affective_dialog"] = True )
            if GetProactiveAudioEnabled():
                cfg["proactivity"] = types.ProactivityConfig(proactive_audio=True)

        if self._tuned_live:
            cfg.update(self.TuningConfig())

        return types.LiveConnectConfig(**cfg)

    def TuningConfig(self) -> dict:
        """The optional knobs, kept apart so one bad field can be dropped wholesale.

        Every one of these is a preview-API field. If a future model release
        stops accepting any of them the connection fails at setup, so the run
        loop turns `_tuned_live` off and reconnects on the plain config rather
        than leaving the user with an assistant that will not start.
        """
        out: dict = {}

        # How long the server waits through a pause before deciding your turn is
        # over. This — not the size of the prompt — is what most of the delay
        # before a reply actually is, and the default has to suit everybody, so
        # it is necessarily cautious.
        turn = GetTurnTuning()
        if turn.get("enabled", True):
            detect = types.AutomaticActivityDetection(
                silence_duration_ms=turn["silence_ms"],
                prefix_padding_ms=turn["prefix_ms"],
            )
            if turn["end_sensitivity"] == "high":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_HIGH
            elif turn["end_sensitivity"] == "low":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_LOW
            if turn["start_sensitivity"] == "high":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_HIGH
            elif turn["start_sensitivity"] == "low":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_LOW
            out["realtime_input_config"] = types.RealtimeInputConfig(
                automatic_activity_detection=detect)

        # Screenshots and camera frames are tokenised at this resolution and then
        # stay in the session's context. 'medium' keeps on-screen text legible
        # for a fraction of a full-resolution frame.
        res = GetMediaResolution()
        if res != "default":
            out["media_resolution"] = {
                "low":    types.MediaResolution.MEDIA_RESOLUTION_LOW,
                "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
                "high":   types.MediaResolution.MEDIA_RESOLUTION_HIGH,
            }[res]

        # Thinking is left at the server default deliberately. Forcing the budget
        # to zero was measured on gemini-3.1-flash-live over interleaved trials
        # and did not make the first word arrive sooner — this model does not
        # appear to deliberate on the Live path, so pinning the field only adds a
        # way for a future release to behave differently. Set "thinking_enabled"
        # in Config/ApiKeys.json to true to let it reason instead.
        if GetThinkingEnabled():
            out["thinking_config"] = types.ThinkingConfig(thinking_budget=-1)

        return out

    async def ExecuteTool(self, fc, privately=None) -> types.FunctionResponse:
        """Run one tool call, and count it for Settings › Usage.

        A private result (Brain/Privacy.py) is read out on the laptop and the model gets only a note;
        `privately`, when given (the phone's desk runner), receives the private text instead."""
        reply = await self.RunTool(fc)
        split = Privacy.Split(str((reply.response or {}).get("result", "")))
        if split is not None:
            note, spoken = split
            reply.response = dict(reply.response or {}, result=note)
            if privately is not None:
                privately(spoken)
            else:
                await self.SpeakPrivately(spoken)
        return reply

    async def SpeakPrivately(self, text: str) -> None:
        """Read `text` out with Orion's own voice on the laptop (not Gemini's), through the same
        speaker queue as everything else, so the reactor moves and a click stops it."""
        text = Privacy.Spoken(text)
        if not text:
            return
        self.ui.WriteLog("SYS: Read privately on the laptop.")
        q = self.audio_in_queue
        tts = await asyncio.to_thread(self.PrivateVoice)
        if tts is None or q is None:
            # No voice could be made: show it on screen instead (this log never reaches Gemini).
            self.ui.WriteLog(f"SYS: (private) {text}")
            return
        try:
            async for pcm in tts.Speak(text):
                for i in range(0, len(pcm or b""), 4800):
                    q.put_nowait(pcm[i:i + 4800])
        except Exception as e:
            print(f"[Privacy] private voice failed: {e}")
            self.ui.WriteLog(f"SYS: (private) {text}")

    def PrivateVoice(self):
        """Piper with the laptop brain's voice once it's downloaded (fetched in the background the first
        time), else this computer's own voice. Built once."""
        cached = getattr(self, "_private_voice", None)
        if cached is not None and getattr(cached, "voice", "") == LocalBrain.LocalVoice():
            return cached                     # a voice picked in ⚙ › Voice since then is rebuilt
        from Engines import Registry
        voice = None
        try:
            from importlib.util import find_spec
            if find_spec("piper") is not None:
                from Engines import TtsPiper
                cfg = LocalBrain.TtsConfig()
                if cfg["voice"] in TtsPiper.installed():
                    voice = Registry.build(LocalBrain.TTS_ID, cfg)
                else:
                    threading.Thread(target=lambda: TtsPiper.prepare(cfg), daemon=True).start()
        except Exception as e:
            print(f"[Privacy] Piper unavailable: {e}")
        if voice is None:
            try:
                voice = Registry.build("system_tts", Registry.defaults("system_tts"))
            except Exception as e:
                print(f"[Privacy] no private voice: {e}")
                return None
        else:
            self._private_voice = voice       # only Piper is kept; the system voice is a stand-in
        return voice

    async def RunTool(self, fc) -> types.FunctionResponse:
        started = time.monotonic()
        # A long job (still going after 15 s) shows on the phone as "Orion is working on …", then "finished".
        task = _PHONE_TASKS.get(fc.name)
        if task:
            self._phone_job_n = getattr(self, "_phone_job_n", 0) + 1
            session = f"{fc.name}:{self._phone_job_n}"
            told = threading.Timer(15.0, lambda: AlertBus.Work(session, task, "working"))
            told.daemon = True
            told.start()
        try:
            reply = await self._ExecuteTool(fc)
        finally:
            if task:
                told.cancel()
                if time.monotonic() - started >= 15.0:
                    AlertBus.Work(session, task, "done")
        try:
            result = str((reply.response or {}).get("result", ""))
            ok = not Outcome.IsProblem(result) and not result.startswith(("Tool '", "Unknown tool"))
            Usage.RecordTool(fc.name, (time.monotonic() - started) * 1000, ok)
        except Exception:
            pass
        return reply

    async def _ExecuteTool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[ORION] 🔧 {name}  {args}")
        self.ui.SetState("THINKING")


        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                UpdateMemory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.SetState("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "learn_rule":
                from Brain import Rules
                result = Rules.Learn(str(args.get("rule", "") or ""), str(args.get("replaces", "") or ""))
                print(f"[Rules] {result[:100]}")

            elif name == "recall_memory":
                # Local file search: no network, no second model. Kept out of
                # the executor deliberately — it is a dictionary scan over a few
                # hundred short strings, and a thread hop would cost more than
                # the work itself.
                result = SearchMemory(args.get("query", ""), limit=8)
                if Privacy.Active():
                    keep, hidden = Privacy.SplitMemory(result)
                    if hidden:
                        result = Privacy.Privately(result, spoken=hidden, note=(
                            (keep + "\n" if keep.strip() else "")
                            + "(Some matching facts are private; they were read to the user on the laptop, "
                              "not to you.)"))

            elif name == "undo":
                if str(args.get("action", "")).lower().strip() == "list":
                    items = undo_stack.history()
                    result = ("Things I can undo, most recent first:\n"
                              + "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
                              ) if items else "I have not changed anything I can undo yet."
                else:
                    result = await loop.run_in_executor(None, undo_stack.UndoLast)

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # seconds — covers echo window after speaking ends
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(None, CaptureCamera)
                        self.ui.StartCameraStream()
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        img_b, mime_t = await loop.run_in_executor(None, CaptureScreen)
                        print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                        _stall = "screen"
                    self._pending_vision = (img_b, mime_t, user_text, angle)
                    # The image is attached to this same exchange, so there is
                    # nothing to stall for and nothing to announce. Asking for an
                    # acknowledgement here is what produced two spoken answers —
                    # the model filled that turn by answering the question from
                    # imagination, then answered it again once it could see.
                    result = (
                        f"[VISION_ACTIVE] {_stall.capitalize()} captured and attached to this "
                        f"same exchange. Do not acknowledge and do not answer yet — the image "
                        f"is arriving with this result. Reply once, from what you actually see "
                        f"in it."
                    )

            elif name == "close_camera":
                self.ui.StopCameraStream()
                result = "Camera closed."

            elif name == "system_status":
                r = await loop.run_in_executor(None, GetSystemStatus)
                result = str(r)

            elif name == "manage_monitor":
                action = args.get("action", "").lower().strip()
                topic  = args.get("topic", "").strip()
                if action == "add" and topic:
                    result = await asyncio.to_thread(AddMonitor, topic)
                elif action == "remove" and topic:
                    result = await asyncio.to_thread(RemoveMonitor, topic)
                elif action == "list":
                    topics = await asyncio.to_thread(ListMonitors)
                    result = ("Monitoring: " + ", ".join(topics)) if topics else "No topics are being monitored."
                else:
                    result = "Specify action (add/remove/list) and a topic."

            elif name == "shutdown_orion":
                self.ui.WriteLog("SYS: Shutdown requested.")
                async def _do_shutdown():
                    await self._SaveSessionSummary()
                    if self.session:
                        try:
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [{"text": "Say a brief natural goodbye to the user."}]},
                                turn_complete=True,
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)
                    import os as _os
                    CrashWatch.MarkEnded()
                    _os._exit(0)
                asyncio.create_task(_do_shutdown())

            elif name == "self_repair":
                result = SelfRepair.Run(args, show=self.ui.ShowContent, say=self.PluginSay)

            elif name == "skill_forge":
                result = SkillForge.Run(args, self._plugin_registry, show=self.ui.ShowContent,
                                        say=self.PluginSay, after=self.OnSkillsChanged)

            elif self._action_registry.has(name):
                # file_processor: fall back to the currently-uploaded file when none is given
                if name == "file_processor" and not args.get("file_path") and self.ui.CurrentFile:
                    args["file_path"] = self.ui.CurrentFile
                _ctx = {"player": self.ui, "speak": self.Speak,
                        "response": None, "session_memory": None}
                r = await loop.run_in_executor(None, lambda: self._action_registry.run(name, args, _ctx))
                result = r or "Done."
                # web_search: mirror results to the on-screen content panel
                if (name == "web_search" and r
                        and not r.startswith("No results")
                        and not r.startswith("Search failed")):
                    _mode  = args.get("mode", "search")
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    # unwrap: the untrusted markers are for the MODEL. Showing
                    # them to the person who asked for the search is the
                    # machinery leaking into the interface.
                    self.ui.ShowContent(_label, Outcome.unwrap(r))

            else:
                if self._plugin_registry.has(name):
                    r = await loop.run_in_executor(
                        None,
                        lambda: self._plugin_registry.run(name, args, player=self.ui, session_memory=None)
                    )
                    result = r or "Done."
                else:
                    result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}" + SelfRepair.NoteFailure(name, traceback.format_exc())
            traceback.print_exc()
            self.SpeakError(name, e)

        if not self.ui.muted:
            self.ui.SetState("LISTENING")

        print(f"[ORION] 📤 {name} → {str(result)[:80]}")

        # A tool that declared itself NON_BLOCKING also says when its answer may
        # re-enter the conversation. Without this the model finishes whatever it
        # was saying and then reads the result out on top of it — which, for
        # something like a phone call already ringing, is exactly the noise the
        # non-blocking call was meant to avoid. Tools that declared nothing get
        # the API default and behave as they always have.
        _sched = (self._action_registry.scheduling(name)
                  or self._plugin_registry.scheduling(name))
        _extra = {"scheduling": _sched} if _sched else {}
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result},
            **_extra
        )

    async def _SendRealtime(self):
        while True:
            msg = await self.out_queue.get()
            if msg.get("stream_end"):
                # Hold to speak released: the user is done talking.
                try:
                    await self.session.send_realtime_input(audio_stream_end=True)
                except Exception as e:
                    print(f"[ORION] audio_stream_end failed: {e}")
                continue
            # Gemini 3.x Live rejects the old realtime_input.media_chunks field
            # (what `media=...` maps to) and closes the socket with a 1007. Send
            # mic / phone PCM through the new `audio` field instead. Queue items
            # are {"data": <bytes>, "mime_type": <str>} from _listen_audio and
            # the phone relay.
            await self.session.send_realtime_input(
                audio=types.Blob(
                    data=msg["data"],
                    mime_type=msg.get("mime_type", "audio/pcm"),
                )
            )

    async def ListenAudio(self):
        print("[ORION] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            with self._speaking_lock:
                orion_speaking = self._is_speaking

            # ── Barge-in ─────────────────────────────────────────────────────
            # While ORION talks the mic is not streamed, but it is still worth
            # listening to locally: if the user starts speaking, cut the answer
            # short the way a person would stop when interrupted.
            #
            # The whole difficulty is echo — on speakers the mic hears ORION.
            # So the test is not "is the mic loud" but "is the mic louder than
            # the echo of what we are playing right now", sustained long enough
            # that a cough or a keystroke cannot trigger it.
            if orion_speaking:
                # Nothing is streamed while ORION talks.
                #
                # Interrupting by voice used to live here: `EchoGuard` can pick a
                # user out from under our own echo, and `core/echo.py` still does
                # that for the tail below. Re-enabling is small — classify each
                # block here and call interrupt() after `required_blocks` of
                # agreement — but it depends on the listener's room, so it stays
                # out until it can be tried on real hardware.
                return

            # ── Echo tail ────────────────────────────────────────────────────
            # The speaking flag has dropped but the speakers have not finished.
            # Sending this to the model is how an assistant hears itself, decides
            # it was addressed, and answers its own last sentence. The microphone
            # stays OPEN — the guard only drops blocks that are our own voice, so
            # replying the instant it stops still works.
            if self.TailActive():
                try:
                    if not self._echo.IsUserSpeech(
                            indata, SEND_SAMPLE_RATE, PcmLevel(indata)):
                        return
                    self._tail_until = 0.0      # a real voice ends the tail early
                except Exception:
                    return
            elif self._echo._hist:
                self._echo.reset()

            # Hold to speak works through mute itself: the chord unmutes and
            # releasing it mutes, so the check below covers it.
            if not self.ui.muted:
                data = indata.tobytes()
                loop.call_soon_threadsafe(
                    self.out_queue.put_nowait,
                    {"data": data, "mime_type": "audio/pcm"}
                )
                # Feed the live mic level to the HUD so the waveform reacts to
                # the user's actual voice while listening. Purely cosmetic — any
                # failure here must never disturb the mic.
                try:
                    self.ui.SetAudioLevel(PcmLevel(indata))
                except Exception:
                    pass

        try:
            def OpenMic(dev):
                return sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    device=dev,
                    callback=callback,
                )

            # Which microphone. resolve() returns None for "system default" and
            # for a saved device that is no longer present — so a headset
            # unplugged since the last run falls back to the built-in mic
            # instead of raising on startup and taking the session with it.
            _mic_name = GetInputDevice()
            _mic_dev  = AudioDevices.resolve(_mic_name, "input")
            if _mic_dev is not None:
                print(f"[ORION] 🎤 Input device: {_mic_name}")
            def OpenAny():
                try:
                    return OpenMic(_mic_dev)
                except Exception as _e:
                    # A device the picker listed but the driver will not open right
                    # now — exclusive mode, a webcam already in use, a virtual mic
                    # whose source went away. Chosen hardware failing must never
                    # mean the assistant cannot hear at all.
                    if _mic_dev is None:
                        raise
                    print(f"[ORION] ⚠️  Mic '{_mic_name}' failed: {_e} — using default")
                    self.ui.WriteLog(
                        f"SYS: Microphone '{_mic_name}' unavailable — using system default."
                    )
                    return OpenMic(None)

            # Muted means the microphone is CLOSED, not just ignored: the stream
            # is shut so Windows shows no app using the mic, and opened again on
            # unmute.
            _mic_stream = None
            try:
                while True:
                    if self.ui.muted:
                        if _mic_stream is not None:
                            _mic_stream.stop()
                            _mic_stream.close()
                            _mic_stream = None
                            print("[ORION] 🎤 Mic closed (muted)")
                    elif _mic_stream is None:
                        _mic_stream = OpenAny()
                        _mic_stream.start()
                        print("[ORION] 🎤 Mic stream open")
                    # Check faster in hold-to-speak so the first word after the
                    # keys go down is not clipped.
                    await asyncio.sleep(0.02 if self._ptt_enabled else 0.1)
            finally:
                if _mic_stream is not None:
                    try:
                        _mic_stream.stop()
                        _mic_stream.close()
                    except Exception:
                        pass
        except Exception as e:
            print(f"[ORION] ❌ Mic: {e}")
            raise

    async def FlushPendingVision(self) -> bool:
        """Send a captured frame immediately after its tool response.

        The frame is already in hand by the time `screen_process` returns — the
        capture happened inside the tool call. The old flow still made the model
        speak a turn first and only injected the image on that turn's
        turn_complete, which cost a whole extra round trip AND produced two
        spoken answers: one improvised without the picture, then the real one.
        Sending it here means the model has the tool result and the image before
        it generates anything, so the user gets one answer, sooner.
        """
        if not (self._pending_vision and self.session):
            return False

        import base64 as _b64
        img_b, mime_t, question, angle = self._pending_vision
        self._pending_vision = None
        b64 = _b64.b64encode(img_b).decode("ascii")
        print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")

        # Label the source. Without it the image arrives carrying nothing but
        # the user's own sentence, and a screenshot of this app — which has a
        # face in the middle of it — got read as a photo of the user. What the
        # label *means* is explained once, in the generated [SELF] block.
        src = ("[IMAGE SOURCE: WEBCAM]" if angle == "camera"
               else "[IMAGE SOURCE: SCREEN CAPTURE]")
        await self.session.send_client_content(
            turns={"role": "user", "parts": [
                {"inline_data": {"mime_type": mime_t, "data": b64}},
                {"text": f"{src}\n\n{question}"},
            ]},
            turn_complete=True,
        )

        if self._vision_cam_active:
            # Camera: stay busy until ORION has finished speaking the answer,
            # then close the preview.
            self._vision_cam_active    = False
            self._vision_close_pending = True
        else:
            self._vision_busy = False
        return True

    async def ReceiveAudio(self):
        print("[ORION] 👂 Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():
                    # Anything at all from the server proves the return path is
                    # alive, so the stall clock stops here rather than in any of
                    # the branches below — a tool call, a transcript or a bare
                    # keepalive all count.
                    self.ClearStall()

                    # Tokens Gemini says this reply used, for Settings › Usage.
                    _um = getattr(response, "usage_metadata", None)
                    if _um is not None:
                        Usage.AddTokens(getattr(_um, "total_token_count", 0) or 0,
                                        getattr(_um, "prompt_token_count", 0) or 0,
                                        getattr(_um, "response_token_count", 0) or 0)

                    # ── Session resumption ───────────────────────────────────
                    # The server sends this periodically. `resumable` goes false
                    # while a turn is mid-flight — replaying a handle from that
                    # moment is what the flag exists to prevent — so only
                    # resumable handles are kept. This is three lines and it is
                    # the entire fix for "every reconnect forgets everything".
                    _sru = getattr(response, "session_resumption_update", None)
                    if _sru is not None:
                        if getattr(_sru, "resumable", False) and getattr(_sru, "new_handle", None):
                            if self._resume_handle is None:
                                print("[ORION] 🔗 Session resumption armed")
                            self._resume_handle = _sru.new_handle

                    # The model answering (sound, words or a tool call) means
                    # the user's turn is over: the moment to check for a fast
                    # command, once per turn.
                    if self._fast_turn is None and in_buf and (
                            response.data or response.tool_call or (
                                response.server_content and response.server_content.output_transcription
                                and response.server_content.output_transcription.text)):
                        await self.CheckFastTurn(" ".join(in_buf))

                    if response.data:
                        if self._interrupted or self.PhoneTurn():
                            pass  # discard: interrupted, or "Hey JARVIS…" at the desk (the phone answers that one)
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                            _audio_data = response.data
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = CleanTranscript(sc.output_transcription.text)
                            # A turn that involves a tool call passes through
                            # several turn_completes, and the API re-sends the
                            # tail of the transcript across them. Comparing only
                            # against the previous chunk missed that — once
                            # out_buf had been flushed and emptied, the repeat
                            # sailed straight back in, which logged the answer
                            # twice.
                            if txt and not IsRepeatChunk(txt, out_buf):
                                out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = CleanTranscript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()
                            self._fast_turn = None

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf  = []
                                out_buf = []
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self._last_out_logged = ""   # new exchange
                                self.ui.WriteLog(f"You: {full_in}")
                                self._session_log.append(f"User: {full_in}")
                                if self._together is not None:
                                    self._together.OnTurn(full_in)
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            if self.PhoneTurn() and full_out:
                                # Kept as context, not said or logged as Orion's answer: JARVIS answered this one.
                                self._session_log.append(f"({self._asst_name} let JARVIS answer)")
                                full_out = ""
                            # Second line of defence: even if a repeat slips
                            # into a *fresh* buffer after a flush, never log the
                            # same answer (or a tail of it) twice in a row.
                            if full_out and len(full_out) >= _REPEAT_MIN and self._last_out_logged:
                                if full_out in self._last_out_logged:
                                    full_out = ""
                            if full_out:
                                self._last_out_logged = full_out
                                self.ui.WriteLog(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                            out_buf = []

                            if self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def CamClose():
                                    await asyncio.sleep(2.0)
                                    self.ui.StopCameraStream()
                                asyncio.create_task(CamClose())

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[ORION] 📞 {fc.name}")
                            if self._fast_turn:
                                # Already done by the fast path this turn: say
                                # so instead of doing it twice.
                                fn_responses.append(types.FunctionResponse(
                                    id=fc.id, name=fc.name, response={"result": (
                                        f"status=done already_handled=true -- {self._fast_turn} "
                                        "This was already done on the user's computer the moment "
                                        "they asked. Do not do it again and do not call another "
                                        "tool for it; confirm in two or three words.")}))
                                continue
                            fr = await self.ExecuteTool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
                        await self.FlushPendingVision()
        except Exception as e:
            print(f"[ORION] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def PlayAudio(self):
        print("[ORION] 🔊 Play started")

        _spk_name = GetOutputDevice()
        _spk_dev  = AudioDevices.resolve(_spk_name, "output")
        if _spk_dev is not None:
            print(f"[ORION] 🔊 Output device: {_spk_name}")

        def OpenSpk(dev):
            st = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=dev,
            )
            st.start()
            return st

        try:
            stream = OpenSpk(_spk_dev)
        except Exception as _e:
            # A chosen output that the host API accepts by name but refuses to
            # open (exclusive mode, wrong sample rate, device asleep) must not
            # cost the user their voice. Fall back to the default and say so.
            if _spk_dev is None:
                raise
            print(f"[ORION] ⚠️  Output device '{_spk_name}' failed: {_e} — using default")
            self.ui.WriteLog(f"SYS: Speaker '{_spk_name}' unavailable — using system default.")
            stream = OpenSpk(None)

        # Ask the device how far behind the speakers actually are, rather than
        # assuming. This is what the echo tail is sized from, so a machine with a
        # large audio buffer gets a correspondingly longer guard — and one with a
        # tiny buffer is not penalised with a delay it does not need.
        try:
            lat = float(getattr(stream, "latency", 0.0) or 0.0)
            if 0.0 < lat < 1.0:
                self._out_latency = lat
            print(f"[ORION] 🔊 Output latency {self._out_latency*1000:.0f} ms "
                  f"→ echo tail {(self._out_latency + _TAIL_MARGIN)*1000:.0f} ms")
        except Exception:
            pass

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.SetSpeaking(False)
                        self._turn_done_event.clear()
                    elif (self._is_speaking and self.audio_in_queue.empty()
                          and time.time() > self._play_cursor + _QUIET_END):
                        # Speech with no turn end behind it (the private voice,
                        # the laptop brain's hello, announcements): played out
                        # and quiet, so it is over.
                        self.SetSpeaking(False)
                    continue

                self.SetSpeaking(True)

                # Batch all immediately-available chunks into one write to reduce
                # thread-pool round-trips (was one asyncio.to_thread per 50ms slice).
                # Cap at ~200 ms so interrupt() still stops audio within ~200 ms.
                batch = bytearray(chunk)
                while len(batch) < 9600:   # 9600 bytes ≈ 200 ms at 24 kHz / 16-bit mono
                    try:
                        batch.extend(self.audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                # Drive the HUD waveform and the reactor from ORION's own
                # voice. The batch is up to 200 ms long, so we hand over a
                # *schedule* of 20 ms level frames instead of a single averaged
                # level and let the HUD play it out in step with the audio.
                try:
                    pcm = np.frombuffer(bytes(batch), dtype=np.int16)
                    hop = _VIS_HOP / RECEIVE_SAMPLE_RATE
                    frames = PcmVisemes(pcm, sr=RECEIVE_SAMPLE_RATE)
                    # When does this batch become audible? The stream was
                    # started at launch and its callback has been pulling
                    # silence ever since, so the first bytes of a reply reach
                    # the speaker about one callback period later — NOT one
                    # buffer later. `stream.latency` reports the buffer's
                    # capacity, which is how much can be queued ahead, and on
                    # Windows that is commonly 300-500 ms. Anchoring on it put
                    # the entire schedule a buffer late; that is the half second
                    # of lag, and it grew with whatever the device reported.
                    #
                    # After the anchor nothing needs measuring: the device
                    # consumes at exactly realtime, so each batch sounds one
                    # batch-duration after the one before it. The cursor is
                    # re-anchored only when it leaves the range physically
                    # possible — behind `now` means the device drained and this
                    # batch starts a fresh stretch of speech, while further
                    # ahead than the buffer can hold means it has drifted.
                    now = time.time()
                    horizon = self._out_latency + _CURSOR_SLACK
                    if not (now <= self._play_cursor <= now + horizon):
                        self._play_cursor = now + _FIRST_SOUND
                    at = self._play_cursor
                    # Advance by the batch's own duration whether or not it
                    # yielded frames, so a block too short to analyse cannot
                    # shift everything after it out of step with the audio.
                    self._play_cursor += pcm.size / RECEIVE_SAMPLE_RATE
                    if frames:
                        self.ui.PushVisemes(frames, hop, at)
                        # Barge-in needs to know what we are playing, not just
                        # how loud: the guard subtracts this from the microphone.
                        self._out_level = max(f[0] for f in frames)
                        self._echo.NoteOutput(pcm, RECEIVE_SAMPLE_RATE,
                                               self._out_level)
                    else:
                        lvl = PcmLevel(pcm)
                        self.ui.SetAudioLevel(lvl)
                        self._out_level = lvl
                        self._echo.NoteOutput(pcm, RECEIVE_SAMPLE_RATE, lvl)
                except Exception:
                    pass

                try:
                    await asyncio.to_thread(stream.write, bytes(batch))
                except (RuntimeError, asyncio.CancelledError):
                    break   # executor shutting down — exit cleanly
        except Exception as e:
            print(f"[ORION] ❌ Play: {e}")
            raise
        finally:
            self.SetSpeaking(False)
            stream.stop()
            stream.close()

    # ── Morning briefing ────────────────────────────────────────────────────────

    async def SendStartupBriefing(self) -> None:
        """
        Two-phase briefing optimized for speed:
          Phase 1 — instant greeting (no tools) → speech starts in <1s
          Phase 2 — news pre-fetched in a background thread while Phase 1 plays,
                    delivered as ready text (no Gemini tool-call round-trip) and
                    shown on the UI content panel. Waits for turn_complete event
                    instead of a fixed sleep so there is no unnecessary gap.
        """
        memory   = LoadMemory()
        identity = memory.get("identity", {})

        def Val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = Val("language")
        name = Val("name")
        time_str = datetime.now().strftime("%H:%M")

        # Start fetching news immediately — runs in parallel while phase 1 plays
        loop = asyncio.get_event_loop()
        news_future = loop.run_in_executor(None, _fetch_news_sync, "top world news today")

        await asyncio.sleep(0.3)
        if not self.session:
            return

        # ── Phase 1: instant greeting ─────────────────────────────────────────
        # The briefing fires before the user has said anything, so the
        # remembered language is the only signal there is. It is a starting
        # point, not a setting: the moment they reply, their language wins.
        lang_clause = (f" Speak this greeting in {lang}, then follow the "
                       f"user's own language from their first reply onward."
                       if lang else "")
        name_clause = f" Address the user as {name}." if name else ""

        # Inject last session context if available — pop removes it so it's never repeated
        last = await asyncio.to_thread(PopLastSession)
        session_clause = ""
        if last:
            try:
                _delta = (datetime.now() - datetime.strptime(last["date"], "%Y-%m-%d")).days
                _when  = "earlier today" if _delta == 0 else ("yesterday" if _delta == 1 else f"{_delta} days ago")
            except Exception:
                _when = "last time"
            session_clause = (
                f" Also briefly and naturally mention that {_when}: {last['summary']}"
            )

        p1 = (
            f"Greet the user warmly, mention it is {time_str}, and say you are fetching today's news now.{session_clause} "
            f"Keep it to 2 short sentences max. Do not call any tools.{lang_clause}{name_clause}"
        )

        # Clear the turn-done event so we can wait for Phase 1 to finish
        if self._turn_done_event:
            self._turn_done_event.clear()

        await self.session.send_client_content(
            turns={"role": "user", "parts": [{"text": p1}]},
            turn_complete=True,
        )
        print("[ORION] Briefing phase 1 (greeting) sent.")

        # ── Phase 2: fire as soon as Phase 1 audio is done ───────────────────
        async def DeliverNews():
            try:
                lang_str = (f" Speak in {lang} unless the user has since "
                            f"spoken another language, in which case use theirs."
                            if lang else "")

                # Wait for news fetch (already running) and Phase 1 turn-complete
                # in parallel — whichever takes longer determines the wait time
                news_done   = asyncio.wrap_future(news_future)
                turn_waited = False
                if self._turn_done_event:
                    try:
                        await asyncio.wait_for(self._turn_done_event.wait(), timeout=6.0)
                        turn_waited = True
                    except asyncio.TimeoutError:
                        pass

                # Extra buffer: turn_complete fires when Gemini finishes *generating*
                # Phase 1, but audio may still be playing.  Waiting a beat here
                # prevents Phase 2 audio from arriving while Phase 1 is mid-sentence
                # (which sounds like a "repeated first response" to the user).
                if turn_waited:
                    await asyncio.sleep(0.8)
                else:
                    await asyncio.sleep(1.0)

                try:
                    news_text = await asyncio.wait_for(news_done, timeout=8.0)
                except Exception as e:
                    self.ui.WriteLog(f"SYS: News fetch timed out/failed: {e!r}")
                    news_text = ""

                if not self.session:
                    return

                failed = (not news_text) or news_text.startswith(
                    ("No news found", "Search failed", "Please provide")
                )
                if not failed:
                    # Show on UI content panel immediately
                    self.ui.ShowContent("NEWS — top world news today", news_text)

                    p2 = (
                        f"[BRIEFING] Here are today's top news headlines:\n{news_text}\n\n"
                        "Pick ONE headline, summarise it in one sentence, then say the full list "
                        f"is displayed on screen. Do not call any tools.{lang_str}"
                    )
                else:
                    self.ui.WriteLog(
                        f"SYS: News unavailable — backend returned: {news_text[:120]!r}"
                    )
                    p2 = (
                        "News headlines could not be fetched right now. "
                        f"Let the user know briefly.{lang_str}"
                    )

                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": p2}]},
                    turn_complete=True,
                )
                print("[ORION] Briefing phase 2 (news) sent.")
            except Exception as e:
                print(f"[Briefing] Phase 2 error: {e}")
                print(f"[ORION] Briefing phase 2 failed: {e}")
                self.ui.WriteLog("SYS: Could not fetch the news for the briefing.")

        asyncio.create_task(DeliverNews())

    # ── Session memory ──────────────────────────────────────────────────────────

    @staticmethod
    def SummariseLocally(prompt: str) -> str:
        """Blocking: one answer from the laptop's own model. Stopped again after
        if Gemini is the brain, so it doesn't sit in memory for one summary."""
        from Brain import Gemini
        try:
            reply = Gemini.ProviderCall(Gemini.LOCAL_RUNG, prompt, None, 120_000)
            return (getattr(reply, "text", "") or "").strip()
        except Exception as e:
            print(f"[Memory] local session summary failed ({e}); using Gemini.")
            return ""
        finally:
            if not LocalBrain.ACTIVE:
                LocalBrain.Release()

    async def _SaveSessionSummary(self) -> None:
        """Summarise the current session in 1-2 sentences and save to LongTerm.json."""
        log = self._session_log
        if len(log) < 3:          # need at least one exchange to be worth saving
            return
        self._session_log = []    # reset immediately so the next session starts clean

        memory = LoadMemory()
        lang_entry = memory.get("identity", {}).get("language", {})
        lang = (lang_entry.get("value", "") if isinstance(lang_entry, dict) else str(lang_entry)).strip()
        lang = lang or "English"

        convo = "\n".join(log[-40:])   # cap at last 40 turns to stay within token budget
        prompt = (
            f"Summarize this conversation in 1-2 sentences in {lang}. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\n\n" + convo
        )
        try:
            from Brain import Gemini
            summary = ""
            if Privacy.On() and LocalBrain.Installed():
                # Private mode: the laptop's own model writes it (plan §6). On-device
                # mode gets here through Gemini.text below, whose only rung is local.
                summary = await asyncio.to_thread(self.SummariseLocally, prompt)
            if not summary:
                summary = await asyncio.to_thread(
                    Gemini.text, prompt, Gemini.SMART, None, 30_000,
                )
            if summary:
                SaveSessionSummary(summary, lang)
        except Exception as e:
            print(f"[Memory] ⚠️ Session summary failed: {e}")

    # ── System monitor ──────────────────────────────────────────────────────────

    async def RunSystemMonitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if alert:
                AlertBus.Alert("alert", "Laptop", AlertBus.Plain(alert), speak=False)
            if not alert or not self.session:
                continue
            # Don't interrupt an active conversation
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": alert}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Background monitor ──────────────────────────────────────────────────────

    async def RunBackgroundMonitor(self) -> None:
        """Check user-configured topics once per day; speak alerts when new headlines appear."""
        await asyncio.sleep(300)          # wait 5 min after startup before first check
        while True:
            if self.session:
                # Don't interrupt if user spoke recently or ORION is mid-sentence
                with self._speaking_lock:
                    speaking = self._is_speaking
                recent_speech = (time.monotonic() - self._last_user_speech) < 30
                if not speaking and not recent_speech:
                    try:
                        alerts = await asyncio.to_thread(monitor_check_all)
                        memory = LoadMemory()
                        lang_e = memory.get("identity", {}).get("language", {})
                        lang   = (lang_e.get("value", "") if isinstance(lang_e, dict) else str(lang_e)).strip() or "English"
                        for alert in alerts:
                            AlertBus.Alert("monitor", *AlertBus.MonitorText(alert))
                            msg = (
                                f"{alert}\n\n"
                                f"Inform the user about this development naturally in {lang}. "
                                "One brief sentence only."
                            )
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [{"text": msg}]},
                                turn_complete=True,
                            )
                            print("[ORION] Monitor alert sent.")
                            await asyncio.sleep(6)   # gap between consecutive alerts
                    except Exception as e:
                        print(f"[Monitor] ⚠️ Background check error: {e}")
            await asyncio.sleep(1800)     # check every 30 minutes

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def RunProactiveMode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            if not self._proactive.ShouldTrigger(self._last_user_speech):
                continue

            self._proactive.MarkTriggered()

            try:
                memory       = await asyncio.to_thread(LoadMemory)
                monitors     = await asyncio.to_thread(ListMonitors)
                recent_turns = self._session_log[-8:] if self._session_log else []
                prompt = self._proactive.BuildPrompt(
                    memory       = memory,
                    monitors     = monitors or None,
                    recent_turns = recent_turns or None,
                )
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                print("[ORION] Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    # ── main loop ───────────────────────────────────────────────────────────

    # ── Which engine holds the conversation ──────────────────────────────────
    # Everything in this section is inert on a default install: `_engine_mode()`
    # reads one key out of the config that is already loaded, gets "gemini", and
    # returns before the providers package is imported. Delete the folder and
    # the import below raises ImportError, which is caught and reported as
    # "providers are not installed" — the assistant then runs exactly as it did
    # before any of this existed.
    def EngineMode(self) -> str:
        try:
            from Brain.Settings import GetProviderMode
            return GetProviderMode()
        except Exception:
            return "gemini"

    def ResolveEngineCaps(self):
        """What the chosen engine can do — or None for Gemini Live.

        Called BEFORE `_build_config()`, because the tool declarations depend on
        the answer. Building the providers here rather than later is deliberate:
        the registry caches them by configuration, so the objects made now are
        the same ones the engine gets a moment later, and a misconfiguration is
        reported while there is still a Gemini session to fall back to.
        """
        if self.UsingLocal():
            return self.LocalCaps()
        if self.EngineMode() != "pipeline":
            return None
        try:
            from Brain.Settings import GetPipeline, GetProviderConfig
            from Engines import Registry

            pipe = GetPipeline()
            llm_id = str(pipe.get("llm") or "").strip()
            if not llm_id:
                raise RuntimeError("no language model is chosen")
            missing = Registry.MissingFields(llm_id, GetProviderConfig(llm_id))
            if missing:
                raise RuntimeError(f"{llm_id} needs: {', '.join(missing)}")
            llm = Registry.build(llm_id, GetProviderConfig(llm_id))

            # Ask the model whether it can see, once, and remember the answer.
            #
            # This decides whether `screen_process` is declared at all, so it
            # has to be settled BEFORE the config is built. Leaving it to the
            # panel's TEST button meant that anybody who never pressed it ran a
            # vision model with its eyes switched off — silently, and for every
            # session. Measured once per model, in about a second, then read
            # from the settings for ever after.
            from Brain.Settings import SaveProviderConfig
            ensure = getattr(llm, "ensure_vision_known", None)
            if ensure is not None:
                try:
                    seen = ensure(lambda v, _id=llm_id: SaveProviderConfig(_id, v))
                    self.ui.WriteLog(
                        f"SYS: {llm_id} "
                        + ("can see images." if seen else "is text only."))
                except Exception as e:
                    print(f"[ORION] vision check skipped: {e}")

            caps = dict(getattr(llm, "caps", {}) or {})
            caps["audio_out"] = bool(str(pipe.get("tts") or "").strip())
            return caps
        except Exception as e:
            # A local engine that will not start must never mean no assistant.
            # Falling back to Live is the whole reason the ladder exists, and
            # the reason is said out loud rather than left in a console.
            self.ui.WriteLog(f"SYS: local engine unavailable ({e}) — "
                              f"using Gemini Live.")
            print(f"[ORION] Pipeline engine unavailable: {e}")
            return None

    # ── The laptop's own brain (System/LocalBrain.py) ──────────────────────
    def UsingLocal(self) -> bool:
        """On-device mode, or Auto mode after Gemini failed. Never under My own setup."""
        if LocalBrain.Mode() != "auto":
            self._local_fallback = False       # Auto's switch doesn't carry over to a later Auto
        return self.EngineMode() != "pipeline" and LocalBrain.UseLocal(self._local_fallback)

    def LocalCaps(self) -> dict:
        from Engines import Registry
        llm = Registry.build(LocalBrain.LLM_ID, LocalBrain.LlmConfig())
        caps = dict(getattr(llm, "caps", {}) or {})
        caps.update({"audio_out": True, "local_brain": True})
        return caps

    def LocalSession(self) -> bool:
        caps = getattr(self, "_engine_caps", None)
        return bool(caps and caps.get("local_brain"))

    def FallBackToLocal(self, why: str) -> bool:
        """Auto mode: Gemini failed, so switch to the laptop's own brain. False when
        that isn't possible (another mode, not downloaded, already on it)."""
        if self._local_fallback or self.EngineMode() == "pipeline" or not LocalBrain.CanFallBack():
            return False
        self._local_fallback = True
        self._local_hello = True
        # Gemini failing again soon after coming back: check less often.
        self._back_wait = min(self._back_wait * 2, 1800.0) if time.monotonic() - self._back_at < 600 else 60.0
        self._conn_backoff = 0
        print(f"[ORION] {why} — switching to the laptop's own brain.")
        self.ui.WriteLog(f"SYS: {why} — switching to the laptop's own brain.")
        return True

    async def LocalFast(self, text: str) -> str | None:
        """The local brain's first stops: fast commands, the plain phone asks, then the
        "limited tool access" check."""
        reply = await self.RunFastCommand(text)
        if reply is not None:
            return reply
        phone = LocalBrain.PhoneRule(text)
        if phone is not None:
            self._fast_n += 1
            fr = await self.ExecuteTool(types.FunctionCall(id=f"fast-{self._fast_n}", name="ask_phone", args=phone))
            return LocalBrain.PhoneReply(phone["action"], str((fr.response or {}).get("result", "")))
        return LocalBrain.LimitedReply(text)

    async def LocalHello(self) -> None:
        """After the warm-up: say once that the laptop's own brain is answering."""
        engine = self._engine
        if engine is None:
            return
        await engine.warmup()
        self._local_hello = False
        await engine.Announce(LocalBrain.ON_LOCAL)

    async def WatchGeminiBack(self) -> None:
        """Auto mode on the local brain: when Gemini answers again, switch back at a quiet moment."""
        while self._local_fallback:
            await asyncio.sleep(self._back_wait)
            if not await asyncio.to_thread(self.GeminiReachable):
                continue
            while (self._is_speaking or self.TailActive()
                   or time.monotonic() - self._last_user_speech < 20):
                await asyncio.sleep(2)
            self._local_fallback = False
            self._back_at = time.monotonic()
            self.ui.WriteLog("SYS: Gemini is back — switching to it.")
            self.RequestReconnect(keep_context=False, reason="Gemini is back")
            return

    @staticmethod
    def GeminiReachable() -> bool:
        """A cheap check that the key works and Gemini answers (reads a model's details;
        no tokens are spent)."""
        try:
            client = genai.Client(api_key=GetApiKey(), http_options={"timeout": 8000})
            client.models.get(model=_gemini.LiveModel())
            return True
        except Exception as e:
            print(f"[ORION] Gemini still unavailable: {str(e)[:120]}")
            return False

    @asynccontextmanager
    async def OpenSession(self, live_model: str, config):
        """The conversation, from whichever engine is configured.

        Both branches yield an object with the same four methods and the same
        event shapes, which is the entire contract — see providers/base.py. The
        Live branch is unchanged from what it has always been, down to the
        client being rebuilt on every reconnect.
        """
        if self._engine_caps is None:
            client = genai.Client(
                api_key=GetApiKey(),
                http_options={"api_version":
                              "v1alpha" if self._enhanced_live else "v1beta"},
            )
            async with client.aio.live.connect(model=live_model,
                                               config=config) as session:
                yield session
            return

        from Brain.Settings import GetPipeline, GetProviderConfig
        from Engines import Registry
        from Engines.EnginePipeline import PipelineEngine

        local = self.LocalSession()
        pipe = ({"llm": LocalBrain.LLM_ID, "stt": LocalBrain.STT_ID, "tts": LocalBrain.TTS_ID}
                if local else GetPipeline())
        local_cfg = {LocalBrain.LLM_ID: LocalBrain.LlmConfig, LocalBrain.STT_ID: LocalBrain.SttConfig,
                     LocalBrain.TTS_ID: LocalBrain.TtsConfig}

        def part(slot):
            pid = str(pipe.get(slot) or "").strip()
            if not pid:
                return None
            # `log` so a provider can say what it is doing — downloading a
            # voice, loading a model — in the activity log rather than only on a
            # console the user may not be watching.
            cfg = dict(local_cfg[pid]() if local else GetProviderConfig(pid))
            if not (local and slot == "llm"):      # the same object LocalCaps() built
                cfg["log"] = self.ui.WriteLog
            return Registry.build(pid, cfg)

        engine = PipelineEngine(part("llm"), part("stt"), part("tts"),
                                config=config, log=self.ui.WriteLog)
        # Without push-to-talk nothing else knows when a sentence ended, so the
        # engine finds it in the audio. With push-to-talk the key release is the
        # signal and the detector stays out of the way.
        engine.auto_turn = not self._ptt_enabled
        # My own setup: what was heard is checked for a fast command before it
        # reaches the language model, so those cost nothing and answer at once.
        engine.fast_path = self.LocalFast if local else self.RunFastCommand
        self._engine = engine
        try:
            yield engine
        finally:
            self._engine = None
            await engine.close()

    async def run(self):
        self._loop = asyncio.get_event_loop()
        CrashWatch.WatchLoop(self._loop)
        self._reconnect_event = asyncio.Event()

        # ── Wire the shared core services to the interface ───────────────────
        # The confirmation gate is useless without a way to ask, and a memory
        # trim is invisible without a way to say so. Both are bound once here
        # rather than passed down through every action signature.
        confirm_gate.bind(
            show = self.ui.ShowConfirm,
            hide = self.ui.HideConfirm,
            log  = self.ui.WriteLog,
        )
        SetTrimNotifier(self.ui.WriteLog)
        Privacy.SetSpeaker(lambda text: asyncio.run_coroutine_threadsafe(self.SpeakPrivately(text), self._loop))
        PhoneBridge.BRIDGE.WrapConfirm(confirm_gate)   # Orion's confirmations reach the phone when you're away

        # Tell the device picker the exact rates the streams open at, from the
        # constants that actually open them — so it can never list a device that
        # cannot be opened at them.
        AudioDevices.configure(SEND_SAMPLE_RATE, RECEIVE_SAMPLE_RATE)

        # Enumerate audio devices off-thread. The settings drawer must never pay
        # for host-API enumeration on the Qt thread.
        AudioDevices.prefetch()

        while True:
            try:
                print("[ORION] Connecting...")
                self.ui.SetState("THINKING")
                # Pick the rung to open the conversation on. A model resting
                # off a quota limit is skipped; the name is published back to
                # LIVE_MODEL so plugins follow whatever is actually in use.
                global LIVE_MODEL
                LIVE_MODEL = _gemini.LiveModel()
                live_model = LIVE_MODEL
                print(f"[ORION] Live model: {live_model}")

                _resumed_with = self._resume_handle is not None

                # Decided BEFORE the config is built, because the tool
                # declarations depend on what the engine can serve. On a default
                # install this returns None in a few microseconds and nothing
                # below behaves any differently.
                # The laptop's own brain: get Ollama and the model ready first (off the
                # loop: a cold start takes ~20 s), or free them while Gemini answers.
                if self.UsingLocal():
                    try:
                        await asyncio.to_thread(LocalBrain.Prepare)
                    except LocalBrain.NotReady as e:
                        if self._local_fallback:
                            self._local_fallback = False
                            self.ui.WriteLog(f"SYS: {e} Staying on Gemini.")
                        else:
                            # On-device mode and it can't run: wait for a settings change.
                            self.ui.WriteLog(f"SYS: {e}")
                            self.ui.SetState("SLEEPING")
                            self._reconnect_event.clear()
                            try:
                                await asyncio.wait_for(self._reconnect_event.wait(), 300)
                            except TimeoutError:
                                pass
                            self._reconnect_event.clear()
                            continue
                elif LocalBrain.ACTIVE:
                    LocalBrain.ACTIVE = False
                    await asyncio.to_thread(LocalBrain.Release)

                self._engine_caps = self.ResolveEngineCaps()
                LocalBrain.ACTIVE = self.LocalSession()
                config = self.BuildConfig()

                # The conversation itself. `_open_session` gives back a Gemini
                # Live session or a local pipeline wearing the same interface —
                # the Live branch still builds a fresh client on every reconnect
                # to avoid stale HTTP state, and still asks for v1alpha when
                # proactive audio is on.
                async with (
                    self.OpenSession(live_model, config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    # Every user turn arms the stall watchdog. Wrapping the
                    # session once covers all eleven send sites — and every one
                    # added later — where instrumenting them by hand would work
                    # until somebody adds the twelfth and it silently is not
                    # covered. The wrapper is per-session, so it goes away with
                    # the session it belongs to.
                    _orig_send = session.send_client_content

                    async def SendAndArm(*a, **kw):
                        result = await _orig_send(*a, **kw)
                        self.ArmStall()
                        return result

                    session.send_client_content = SendAndArm

                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()
                    self._rx_deadline     = None

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False

                    print("[ORION] Connected.")
                    if _resumed_with:
                        # Say it plainly: the difference between "it reconnected"
                        # and "it reconnected and still knows what we were doing"
                        # is the whole point, and it is invisible otherwise.
                        self.ui.WriteLog("SYS: Reconnected — conversation restored.")

                    self.ui.SetState("LISTENING")
                    self.ui.WriteLog("SYS: ORION online.")

                    # What this machine will not permit, said once, on screen.
                    #
                    # A Wayland session or a macOS without Accessibility cannot
                    # be typed into, clicked or captured — and the APIs report
                    # success regardless. Telling the user at startup turns
                    # "the assistant is broken" into one sentence they can act
                    # on, and it is said once per session rather than on every
                    # attempt.
                    if not self._platform_notes_shown:
                        self._platform_notes_shown = True
                        try:
                            from System import PlatformSupport
                            for _line in PlatformSupport.DescribeForUser(
                                    PlatformSupport.detect()):
                                self.ui.WriteLog(_line)
                        except Exception as _e:
                            print(f"[Platform] capability notes unavailable: {_e}")


                    self._reconnect_event.clear()  # ignore requests from before this session
                    tg.create_task(self.WatchReconnect())
                    tg.create_task(self.WatchStall())
                    tg.create_task(self._SendRealtime())
                    tg.create_task(self.ListenAudio())
                    tg.create_task(self.ReceiveAudio())
                    tg.create_task(self.PlayAudio())
                    tg.create_task(self.RunSystemMonitor())
                    tg.create_task(self.RunBackgroundMonitor())
                    tg.create_task(self.RunProactiveMode())

                    # Bring a local engine's speech model up before anybody
                    # speaks. Measured: without it the FIRST sentence waits 8.9
                    # seconds and every one after it 1.2 — and eight seconds of
                    # nothing is how a working assistant gets reported broken.
                    if self._engine is not None:
                        tg.create_task(self.LocalHello() if self.LocalSession() and self._local_hello
                                       else self._engine.warmup())
                    if self.LocalSession() and self._local_fallback:
                        tg.create_task(self.WatchGeminiBack())

                    # Morning briefing — fires once per process launch (if enabled).
                    if (not self._briefing_sent and GetBriefEnabled()
                            and (self._together is None or self._together.ShouldBrief())):
                        self._briefing_sent = True
                        tg.create_task(self.SendStartupBriefing())
                        if self._together is not None:
                            self._together.Briefed()   # so the phone skips its own today

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                # Voluntary reconnect (voice change) — not an error. Rebuild the
                # session immediately with no backoff and no scary logs.
                if IsReconnectSignal(e):
                    print("[ORION] Voluntary reconnect requested.")
                    if not KeepContextOf(e):
                        # A deliberate clean slate (voice change) — drop the
                        # handle so the next connect really does start empty.
                        self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                # The laptop's own brain stopped (Ollama closed, a bug...). None of the
                # Gemini handling below applies. Auto mode tries Gemini again.
                if self.LocalSession():
                    print(f"[ORION] Local brain error ({type(e).__name__}): {e}")
                    traceback.print_exc()
                    self.ui.WriteLog(f"SYS: The laptop's own brain stopped ({str(e)[:100]}).")
                    self._local_fallback = False
                    for _tb in BugTracebacks(e):
                        CrashWatch.Report(_tb, "the laptop's own brain")
                    self.SetSpeaking(False)
                    self.ui.SetState("SLEEPING")
                    await asyncio.sleep(5)
                    continue

                # A resumption handle the server will not accept — expired, or
                # belonging to a session it has since dropped. Without this, the
                # same dead handle would be replayed on every retry and the
                # assistant would never come back at all: the feature meant to
                # survive a reconnect would be the thing preventing one. Drop it
                # once and let the next attempt start clean.
                if _resumed_with and (
                    "resum" in str(e).lower()
                    or "handle" in str(e).lower()
                    or "INVALID_ARGUMENT" in str(e)
                    or "NOT_FOUND" in str(e)
                ):
                    print("[ORION] 🔗 Resumption handle rejected — starting a fresh session")
                    self.ui.WriteLog("SYS: Could not restore the conversation — starting fresh.")
                    self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                err_str = str(e)
                print(f"[ORION] Error ({type(e).__name__}): {e}")
                traceback.print_exc()

                # Out of quota, or this model is not available to this key —
                # step down the ladder and reconnect straight away. This is the
                # difference between "ORION is quieter today" and "ORION does
                # not start today": one model means one daily limit, and the
                # limit always arrives mid-conversation.
                if _gemini.NoteLiveFailure(live_model, err_str):
                    nxt = _gemini.LiveModel()
                    if nxt == live_model and self.FallBackToLocal("Every Gemini model is out of quota"):
                        continue
                    self.ui.WriteLog(
                        f"SYS: Switching to {nxt.split('/')[-1]} — the previous "
                        f"model is out of quota."
                        if nxt != live_model else
                        "SYS: Every live model is rate-limited — retrying.")
                    self._conn_backoff = 0 if nxt != live_model else 15
                    if nxt == live_model:
                        await asyncio.sleep(self._conn_backoff)
                    continue

                # Turn-taking / media / thinking knobs rejected by the server
                # (preview API drift) — drop them first, because they are the
                # newest fields and the cheapest to lose. Proactive audio is
                # tried again on the next pass if the error persists.
                if self._tuned_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                    or "realtime_input" in err_str.lower()
                    or "media_resolution" in err_str.lower()
                    or "thinking" in err_str.lower()
                ):
                    self._tuned_live = False
                    print("[ORION] Live tuning rejected — reconnecting without it.")
                    continue

                # Proactive audio rejected by the server (preview API drift) —
                # drop it and reconnect with the plain config.
                if self._enhanced_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "proactiv" in err_str.lower()
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                ):
                    self._enhanced_live = False
                    self.ui.WriteLog(
                        "SYS: Proactive audio unavailable — reconnecting without it."
                    )
                    continue

                # Invalid API key — stop hammering the API, prompt re-configuration
                if "API key not valid" in err_str or "1007" in err_str:
                    if self.FallBackToLocal("The Gemini key was refused"):
                        self.ui.WriteLog("ERR: API key invalid — re-enter it in ⚙ to use Gemini again.")
                        continue
                    self.ui.WriteLog("ERR: API key invalid — please re-enter your key.")
                    self.ui.SetState("SLEEPING")
                    self.ui.PromptReconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[ORION] New API key saved — reconnecting...")
                    _conn_backoff = 3
                    continue

                # Network / timeout errors — log clearly and back off
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_net_err and self.FallBackToLocal("Gemini can't be reached"):
                    continue
                if is_net_err:
                    _conn_backoff =min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.WriteLog(
                        f"NET: Connection failed — retrying in {_conn_backoff}s. "
                        "(a VPN may be required)"
                    )
                else:
                    self._conn_backoff = 3
                    # A plain bug (not the network, not the server) in Orion's
                    # own code: keep it for self-repair and offer a fix.
                    for _tb in BugTracebacks(e):
                        CrashWatch.Report(_tb, "the voice session")
            finally:
                self.session = None
                # Only save if there was a real conversation (≥3 turns)
                if len(self._session_log) >= 3:
                    asyncio.create_task(self._SaveSessionSummary())

            self.SetSpeaking(False)
            self.ui.SetState("SLEEPING")


            delay = getattr(self, "_conn_backoff", 3)
            print(f"[ORION] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

_BUG_TYPES = (AttributeError, KeyError, IndexError, TypeError, NameError, ValueError,
              ZeroDivisionError, AssertionError)


def BugTracebacks(e: BaseException) -> list[str]:
    """Tracebacks of the plain programming errors inside `e` (an exception group
    is opened up). Network, server and cancellation errors are left out: they
    are not bugs Orion could repair."""
    out, stack = [], [e]
    while stack:
        x = stack.pop()
        subs = getattr(x, "exceptions", None)
        if isinstance(subs, (list, tuple)) and isinstance(x, BaseException) and subs:
            stack.extend(subs)
            continue
        mod = type(x).__module__ or ""
        if isinstance(x, _BUG_TYPES) and not mod.startswith(("google", "websockets", "aiohttp", "httpx")):
            out.append("".join(traceback.format_exception(type(x), x, x.__traceback__)))
    return out


def main():
    CrashWatch.Install()
    import atexit
    atexit.register(LocalBrain.Release)    # never leave the model holding the graphics card
    ui = OrionUI()
    if getattr(ui, "duplicate", False):
        return                              # the running Orion was asked to show itself

    def runner():
        ui.WaitForApiKey()
        orion = OrionLive(ui)
        try:
            asyncio.run(orion.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")
        except Exception:
            # Orion's brain stopped; the window is still up, so say why.
            traceback.print_exc()
            CrashWatch.Report(traceback.format_exc(), "Orion's main loop")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()