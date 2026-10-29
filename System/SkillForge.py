"""
System/SkillForge.py: Orion writing itself a new skill when you ask for one.

"Make yourself a skill that tells me the price of bitcoin" becomes a new
plugin in Plugins/, in the same shape as every other plugin (a PLUGIN dict and
a run() function), so Settings › Plugins lists it with a switch like the rest.

  1. Draft: the model writes the plugin and an example call.
  2. Safety scan, before anything runs: it may not run programs, delete
     files, change the registry, load code at run time or read Orion's keys
     and memory, and it may only import packages that are already installed
     (nothing is pip-installed for it). From Orion itself it may only use
     Brain.Gemini, the shared model client.
  3. Test run: the example call runs in a separate Python process with a
     time limit. It has to return an answer, not crash.
  4. If the scan or the test fails, the model gets the reason and tries again
     (twice at most).
  5. The code is shown in the content panel and the confirmation banner asks
     whether to add it. Only then is the file written. The session reconnects
     so the model can use it, and "undo" removes it again.

Ported from Brahma's skill_forge and skill_crucible. Differences: Brahma
installs whatever packages the code imports and turns the skill on by itself;
here you see the code and say yes first, and nothing is installed.

The test run is a separate process, not a locked-down sandbox: it is the scan
that keeps dangerous code out, and your yes that lets the skill in.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = ROOT / "Plugins"
REMOVED_DIR = ROOT / "Config" / "Forge" / "Removed"     # removed skills are moved here, not deleted
MARK = "# Made by Skill Forge"
MAX_TRIES = 3
TEST_TIMEOUT = 60
_NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")

# ── the safety scan ─────────────────────────────────────────────────────────

_BANNED_IMPORTS = {
    "subprocess", "ctypes", "winreg", "_winreg", "pickle", "marshal", "shelve", "importlib",
    "multiprocessing", "runpy", "code", "codeop", "pty", "keyring", "win32api", "win32con",
    "win32process", "win32security", "pywintypes", "comtypes", "psutil", "pyautogui",
    "pynput", "keyboard", "mouse", "smtplib", "ftplib", "telnetlib", "socketserver", "builtins",
}
_BANNED_NAMES = {"eval", "exec", "compile", "__import__", "globals", "breakpoint", "input"}
# Attribute calls, wherever they come from: os.remove, Path.unlink, shutil.rmtree…
_BANNED_ATTRS = {
    "system", "popen", "startfile", "spawnl", "spawnv", "spawnlp", "spawnvp", "execv", "execl",
    "execvp", "execlp", "kill", "remove", "unlink", "rmdir", "removedirs", "rmtree", "move",
    "rename", "replace", "chmod", "chown", "__subclasses__", "__globals__", "__builtins__",
}
# list.remove, str.replace and platform.system are everywhere: for these the owner decides.
_ATTR_NEEDS_MODULE = {"remove": {"os"}, "replace": {"os"}, "rename": {"os"}, "move": {"shutil"},
                      "system": {"os"}, "popen": {"os"}, "kill": {"os"}}
_BANNED_TEXT = ("apikeys.json", "api_keys.json", "longterm.json", "login data", ".ssh",
                "system32", "format c:", "wallet.dat", "keychain")
_FIRST_PARTY_OK = {"Brain.Gemini", "Brain"}       # `from Brain import Gemini`
_NETWORK = {"requests", "urllib", "http", "socket", "aiohttp", "httpx", "websocket", "websockets"}


def _Installed(top: str) -> bool:
    if top in sys.stdlib_module_names:
        return True
    try:
        return importlib.util.find_spec(top) is not None
    except (ImportError, ValueError):
        return False


@dataclass
class Scan:
    problems: list[str] = field(default_factory=list)
    uses: list[str] = field(default_factory=list)     # "the internet", "writes files"…

    @property
    def ok(self) -> bool:
        return not self.problems


def ScanCode(code: str, name: str = "") -> Scan:
    """Everything wrong with `code` as a forged plugin, and what it uses."""
    out = Scan()
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        out.problems.append(f"it doesn't parse (line {e.lineno}: {e.msg})")
        return out
    from System.PluginLoader import IsFirstParty

    imported: set[str] = set()
    network = writes = False
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                out.problems.append("it uses a relative import")
            mods = [node.module or ""]
            if node.module == "Brain":
                bad = [a.name for a in node.names if a.name != "Gemini"]
                if bad:
                    out.problems.append(f"it imports {', '.join(bad)} from Brain (only Gemini is allowed)")
        for m in mods:
            top = m.split(".")[0]
            imported.add(top)
            if top in _BANNED_IMPORTS:
                out.problems.append(f"it imports {top}, which skills may not use")
            elif IsFirstParty(top, PLUGINS_DIR):
                if m not in _FIRST_PARTY_OK:
                    out.problems.append(f"it imports {m} from Orion itself (only Brain.Gemini is allowed)")
            elif not _Installed(top):
                out.problems.append(f"it needs {top}, which isn't installed")
            if top in _NETWORK:
                network = True
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in _BANNED_NAMES:
                out.problems.append(f"it calls {f.id}()")
            if isinstance(f, ast.Attribute) and f.attr in _BANNED_ATTRS:
                owner = f.value.id if isinstance(f.value, ast.Name) else ""
                need = _ATTR_NEEDS_MODULE.get(f.attr)
                if need is None or owner in need:
                    out.problems.append(f"it calls {(owner + '.') if owner else ''}{f.attr}()")
            if isinstance(f, ast.Attribute) and f.attr in ("write_text", "write_bytes"):
                writes = True
            if isinstance(f, ast.Name) and f.id == "open":
                mode = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "mode"), None)
                if isinstance(mode, ast.Constant) and any(c in str(mode.value) for c in "wax+"):
                    writes = True
        if isinstance(node, ast.Attribute) and node.attr in ("__subclasses__", "__globals__", "__builtins__", "__code__"):
            out.problems.append(f"it reaches into {node.attr}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            low = node.value.lower()
            hit = next((t for t in _BANNED_TEXT if t in low), None)
            if hit:
                out.problems.append(f"it mentions {hit}, which skills may not touch")

    plugin = next((n for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "PLUGIN" for t in n.targets)), None)
    if plugin is None:
        out.problems.append("it has no PLUGIN dict")
    elif name:
        try:
            meta = ast.literal_eval(plugin.value)
            if meta.get("name") != name:
                out.problems.append(f"PLUGIN['name'] must be '{name}'")
        except (ValueError, AttributeError):
            out.problems.append("PLUGIN must be a plain dict")
    if not any(isinstance(n, ast.FunctionDef) and n.name == "run" for n in tree.body):
        out.problems.append("it has no run(parameters) function")

    if network:
        out.uses.append("the internet")
    if writes:
        out.uses.append("writes files")
    if "webbrowser" in imported:
        out.uses.append("opens your browser")
    if "Brain" in imported:
        out.uses.append("the AI model")
    out.problems = list(dict.fromkeys(out.problems))
    return out


# ── the test run ────────────────────────────────────────────────────────────

_HARNESS = r'''
import json, sys
root, path, params = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
sys.path.insert(0, root)
import importlib.util
from System.PluginLoader import Validate, CallRun
spec = importlib.util.spec_from_file_location("Plugins._forge_test", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
rec = Validate(module, path)
if not rec.valid:
    print("FORGE-RESULT " + json.dumps({"ok": False, "why": rec.error}))
    sys.exit(0)
result = CallRun(rec.run, params, None, None)
print("FORGE-RESULT " + json.dumps({"ok": True, "result": result if isinstance(result, str) else repr(result),
                                     "is_text": isinstance(result, str)}))
'''


def TestRun(code: str, params: dict, timeout: float = TEST_TIMEOUT) -> tuple[bool, str]:
    """Run the plugin once in its own process. (passed, its answer or why it failed)."""
    tmp = Path(tempfile.mkdtemp(prefix="forge-"))
    try:
        (tmp / "skill.py").write_text(code, encoding="utf-8")
        (tmp / "harness.py").write_text(_HARNESS, encoding="utf-8")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", QT_QPA_PLATFORM="offscreen")
        try:
            r = subprocess.run([sys.executable, str(tmp / "harness.py"), str(ROOT), str(tmp / "skill.py"),
                                json.dumps(params or {})], cwd=str(tmp), env=env, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"the test run took longer than {int(timeout)} seconds"
        line = next((l for l in reversed(r.stdout.splitlines()) if l.startswith("FORGE-RESULT ")), None)
        if line is None:
            err = [l for l in (r.stderr or "").strip().splitlines() if l.strip()]
            return False, "it crashed: " + (err[-1][:300] if err else f"exit code {r.returncode}")
        data = json.loads(line[len("FORGE-RESULT "):])
        if not data.get("ok"):
            return False, f"the plugin loader rejected it: {data.get('why')}"
        answer = str(data.get("result") or "").strip()
        if not data.get("is_text"):
            return False, "run() must return a sentence (a str)"
        if not answer:
            return False, "run() returned nothing"
        if LooksFailed(answer):
            return False, (f'its test run answered "{answer[:200]}", which means it didn\'t work: '
                           f"make it actually do the job")
        return True, answer[:400]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# A skill that catches its own error answers politely instead of crashing, so
# a test run that "passed" can still be a failure. Only the opening is checked.
_FAILED_ANSWER = re.compile(
    r"\b(error|failed|failure|unable to|not able to|couldn'?t|could not|can'?t|cannot|sorry|"
    r"not permitted|not allowed|prohibited|for security reasons|traceback|exception)\b", re.IGNORECASE)


def LooksFailed(answer: str) -> bool:
    return bool(_FAILED_ANSWER.search((answer or "")[:160]))


# ── drafting ────────────────────────────────────────────────────────────────

_PROMPT = """You write plugins for Orion, a Python 3 desktop assistant on Windows. Write one for:
{goal}
{name_line}
A plugin is ONE Python file:
- a module-level PLUGIN dict: {{"name": "<snake_case>", "description": "<when the voice model should call
  this tool, with example phrases>", "parameters": {{"type": "OBJECT", "properties": {{...}}, "required": []}}}}
  Property types are STRING, INTEGER, NUMBER or BOOLEAN.
- def run(parameters: dict, player=None, session_memory=None) -> str
  It returns ONE short sentence (or a few lines) that will be spoken. Use parameters.get() with
  sensible defaults: it must work when called with only the example parameters.
- Catch your own errors and return a friendly sentence instead of raising.
Rules:
- Standard library, plus only these installed packages if needed: requests, PIL, bs4. No API keys:
  use free public endpoints that need none, with timeout=10.
- To ask an AI model something: `from Brain import Gemini` then `Gemini.text(prompt, tier=Gemini.FAST)`.
- Never run programs, delete or move files, edit the registry, use eval/exec, or read anything in
  Orion's Config folder.
Reply with ONLY this JSON:
{{"name": "<snake_case>", "code": "<the whole file>", "example": {{<parameters for one test call>}},
  "summary": "<one plain sentence: what the skill does>"}}
If the goal can't really be done within these rules (it would need deleting files, passwords,
running programs...), don't write a skill that only explains that. Reply ONLY
{{"refused": "<one plain sentence: why>"}}"""

_RETRY = """{prompt}

Your previous attempt was not accepted because {why}.
The previous code:
```python
{code}
```
Fix that and reply with the same JSON shape."""


@dataclass
class Skill:
    name: str
    code: str
    summary: str
    example: dict
    answer: str                 # what the test run returned
    uses: list[str]
    goal: str

    def Header(self) -> str:
        return (f"{MARK} on {time.strftime('%Y-%m-%d')}, from: {self.goal[:150]!r}\n"
                f"# Test run with {json.dumps(self.example)[:150]} answered: {self.answer[:150]!r}\n")


def _Name(raw: str) -> str:
    n = re.sub(r"[^a-z0-9_]+", "_", (raw or "").lower()).strip("_")
    return n if _NAME.match(n or "") else ""


def Draft(goal: str, name: str = "", taken: set[str] = frozenset(),
          ask: Optional[Callable[[str], Optional[dict]]] = None,
          test: Callable[[str, dict], tuple[bool, str]] = TestRun) -> Skill | str:
    """A scanned and tested skill for `goal`, or a sentence saying why not."""
    goal = (goal or "").strip()
    if len(goal) < 6:
        return "Say what the new skill should do."
    if ask is None:
        from Brain import Gemini

        def ask(prompt):
            return Gemini.AsJson(prompt, tier=Gemini.SMART, timeout_ms=90_000)
    want = _Name(name)
    if want and (want in taken or (PLUGINS_DIR / f"{want}.py").exists()):
        return f"There's already a tool called {want}. Pick another name."
    base =_PROMPT.format(goal=goal, name_line=f"Call it {want}." if want else "")
    prompt, why = base, "no answer"
    for _ in range(MAX_TRIES):
        reply = ask(prompt)
        if isinstance(reply, dict) and reply.get("refused") and not reply.get("code"):
            return f"I can't make that skill safely: {str(reply['refused']).strip()}"
        if not isinstance(reply, dict) or not str(reply.get("code") or "").strip():
            why = "the reply wasn't the JSON asked for"
            prompt = base
            continue
        code = str(reply["code"])
        skill_name = want or _Name(str(reply.get("name", "")))
        if not skill_name:
            why = "the name isn't snake_case"
        elif skill_name in taken or (PLUGINS_DIR / f"{skill_name}.py").exists():
            why = f"the name {skill_name} is already taken, pick another"
        else:
            scan = ScanCode(code, skill_name)
            if not scan.ok:
                why = "; ".join(scan.problems[:4])
            else:
                example = reply.get("example") if isinstance(reply.get("example"), dict) else {}
                passed, answer = test(code, example)
                if passed:
                    return Skill(skill_name, code, str(reply.get("summary") or "").strip() or goal,
                                 example, answer, scan.uses, goal)
                why = answer
        prompt = _RETRY.format(prompt=base, why=why, code=code[:8000])
    return f"I couldn't build a skill that passed the checks: {why}."


# ── adding and removing ─────────────────────────────────────────────────────

def Install(skill: Skill, registry, after: Callable[[], None] = lambda: None) -> str:
    """Write the plugin, load it into the running registry, and push an undo."""
    from Brain.Undo import PushUndo
    path = PLUGINS_DIR / f"{skill.name}.py"
    if path.exists():
        return f"{path.name} already exists, so I didn't overwrite it."
    path.write_text(skill.Header() + skill.code.rstrip() + "\n", encoding="utf-8")
    rec = registry.Add(path) if registry is not None else None
    if rec is not None and not rec.valid:
        _MoveAway(path)
        return f"The skill didn't load ({rec.error}), so I took it out again."
    PushUndo(f"added the skill {skill.name}", lambda n=skill.name: Remove(n, registry, after))
    after()
    return f"Added the skill {skill.name}: {skill.summary}"


def _MoveAway(path: Path) -> Path:
    REMOVED_DIR.mkdir(parents=True, exist_ok=True)
    target = REMOVED_DIR / f"{path.stem}.{time.strftime('%Y%m%d-%H%M%S')}.py"
    shutil.move(str(path), str(target))
    return target


def Forged() -> list[dict]:
    """Skills this forge made, still in Plugins/."""
    out = []
    for p in sorted(PLUGINS_DIR.glob("*.py")):
        try:
            head = p.read_text(encoding="utf-8")[:400]
        except OSError:
            continue
        if head.startswith(MARK):
            made = re.search(r"from: '(.*?)'", head)
            out.append({"name": p.stem, "file": p.name, "goal": made.group(1) if made else ""})
    return out


def Remove(name: str, registry, after: Callable[[], None] = lambda: None) -> str:
    """Take out a forged skill (moved to Config/Forge/Removed, not deleted).
    Only skills this forge made: hand-written plugins are left alone."""
    name = _Name(name) or name
    hit = next((s for s in Forged() if s["name"] == name), None)
    if hit is None:
        return f"There's no forged skill called {name}."
    path = PLUGINS_DIR / hit["file"]
    _MoveAway(path)
    if registry is not None:
        registry.Drop(name)
    after()
    return f"Removed the skill {name}."


# ── the tool ────────────────────────────────────────────────────────────────

TOOL = {
    "name": "skill_forge",
    "description": (
        "Builds Orion a NEW SKILL (a plugin) when the user asks you to make, create, build or add "
        "a skill, feature or ability you don't have: 'make yourself a skill that tells me the "
        "bitcoin price'. 'forge' writes it, safety-checks it, test-runs it and puts it on screen "
        "for the user to confirm; nothing is added without that. 'list' names the forged skills; "
        "'remove' takes one out. Don't use it for things an existing tool already does."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "forge | list | remove"},
            "goal": {"type": "STRING", "description": "What the new skill should do, in the user's words, in English."},
            "skill_name": {"type": "STRING", "description": "Optional snake_case name; for remove, the skill to remove."},
        },
        "required": ["action"],
    },
}

_busy = threading.Lock()


def Run(args: dict, registry, show: Callable[[str, str], None], say: Callable[[str], None],
        after: Callable[[], None] = lambda: None, draft: Callable[..., Skill | str] = Draft) -> str:
    """`skill_forge`. 'forge' works in the background and ends at the
    confirmation banner, so the reply here only says it started."""
    action = str(args.get("action", "forge") or "forge").lower().strip()
    if action in ("list", "history"):
        skills = Forged()
        if not skills:
            return "I haven't forged any skills yet."
        return "Forged skills:\n" + "\n".join(f"- {s['name']}: {s['goal']}" for s in skills)
    if action in ("remove", "delete"):
        return Remove(str(args.get("skill_name", "") or ""), registry, after)

    goal, name = str(args.get("goal", "") or ""), str(args.get("skill_name", "") or "")
    if len(goal.strip()) < 6:
        return "Ask the user what the new skill should do."
    if not _busy.acquire(blocking=False):
        return "I'm already building a skill. Tell the user to wait for that one first."
    taken = (registry.Names() | registry.reserved) if registry is not None else set()

    def Told(result: str) -> str:
        say(f"[SKILL_FORGE] {result} Tell the user that in one short sentence.")
        return result

    def Work():
        try:
            from Brain import Confirm
            skill = draft(goal, name, taken)
            if isinstance(skill, str):
                Told(skill)
                return
            uses = f" Uses {', '.join(skill.uses)}." if skill.uses else ""
            show(f"NEW SKILL — {skill.name}",
                 f"{skill.summary}{uses}\nTest run answered: {skill.answer}\n\n{skill.code}")
            said = Confirm.request("skill_forge", f"Add the skill '{skill.name}'?",
                                   f"{skill.summary}{uses} The code is in the panel below.",
                                   lambda s=skill: Told(Install(s, registry, after)))
            say(f"[SKILL_FORGE] The skill {skill.name} is built and passed its test: {skill.summary} {said}")
        except Exception as e:
            Told(f"Building the skill failed: {e}")
        finally:
            _busy.release()

    threading.Thread(target=Work, daemon=True, name="skill-forge").start()
    return ("Building the skill now. It can take a minute; I'll put it on screen to confirm. "
            "Tell the user that in a few words.")
