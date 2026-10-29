import json
import sys
from pathlib import Path

def GetBaseDir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR    = GetBaseDir()
CONFIG_DIR  = BASE_DIR / "Config"
CONFIG_FILE = CONFIG_DIR / "ApiKeys.json"

def EnsureConfigDir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def ConfigExists() -> bool:
    return CONFIG_FILE.exists()

def SaveApiKeys(gemini_api_key: str) -> None:
    EnsureConfigDir()

    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    data["gemini_api_key"] = gemini_api_key.strip()

    CONFIG_FILE.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8"
    )

def LoadApiKeys() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ Failed to load ApiKeys.json: {e}")
        return {}

def GetGeminiKey() -> str | None:
    return LoadApiKeys().get("gemini_api_key")

def IsConfigured() -> bool:
    key = GetGeminiKey()
    return bool(key and len(key) > 15)


def GetAssistantName() -> str:
    """Return the configured assistant name, or 'ULTRON' if not set."""
    return LoadApiKeys().get("assistant_name", "ULTRON") or "ULTRON"


def GetUserName() -> str:
    """Return the configured user name for addressing."""
    return LoadApiKeys().get("user_name", "")


def SaveAssistantConfig(assistant_name: str, user_name: str) -> None:
    """Persist assistant name and user name to config."""
    EnsureConfigDir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["assistant_name"] = assistant_name.strip() or "ULTRON"
    data["user_name"] = user_name.strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── Assistant voice ──────────────────────────────────────────────────────────
# Gemini Live prebuilt voices. Names are proper nouns — identical in every
# language, so this list is safe to show verbatim in any locale.
AVAILABLE_VOICES = ["Charon", "Puck", "Kore", "Fenrir", "Aoede"]
DEFAULT_VOICE    = "Charon"


def GetVoice() -> str:
    """Return the configured Live voice, falling back to the default if unset
    or if the stored value is not a voice we recognise."""
    v = LoadApiKeys().get("voice_name", DEFAULT_VOICE) or DEFAULT_VOICE
    return v if v in AVAILABLE_VOICES else DEFAULT_VOICE


def SaveVoice(voice_name: str) -> None:
    """Persist the chosen Live voice. Unknown names collapse to the default so a
    bad value can never reach the API and break the session."""
    EnsureConfigDir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    v = (voice_name or "").strip()
    data["voice_name"] = v if v in AVAILABLE_VOICES else DEFAULT_VOICE
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")



def GetPushToTalkEnabled() -> bool:
    """Hold-a-key-to-speak. When on, the mic is closed unless the chord is held."""
    return LoadApiKeys().get("push_to_talk_enabled", False)


def SavePushToTalkEnabled(enabled: bool) -> None:
    SaveFlag("push_to_talk_enabled", enabled)


# ── Live-session tuning ──────────────────────────────────────────────────────
# Everything here is optional and has a working default, so an untouched
# config behaves exactly like a configured one. Each value is also a way out:
# if a future model dislikes one of these, set it back and nothing else changes.

def GetThinkingEnabled() -> bool:
    """Whether the Live model may spend tokens thinking before it answers.

    Off by default. A voice assistant is judged on how fast it starts talking,
    and the reasoning that actually needs deliberation in this app is delegated
    to the planning tools, which run on a separate non-Live model.
    """
    return bool(LoadApiKeys().get("thinking_enabled", False))


def SaveThinkingEnabled(enabled: bool) -> None:
    SaveFlag("thinking_enabled", enabled)


