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
  3. If the scan fails, the model gets the reason and tries again (twice at
     most).
  4. The code is shown in the content panel and the confirmation banner asks
     whether to add it. Nothing the model wrote has run yet.
  5. On yes, the test run: the example call runs in a separate Python process
     with a time limit. It has to return an answer, not crash. Only then is
     the file written. The session reconnects so the model can use it, and
     "undo" removes it again.

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
    "subprocess", "ctypes", "_ctypes", "winreg", "_winreg", "pickle", "marshal", "shelve", "importlib",
    "multiprocessing", "runpy", "code", "codeop", "pty", "keyring", "win32api", "win32con",
    "win32process", "win32security", "pywintypes", "comtypes", "psutil", "pyautogui",
    "pynput", "keyboard", "mouse", "smtplib", "ftplib", "telnetlib", "socketserver", "builtins",
    "socket", "nt", "posix", "_winapi", "_posixsubprocess", "msvcrt", "gc", "inspect", "signal",
    "sqlite3", "win32com", "pythoncom", "_thread",
}
# Builtins that run code or reach any object's insides: banned wherever they appear, called or not.
_BANNED_NAMES = {"eval", "exec", "compile", "__import__", "globals", "locals", "vars", "getattr", "setattr",
                 "delattr", "breakpoint", "input", "memoryview", "__builtins__", "__loader__", "__spec__",
                 "__file__",
                 # a path or command spelled out one character / byte at a time slips past the text checks
                 "chr"}
# Exact functions, after `import x as y` / `from x import y` are resolved.
_BANNED_CALLS = {
    "os.system", "os.popen", "os.startfile", "os.kill", "os.killpg", "os.remove", "os.unlink", "os.rmdir",
    "os.removedirs", "os.rename", "os.renames", "os.replace", "os.chmod", "os.chown", "os.lchown",
    "os.truncate", "os.link", "os.symlink", "os.fork", "os.forkpty", "os.putenv", "os.unsetenv",
    "os.add_dll_directory", "os.execl", "os.execle", "os.execlp", "os.execlpe", "os.execv", "os.execve",
    "os.execvp", "os.execvpe", "os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnlpe", "os.spawnv",
    "os.spawnve", "os.spawnvp", "os.spawnvpe", "os.posix_spawn", "os.posix_spawnp", "os.open", "os.fdopen",
    "os.environ", "os.getenv",
    "sys.modules", "sys._getframe", "sys.settrace", "sys.setprofile", "sys.addaudithook", "sys.meta_path",
    "sys.path_hooks",
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
    "operator.attrgetter", "operator.methodcaller", "types.FunctionType", "types.CodeType",
    "io.open", "codecs.open",
}
# Of shutil, only these are harmless.
_SHUTIL_OK = {"shutil.which", "shutil.disk_usage", "shutil.get_terminal_size"}
# Method names banned whatever they're called on (Path.unlink, x.rmtree…).
_BANNED_ATTRS = {"startfile", "unlink", "rmdir", "removedirs", "rmtree", "rename", "chmod", "chown",
                 "lchmod", "kill", "symlink_to", "hardlink_to",
                 # frames and code objects: the way from any object back to every module
                 "f_globals", "f_locals", "f_builtins", "f_back", "tb_frame", "gi_frame", "cr_frame", "ag_frame",
                 "gi_code", "cr_code", "co_code",
                 "fromhex"}
# list.remove, str.replace and platform.system are everywhere: banned only when the owner is os / shutil.
_ATTR_NEEDS_MODULE = {"remove": {"os"}, "replace": {"os"}, "move": {"shutil"}, "system": {"os"},
                      "popen": {"os"}}
# Modules that may only be used as `os.something`, never passed around (`m = os`, `[os][0]`).
_NO_BARE = {"os", "shutil", "sys", "io", "codecs", "operator", "asyncio", "types", "Brain.Gemini"}
# The only parts of Brain.Gemini a skill may use.
_GEMINI_OK = {"text", "AsJson", "call", "FAST", "SMART", "SEARCH"}
# Harmless dunders; every other __x__ is off limits.
_DUNDERS_OK = {"__name__", "__init__", "__str__", "__repr__", "__eq__", "__ne__", "__lt__", "__le__", "__gt__",
               "__ge__", "__hash__", "__len__", "__iter__", "__next__", "__enter__", "__exit__", "__doc__",
               "__main__", "__post_init__", "__contains__", "__bool__", "__all__", "__version__"}
_BANNED_TEXT = ("apikeys.json", "api_keys.json", "longterm.json", "phonelink.json", "telegram.json",
                "settings.json", "login data", ".ssh", "system32", "format c:", "wallet.dat", "keychain", ".venv",
                # where a dropped file runs by itself (the Startup folder), and files that are programs
                "start menu", "shell:startup", ".bat", ".cmd", ".vbs", ".ps1", ".lnk", ".exe", ".dll", ".pyw")
