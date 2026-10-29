# ULTRON

A voice-first desktop AI assistant for Windows, macOS and Linux, made by **Abhinav Viswam**.

You talk to Ultron and it talks back in real time (Gemini Live). It can open apps,
control your computer, search the web, read files and your screen, send messages,
set reminders and more. The window is built around a glowing 3D reactor that
listens, thinks and breathes as it speaks.

---

## Quick start

Needs **Python 3.11 or newer**.

```bash
cd Ultron_V2
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

On first launch Ultron asks for your **Gemini API key** (free from
[Google AI Studio](https://aistudio.google.com/apikey)) and saves it to
`Config/ApiKeys.json`. That file is git-ignored; never commit it.

To get a desktop icon, open **⚙ Setup → Create desktop shortcut**.

---

## Using it

| Do this | How |
|---|---|
| Talk | Just speak. There is no wake word; Ultron is always listening unless muted. |
| Type instead | The input bar at the bottom. **+** attaches a file. |
| Mute / unmute | The mic button, or **F4**. Muted means the microphone is fully closed. |
| Stop it talking | The red ■ button, or **Esc**. |
| Full screen | **F11** |
| Conversation | ☰ (top right) opens the conversation drawer. It is open by default. |
| System load | The rings in the top bar (CPU, RAM, GPU) and network speed. Click them for details. |
| Settings | ⚙ opens one window: General, Brain, Voice & audio, Look & name, Memory, Plugins, Usage, System, About. |

Push-to-talk is in Settings › General if you'd rather hold a key to speak.

**Usage** (Settings › Usage) shows the tokens Ultron used today and over the last 14 days,
and how often each tool ran, how often it worked and how long it took. It is counted on
this computer from what Gemini reports, and stored in `Brain/Usage.json` (git-ignored).

### The reactor

The reactor shows what Ultron is doing:

- **Listening**: calm, slowly turning.
- **Thinking**: spins faster and pulses.
- **Speaking**: breathes in and out and swells with each word.
- **Muted / asleep**: dims, slows down and shrinks.

It is rendered in software (numpy and Qt) on its own thread at up to 60 fps,
with no GPU, OpenGL or web view needed.

---

## What it can do

Each file in `Actions/` is a tool Ultron can call:

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
`PLUGIN` and `run()`. Ultron finds it automatically on the next start; no other
file needs changing.

If your plugin needs a model, go through `Brain.Gemini`
(`Gemini.text`, `Gemini.AsJson`, `Gemini.call`). It adds timeouts, fallback
models and whichever provider the user picked in ⚙ › Brain.

---

## Project layout

```
Main.py            the assistant: Gemini Live session, mic, speaker, tool calls
Setup.py           one-time install
Interface/         the window (Window.py), the reactor, hotkeys, app identity
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
cross-module name exists, Ultron calls Gemini and Qt by their real method
names, settings, memory, tools, plugins, the reactor and the window). `--all`
adds the engine self-test. Your settings and memory files are saved before and
restored after, so running the tests never changes them.

---

## Credits

Made by **Abhinav Viswam**.