def GetTurnTuning() -> dict:
    """How eagerly the server decides you have stopped speaking.

    OFF by default, and that default was earned. Cutting turns shorter looks
    like a free speed win and is not: proactive audio has to judge whether an
    utterance was even addressed to the assistant, and a turn clipped early
    gives it less to judge, so it stays quiet — and the reply to your first
    sentence only arrives once your second one has given it enough context.
    That reads as the assistant being a turn behind, which is far worse than
    the fraction of a second the tuning saves.

    Turn it on with "turn_tuning": {"enabled": true} if your own microphone and
    speaking pace suit it. `silence_ms` is the one that is felt: the pause the
    server sits through before accepting your turn is over.
    """
    cfg = LoadApiKeys().get("turn_tuning")
    cfg = cfg if isinstance(cfg, dict) else {}

    def _Int(key, default, lo, hi):
        try:
            return max(lo, min(hi, int(cfg.get(key, default))))
        except (TypeError, ValueError):
            return default

    return {
        "enabled":    bool(cfg.get("enabled", False)),
        "silence_ms": _Int("silence_ms", 550, 200, 3000),
        "prefix_ms":  _Int("prefix_ms", 150, 0, 1000),
        # "high" = quicker to decide speech has ended.
        "end_sensitivity":   str(cfg.get("end_sensitivity", "high")).lower(),
        "start_sensitivity": str(cfg.get("start_sensitivity", "default")).lower(),
    }


def SaveTurnTuning(values: dict) -> None:
    EnsureConfigDir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    cur = data.get("turn_tuning")
    cur = dict(cur) if isinstance(cur, dict) else {}
    cur.update(values or {})
    data["turn_tuning"] = cur
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def GetProactiveAudioEnabled() -> bool:
    """Whether the model gets to decide an utterance was not aimed at it and
    stay quiet.

    On by default — it is what stops the assistant answering the room. But it
    is also the first thing to switch off if replies ever seem to arrive a turn
    late: what looks like lag is usually the model having judged your previous
    sentence as not addressed to it, and only changing its mind once the next
    one arrives.
    """
    return bool(LoadApiKeys().get("proactive_audio", True))


def SaveProactiveAudioEnabled(enabled: bool) -> None:
    SaveFlag("proactive_audio", enabled)


MEDIA_RESOLUTIONS = ("default", "low", "medium", "high")


def GetMediaResolution() -> str:
    """How finely the model tokenises the screenshots and camera frames it is
    sent. 'medium' keeps on-screen text readable at a fraction of the tokens a
    full-resolution frame costs; 'low' is cheaper still but starts losing small
    text, which is most of what screen captures are for."""
    v = str(LoadApiKeys().get("media_resolution", "medium")).strip().lower()
    return v if v in MEDIA_RESOLUTIONS else "medium"


def SaveMediaResolution(value: str) -> None:
    v = str(value or "").strip().lower()
    SaveFlag("media_resolution", v if v in MEDIA_RESOLUTIONS else "medium")


def SaveFlag(key: str, value) -> None:
    """Read-modify-write one key without disturbing the rest of the config."""
    EnsureConfigDir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[key] = bool(value) if isinstance(value, bool) else value
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def GetBriefEnabled() -> bool:
    return LoadApiKeys().get("morning_brief_enabled", True)


def SaveBriefEnabled(enabled: bool) -> None:
    EnsureConfigDir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["morning_brief_enabled"] = enabled
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── Audio devices ────────────────────────────────────────────────────────────
# Stored as device NAMES, not sounddevice indices. Indices shift every time a
# USB device is plugged in or removed, so a saved index silently starts pointing
# at a different microphone. The empty string means "system default", which is
# both the factory setting and what an unresolvable saved device falls back to —
# so unplugging a headset degrades to the built-in speakers instead of crashing.

def PatchConfig(**fields) -> None:
    """Read-modify-write one or more keys in ApiKeys.json.

    Every setter in this file open-coded this. Collapsing it here means a new
    setting is one line, and there is one place where a corrupt config file is
    handled instead of nine."""
    EnsureConfigDir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.update(fields)
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def GetInputDevice() -> str:
    """Microphone device name, or '' for the system default."""
    return (LoadApiKeys().get("input_device", "") or "").strip()


def SaveInputDevice(name: str) -> None:
    PatchConfig(input_device=(name or "").strip())


def GetOutputDevice() -> str:
    """Speaker device name, or '' for the system default."""
    return (LoadApiKeys().get("output_device", "") or "").strip()


def SaveOutputDevice(name: str) -> None:
    PatchConfig(output_device=(name or "").strip())