_CONFIG_DIR = re.compile(r"(^|[\\/])config([\\/]|$)", re.IGNORECASE)   # Orion's Config folder, however written
_FIRST_PARTY_OK = {"Brain.Gemini", "Brain"}       # `from Brain import Gemini`
# The content panel shows this much (Interface/Window.py ShowContent): a skill is only offered when all of its
# code fits, so the yes is never given to code that was cut off.
SHOW_MAX = 4000
CODE_MAX = 3200                 # the rest of the panel is the summary and the note above the code
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

    # Every name an import binds, to what it really is: `import os as o` → o = os,
    # `from os import system as s` → s = os.system, `from Brain import Gemini` → Gemini = Brain.Gemini.
    alias: dict[str, str] = {}
    imported: set[str] = set()
    network = writes = False
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
            for a in node.names:
                if a.asname:
                    alias[a.asname] = a.name
                else:
                    alias[a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                out.problems.append("it uses a relative import")
            mod = node.module or ""
            mods = [mod]
            for a in node.names:
                if a.name == "*":
                    out.problems.append(f"it uses `from {mod} import *`")
                    continue
                alias[a.asname or a.name] = f"{mod}.{a.name}"
                mods.append(f"{mod}.{a.name}")
            if mod == "Brain":
                bad = [a.name for a in node.names if a.name != "Gemini"]
                if bad:
                    out.problems.append(f"it imports {', '.join(bad)} from Brain (only Gemini is allowed)")
            if mod == "Brain.Gemini":
                bad = [a.name for a in node.names if a.name not in _GEMINI_OK]
                if bad:
                    out.problems.append(f"it imports {', '.join(bad)} from Brain.Gemini (only {', '.join(sorted(_GEMINI_OK))})")
        for m in dict.fromkeys(mods):
            top = m.split(".")[0]
            first = top not in imported
            imported.add(top)
            if top in _BANNED_IMPORTS:
                out.problems.append(f"it imports {top}, which skills may not use")
            elif IsFirstParty(top, PLUGINS_DIR):
                if m not in _FIRST_PARTY_OK and not m.startswith("Brain.Gemini."):
                    out.problems.append(f"it imports {m} from Orion itself (only Brain.Gemini is allowed)")
            elif first and not _Installed(top):
                out.problems.append(f"it needs {top}, which isn't installed")
            if top in _NETWORK:
                network = True

    def Qual(n) -> str:
        """`o.system` → 'os.system' with imports resolved; '' when it isn't a dotted name that starts at
        an import (local variables don't count)."""
        if isinstance(n, ast.Name):
            return alias.get(n.id, "")
        if isinstance(n, ast.Attribute):
            base = Qual(n.value)
            return f"{base}.{n.attr}" if base else ""
        return ""

    def Banned(q: str) -> str:
        """Why the dotted name `q` is off limits, or ''. Also catches it reached through another module
        (`pathlib.os.system`, `Gemini.os.remove`)."""
        parts = q.split(".")
        for i, p in enumerate(parts):
            if p in _BANNED_IMPORTS and i > 0:
                return f"it reaches {p} through another module"
            tail = ".".join(parts[i:])
            if tail in _BANNED_CALLS:
                return f"it uses {tail}"
            if p == "shutil" and len(parts) > i + 1 and tail not in _SHUTIL_OK:
                return f"it uses {tail}"
        if q.startswith("Brain.Gemini.") and parts[2] not in _GEMINI_OK:
            return f"it uses Brain.Gemini.{parts[2]} (only {', '.join(sorted(_GEMINI_OK))})"
        return ""

    def Folded(n) -> str:
        """Every string piece inside an expression, joined: 'Con' + 'fig/' + x reads as 'Config/'."""
        return "".join(c.value for c in ast.walk(n) if isinstance(c, ast.Constant) and isinstance(c.value, str))

    def CheckText(s: str) -> None:
        low = s.lower()
        hit = next((t for t in _BANNED_TEXT if t in low), None)
        if hit:
            out.problems.append(f"it mentions {hit}, which skills may not touch")
        elif _CONFIG_DIR.search(low.strip()) or low.strip(" /\\") == "config":
            out.problems.append("it reaches into Orion's Config folder, which skills may not touch")
        if re.search(r"\{[^{}]*\.__", s):
            out.problems.append("it reaches into an object's insides through a format string")

    # Names used as `x.attr` (so a bare use of a module can be told apart).
    owners = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id in _BANNED_NAMES:
                out.problems.append(f"it uses {node.id}")
            q = alias.get(node.id, "")
            if q:
                why = Banned(q)
                if why:
                    out.problems.append(why)
                if q in _NO_BARE and id(node) not in owners:
                    out.problems.append(f"it passes the {q} module around")
            if node.id.startswith("__") and node.id.endswith("__") and node.id not in _DUNDERS_OK:
                out.problems.append(f"it reaches into {node.id}")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__") and node.attr not in _DUNDERS_OK:
                out.problems.append(f"it reaches into {node.attr}")
            q = Qual(node)
            why = Banned(q) if q else ""
            if why:
                out.problems.append(why)
            if node.attr in _BANNED_ATTRS:
                out.problems.append(f"it uses .{node.attr}")
            need = _ATTR_NEEDS_MODULE.get(node.attr)
            if need is not None:
                owner = Qual(node.value)
                if owner and owner.split(".")[-1] in need:
                    out.problems.append(f"it calls {owner.split('.')[-1]}.{node.attr}()")
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in _BANNED_NAMES:
                out.problems.append(f"it calls {f.id}()")
            if isinstance(f, ast.Attribute) and f.attr in ("write_text", "write_bytes", "mkdir", "touch"):
                writes = True
            if isinstance(f, ast.Name) and f.id == "open":
                mode = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "mode"), None)
                if mode is not None and not isinstance(mode, ast.Constant):
                    out.problems.append("it opens a file with a mode worked out at run time")
                if isinstance(mode, ast.Constant) and any(c in str(mode.value) for c in "wax+"):
                    writes = True
            CheckText(Folded(node))
        if isinstance(node, (ast.BinOp, ast.JoinedStr)):
            CheckText(Folded(node))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            CheckText(node.value)

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
- Keep the whole file short (under 3000 characters): the user reads all of it before saying yes.
Rules:
- Standard library, plus only these installed packages if needed: requests, PIL, bs4. No API keys:
  use free public endpoints that need none, with timeout=10.
