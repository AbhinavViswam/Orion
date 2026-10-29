# ORION

A voice-first desktop AI assistant for Windows, macOS and Linux, made by **Abhinav Viswam**.

You talk to Orion and it talks back in real time (Gemini Live). It can open apps,
control your computer, search the web, read files and your screen, send messages,
set reminders and more. The window is built around a glowing 3D reactor that
listens, thinks and breathes as it speaks.

---

## Quick start

Needs **Python 3.11 or newer**.

```bash
cd Orion
python -m venv .venv
.venv\Scripts\python.exe Setup.py        # Windows
# .venv/bin/python Setup.py              # macOS / Linux
```

`Setup.py` installs the packages for your operating system only, plus the
Playwright browsers used for web automation.

Run it:

```bash
.venv\Scripts\python.exe Main.py         # Windows
# .venv/bin/python Main.py               # macOS / Linux
```

On first launch Orion asks for your **Gemini API key** (free from
[Google AI Studio](https://aistudio.google.com/apikey)) and saves it to
`Config/ApiKeys.json`. That file is git-ignored; never commit it.

To get a desktop icon, open **⚙ Setup → Create desktop shortcut**.

---

## Using it

| Do this | How |
|---|---|
| Talk | Just speak. There is no wake word; Orion is always listening unless muted. |
| Type instead | The input bar at the bottom. **+** attaches a file. |
| Mute / unmute | The mic button, or **F4**. Muted means the microphone is fully closed. |
| Stop it talking | The red ■ button, or **Esc**. |
| Full screen | **F11** |
| Conversation | ☰ (top right) opens the conversation drawer. It is open by default. |
| System load | The rings in the top bar (CPU, RAM, GPU) and network speed. Click them for details. |
| Settings | ⚙ opens one window: General, Brain, Voice & audio, Look & name, Memory, Plugins, Usage, System, About. Changes save as you make them; ones the live session needs (devices, voice, Gemini options, plugins) reconnect for a moment. Only Brain's mode and background requests wait for **Save & apply**. |

**Hold to speak** is in Settings › General: Orion stays muted and only hears you while you hold **Right Ctrl** (on its own — Right Ctrl+C and other shortcuts are ignored).

**Fast commands.** Everyday commands run at once on this computer, with no model:
volume (up, down, mute, "set volume to 40"), brightness, screenshot, lock, show desktop,
window and tab controls, play/pause, "open Spotify", and the time and date. Typed ones
never reach Gemini at all (no tokens); spoken ones in Gemini Live run the moment Gemini
starts answering, instead of waiting for its tool call. Anything longer or less clear goes to
the model as usual. Switch it off in Settings › General. The phrases are in
`System/FastCommands.py`.

**Learning from corrections.** Tell Orion how you want things done and it keeps it as a
rule for every future conversation: "from now on, call me boss", "never read my emails out
loud", "stop calling me sir", "forget the rule about emails". It says what it will do from
now on, `undo` takes a rule back, and Settings › Memory › Rules lists them (Forget deletes
one). Code: `Brain/Rules.py`.

**Self-repair.** When one of Orion's tools crashes, it can offer to fix its own code. Say
"fix yourself" and it drafts the smallest change that stops that error. The change must
match the file exactly and still compile, and you see it in the panel. The banner then
asks before anything is written. A backup is kept, the file must still import (or the
backup goes straight back), and "undo" takes the repair back. The fix applies the next time
Orion starts. A few files (the repair code itself, confirmations, undo, setup) are never
changed. Backups and history: `Config/Repairs/`. Code: `System/SelfRepair.py`.

**Skill Forge.** "Make yourself a skill that…" writes a new plugin into `Plugins/`. Before
you see it, it is safety-scanned and test-run once in a separate process:
- **Not allowed:** running programs, deleting or moving files, registry access, eval or
  exec, reading Orion's keys and memory.
- **Packages:** only ones already installed; nothing is pip-installed.

You get the code in the panel and a yes/no banner. After a yes it appears in Settings ›
Plugins, and "undo" or "remove the skill X" moves it to `Config/Forge/Removed/`. Code:
`System/SkillForge.py`.

**Call secretary** (plugin, `Plugins/CallSecretary.py`). While a WhatsApp Desktop voice
call rings, say "take this call" and Orion takes a message:
1. It turns its own mic off and presses Accept.
2. It says it's your AI assistant and that the message is recorded, then plays a tone.
3. It records until the caller stops talking.
4. It says goodbye and presses End call.
5. It shows you the message (written down by Gemini) and turns its mic back on.

It only answers when you ask, and never a video call. A banner lets you take the call
yourself at any point. It hears the caller through Stereo Mix, or the mic when there's no
Stereo Mix. Settings › Plugins sets the greeting, the device, the time limit and whether
Gemini writes the message down. "What messages did I get?" reads past notes, saved in
`Config/CallSecretary/`.

**Usage** (Settings › Usage) shows the tokens Orion used today and over the last 14 days,
and how often each tool ran, how often it worked and how long it took. It is counted on
this computer from what Gemini reports, and stored in `Brain/Usage.json` (git-ignored).

### The reactor

The reactor shows what Orion is doing:

- **Listening**: calm, slowly turning.
- **Thinking**: spins faster and pulses.
- **Speaking**: breathes in and out and swells with each word.
- **Muted / asleep**: dims, slows down and shrinks.

It is rendered in software (numpy and Qt) on its own thread at up to 60 fps,
with no GPU, OpenGL or web view needed.

---

## What it can do

Each file in `Actions/` is a tool Orion can call:

| Area | Tools |
|---|---|
| Computer | open apps, keyboard/mouse control, system settings, desktop, files (deletes go to the recycle bin), system monitor |
| Web | web search, browser automation, YouTube, flight finder, weather |
| Seeing | screen and webcam understanding, file reader (PDF, Office, images, code) |
| Messages & time | send messages, reminders, proactive check-ins, background monitor |
| Dev | code helper, dev agent |
| Media | video player, game updater |

Risky actions ask for confirmation first, and many can be undone.

---

## Naming style

Files, folders and functions use PascalCase (`WeatherReport.py`,
`CreateDesktopShortcut`). Names that Python, Qt or other libraries define
keep their own spelling (`run`, `paintEvent`, `__init__`).

---

## Adding a tool (plugin)

Copy `Plugins/_Template.py`, rename it (no leading underscore), and fill in
`PLUGIN` and `run()`. Orion finds it automatically on the next start; no other
file needs changing.

If your plugin needs a model, go through `Brain.Gemini`
(`Gemini.text`, `Gemini.AsJson`, `Gemini.call`). It adds timeouts, fallback
models and whichever provider the user picked in ⚙ › Brain.

---

## Project layout

```
Main.py            the assistant: Gemini Live session, mic, speaker, tool calls
Setup.py           one-time install
Interface/         the window (Window.py), the reactor, colours (Accents.py), hotkeys, app identity
Brain/             Gemini calls, the prompt, memory, settings, confirm, undo
Voice/             microphone devices, speech in and out, echo guard
Engines/           other engines: OpenAI-compatible LLMs, Whisper, Piper, ElevenLabs
System/            tool and plugin loaders, installer, platform support
Actions/           built-in tools
Plugins/           drop-in tools
Config/            ApiKeys.json (yours, git-ignored) and the app icon
```

---

## Privacy and secrets

These stay on your machine and are git-ignored: `Config/ApiKeys.json` and any
other keys, tokens, certificates and `.env` files, your memory and conversation
data, uploads and recordings, downloaded voice models, and the virtual
environment. Check with `git status` before every push.

---

## Checks

```bash
.venv\Scripts\python.exe -m Tests.RunAll --all
```

`Tests.RunAll` runs the unit tests in `Tests/` (every module loads, every
cross-module name exists, Orion calls Gemini and Qt by their real method
names, settings, memory, tools, plugins, the reactor and the window). `--all`
adds the engine self-test. Your settings and memory files are saved before and
restored after, so running the tests never changes them.

---

## Credits

Made by **Abhinav Viswam**.