def GetPluginEnabled(plugin_name: str) -> bool:
    """Plugins are enabled by default the moment they're discovered (opt-out model)."""
    return LoadApiKeys().get("plugins_enabled", {}).get(plugin_name, True)


# ── Per-plugin settings ("tokens" / connection details) ───────────────────────
# Generic store so a plugin can declare its own config fields (PLUGIN_SETTINGS)
# and the settings UI renders + persists them WITHOUT any core edit — keeping the
# drop-in model intact. Values live under plugin_config[<namespace>][<key>].
# A namespace defaults to the plugin name, but a suite of plugins (e.g. the
# several printer plugins) can share ONE namespace.
def GetPluginConfig(namespace: str) -> dict:
    """All stored values for a namespace (empty dict if none set yet)."""
    cfg = LoadApiKeys().get("plugin_config")
    val = cfg.get(namespace) if isinstance(cfg, dict) else None
    return dict(val) if isinstance(val, dict) else {}


def GetPluginSetting(namespace: str, key: str, default=None):
    """A single value from a namespace, or `default` if unset."""
    return GetPluginConfig(namespace).get(key, default)


def SavePluginConfig(namespace: str, values: dict) -> None:
    """Merge `values` into a namespace's stored config (read-modify-write, like
    every other helper here). Only the provided keys are touched."""
    EnsureConfigDir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    pc = data.get("plugin_config")
    if not isinstance(pc, dict):
        pc = {}
    cur = pc.get(namespace)
    if not isinstance(cur, dict):
        cur = {}
    cur.update(values)
    pc[namespace] = cur
    data["plugin_config"] = pc
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def SavePluginEnabled(plugin_name: str, enabled: bool) -> None:
    EnsureConfigDir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    plugins_cfg = data.get("plugins_enabled")
    if not isinstance(plugins_cfg, dict):
        plugins_cfg = {}
    plugins_cfg[plugin_name] = enabled
    data["plugins_enabled"] = plugins_cfg
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")

# ── Providers: local STT/LLM/TTS and non-Gemini APIs ─────────────────────────
# Stored here rather than in a file of their own, for the same reason plugin
# settings are: one config to back up, one already in .gitignore, one place a
# user knows to look.
#
# The important property is what happens when nobody has configured anything.
# `get_provider_mode()` returns "gemini" on a config that has never heard of
# providers, and every caller checks that BEFORE importing the providers
# package — so an ordinary install never executes a line of it. This is the
# whole basis of the claim that the folder costs nothing when unused.
PROVIDER_MODE_GEMINI = "gemini"      # the conversation runs on the Live API
PROVIDER_MODE_PIPELINE = "pipeline"  # STT -> LLM -> TTS, assembled from providers


def GetProviders() -> dict:
    """The whole providers block, or an empty dict."""
    val = LoadApiKeys().get("providers")
    return dict(val) if isinstance(val, dict) else {}


def GetProviderMode() -> str:
    """Which engine holds the conversation. Defaults to Gemini Live."""
    mode = str(GetProviders().get("mode", "") or "").strip().lower()
    return mode if mode in (PROVIDER_MODE_GEMINI, PROVIDER_MODE_PIPELINE) \
        else PROVIDER_MODE_GEMINI


def GetProviderConfig(provider_id: str) -> dict:
    """Stored settings for one provider — address, model, key, limits."""
    creds = GetProviders().get("creds")
    val = creds.get(provider_id) if isinstance(creds, dict) else None
    return dict(val) if isinstance(val, dict) else {}


def GetPipeline() -> dict:
    """Which provider fills each slot of the pipeline engine."""
    val = GetProviders().get("pipeline")
    return dict(val) if isinstance(val, dict) else {}


def GetSidecarProvider() -> str:
    """The provider id used for one-shot calls when Gemini cannot answer them,
    or '' for none.

    Separate from the conversation on purpose: these are different jobs with
    different failure modes. Gemini's Live quota and its text quota are separate
    pools, and on the free tier it is the text pool that runs out first — which
    kills document reading, screen reasoning and code generation while the
    conversation carries on. Being able to point only those at another model,
    without changing the voice at all, is the cheapest useful thing this whole
    folder does.
    """
    return str(GetProviders().get("sidecar", "") or "").strip()