- To ask an AI model something: `from Brain import Gemini` then `Gemini.text(prompt, tier=Gemini.FAST)`.
- Never run programs, delete or move files, edit the registry, use eval/exec, or read anything in
  Orion's Config folder. Don't use getattr/setattr/vars, os.environ, shutil, sockets, __file__ or
  other __dunder__ names: the safety check refuses them.
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
          test: Optional[Callable[[str, dict], tuple[bool, str]]] = None) -> Skill | str:
    """A scanned skill for `goal`, or a sentence saying why not. Model code never runs here: the
    test run waits for the user's yes (Run). `test` is only for callers that already have it."""
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
            elif len(code) > CODE_MAX:
                why = f"the file is {len(code)} characters long: keep the whole file under {CODE_MAX}"
            else:
                example = reply.get("example") if isinstance(reply.get("example"), dict) else {}
                passed, answer = test(code, example) if test is not None else (True, "")
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


def TestAndInstall(skill: Skill, registry, after: Callable[[], None] = lambda: None,
                   test: Callable[[str, dict], tuple[bool, str]] = TestRun) -> str:
    """After the user's yes: the one test run, then (only if it worked) the install."""
    passed, answer = test(skill.code, skill.example)
    if not passed:
        return f"The skill {skill.name} failed its test run ({answer}), so I didn't add it."
    skill.answer = answer
    return Install(skill, registry, after) + f" Its test run answered: {answer[:200]}"


def Run(args: dict, registry, show: Callable[[str, str], None], say: Callable[[str], None],
        after: Callable[[], None] = lambda: None, draft: Callable[..., Skill | str] = Draft,
        test: Callable[[str, dict], tuple[bool, str]] = TestRun) -> str:
    """`skill_forge`. 'forge' works in the background and ends at the
    confirmation banner, so the reply here only says it started. The model's
    code is shown and scanned first; it runs (the test) only after the yes."""
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
    try:
        taken = (registry.Names() | registry.reserved) if registry is not None else set()
    except Exception as e:
        _busy.release()                # never left stuck
        return f"I couldn't read the tool list to start the skill: {e}"

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
            shown = (f"{skill.summary}{uses}\nNot run yet: on your yes it is test-run once, and added only "
                     f"if that works.\n\n{skill.code}")
            if len(shown) > SHOW_MAX:
                Told(f"The skill {skill.name} came out too long to show in full on screen, so I didn't offer "
                     f"it. Ask for something smaller.")
                return
            show(f"NEW SKILL — {skill.name}", shown)
            said = Confirm.request("skill_forge", f"Add the skill '{skill.name}'?",
                                   f"{skill.summary}{uses} The code is in the panel below. Confirm to "
                                   f"test-run it and add it.",
                                   lambda s=skill: Told(TestAndInstall(s, registry, after, test)))
            say(f"[SKILL_FORGE] The skill {skill.name} is written and passed the safety check: "
                f"{skill.summary} {said}")
        except Exception as e:
            Told(f"Building the skill failed: {e}")
        finally:
            _busy.release()

    threading.Thread(target=Work, daemon=True, name="skill-forge").start()
    return ("Building the skill now. It can take a minute; I'll put it on screen to confirm. "
            "Tell the user that in a few words.")