def PatchProviders(**fields) -> None:
    p = GetProviders()
    p.update(fields)
    PatchConfig(providers=p)


def SaveProviderMode(mode: str) -> None:
    mode = (mode or "").strip().lower()
    PatchProviders(mode=mode if mode in (PROVIDER_MODE_GEMINI,
                                           PROVIDER_MODE_PIPELINE)
                     else PROVIDER_MODE_GEMINI)


def SaveProviderConfig(provider_id: str, values: dict) -> None:
    """Merge settings for one provider. Only the given keys are touched, so the
    panel can save one field without reading and rewriting the rest."""
    p = GetProviders()
    creds = p.get("creds")
    if not isinstance(creds, dict):
        creds = {}
    cur = creds.get(provider_id)
    cur = dict(cur) if isinstance(cur, dict) else {}
    cur.update(values or {})
    creds[provider_id] = cur
    p["creds"] = creds
    PatchConfig(providers=p)


def SavePipeline(stt: str = None, llm: str = None, tts: str = None) -> None:
    """Set one or more pipeline slots. `None` leaves a slot alone; '' clears it."""
    p = GetProviders()
    pipe = p.get("pipeline")
    pipe = dict(pipe) if isinstance(pipe, dict) else {}
    for slot, value in (("stt", stt), ("llm", llm), ("tts", tts)):
        if value is not None:
            pipe[slot] = str(value).strip()
    p["pipeline"] = pipe
    PatchConfig(providers=p)


def SaveSidecarProvider(provider_id: str) -> None:
    PatchProviders(sidecar=(provider_id or "").strip())


def GetSidecarFirst() -> bool:
    """Whether the side-call provider is tried BEFORE Gemini rather than after.

    Off by default: measured in core/gemini.py, the Gemini rungs answer a short
    request in well under a second, and a local model on ordinary hardware does
    not. On means something specific — 'I would rather this stayed on my
    machine, and I accept it being slower' — so it is a choice someone makes,
    never a default they inherit.
    """
    return bool(GetProviders().get("sidecar_first", False))


def SaveSidecarFirst(enabled: bool) -> None:
    PatchProviders(sidecar_first=bool(enabled))


def SaveProviders(mode: str = None, pipeline: dict = None, creds: dict = None,
                   sidecar: str = None, sidecar_first: bool = None) -> None:
    """Write the whole providers block in ONE file write.

    The settings panel used to call five setters in a row — one per provider's
    credentials, then the mode, then the pipeline, then the two side-call
    keys — and every one of them was a separate read-modify-write of
    ApiKeys.json. Five reads and five writes of the same file, in sequence,
    while the panel's own APPLY was already triggering a reconnect that makes
    Main.py write to it too. Any interleaving loses whichever key was written
    first, and the one people noticed was the mode: set MY OWN SETUP, save,
    reopen, and it says Gemini again.

    `None` leaves a section alone; `creds` is merged per provider so a panel
    that only edited one does not have to send the others.
    """
    EnsureConfigDir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    block = data.get("providers")
    block = dict(block) if isinstance(block, dict) else {}

    if mode is not None:
        m = str(mode).strip().lower()
        block["mode"] = m if m in (PROVIDER_MODE_GEMINI,
                                   PROVIDER_MODE_PIPELINE) else PROVIDER_MODE_GEMINI
    if pipeline is not None:
        current = block.get("pipeline")
        current = dict(current) if isinstance(current, dict) else {}
        current.update({k: str(v or "").strip() for k, v in pipeline.items()})
        block["pipeline"] = current
    if creds is not None:
        store = block.get("creds")
        store = dict(store) if isinstance(store, dict) else {}
        for pid, values in (creds or {}).items():
            one = store.get(pid)
            one = dict(one) if isinstance(one, dict) else {}
            one.update(values or {})
            store[pid] = one
        block["creds"] = store
    if sidecar is not None:
        block["sidecar"] = str(sidecar or "").strip()
    if sidecar_first is not None:
        block["sidecar_first"] = bool(sidecar_first)

    data["providers"] = block
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
