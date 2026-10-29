"""
System/SelfRepair.py: Orion fixing its own bugs, with you in charge.

When a tool crashes, the traceback is kept and the model is told it may OFFER
a repair. Nothing is changed until you say so:

  1. "fix yourself" (the `self_repair` tool) finds the first file of Orion's
     own in the traceback, and asks the model for the smallest change that
     stops that error (a None check, a missing key, a bad cast).
  2. The change is checked before anything is written: it must match the file
     exactly once, and the result must parse and compile.
  3. The change is shown in the content panel, and the confirmation banner asks
     you to apply it.
  4. On yes: a backup is made, the file is written, and the module is imported
     in a separate process. If it no longer imports, the backup goes straight
     back. "Undo" (or `self_repair` rollback) takes any repair back later.

Ported from Brahma's auto_heal_engine, with one deliberate difference: Brahma
patches itself the moment something fails, without asking. Here a crash only
ever produces an offer, and a few files can never be repaired at all, so a bad
fix can't take away the very things that would undo it.

A fix changes the file on disk; the running copy of Orion keeps the old code
until it restarts.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
REPAIR_DIR = ROOT / "Config" / "Repairs"            # backups + History.json
HISTORY_FILE = REPAIR_DIR / "History.json"

# The safety net itself: a fix to one of these could break undo, confirmation
# or the repairing, so they are never offered.
PROTECTED = {
    "System/SelfRepair.py", "System/SkillForge.py", "Brain/Confirm.py", "Brain/Undo.py",
    "Setup.py", "System/Installer.py", "Tests/RunAll.py",
}
_SKIP_PARTS = {".venv", "site-packages", "Tests", "__pycache__", "Config"}
MAX_CHANGE_LINES = 40          # a "fix" bigger than this is a rewrite, not a repair
IMPORT_TIMEOUT = 60

_FRAME = re.compile(r'File "([^"]+\.py)", line (\d+)(?:, in ([^\r\n]+))?')


# ── where it broke ──────────────────────────────────────────────────────────

@dataclass
class Place:
    path: Path
    rel: str            # "Actions/OpenApp.py"
    line: int
    func: str
    error: str          # "KeyError: 'x'"


def _Rel(path: Path) -> Optional[str]:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return None


def Locate(tb: str) -> Optional[Place]:
    """The innermost frame in one of Orion's own files that may be repaired."""
    lines = [l.strip() for l in (tb or "").strip().splitlines() if l.strip()]
    error = lines[-1][:200] if lines else ""
    for raw, line, func in reversed(_FRAME.findall(tb or "")):
        p = Path(raw)
        if not p.is_absolute():
            p = ROOT / p
        rel = _Rel(p)
        if not rel or not p.is_file() or rel in PROTECTED:
            continue
        if _SKIP_PARTS & set(rel.split("/")[:-1]):
            continue
        return Place(p, rel, int(line), (func or "").strip(), error)
    return None


def Context(source: str, line: int) -> str:
    """The imports plus the whole function around `line`; a window when it won't parse."""
    lines = source.splitlines(keepends=True)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = None
    if tree is not None:
        imports = [ "".join(lines[n.lineno - 1:n.end_lineno])
                    for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        best = None
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.lineno <= line <= (n.end_lineno or 0):
                if best is None or (n.end_lineno - n.lineno) < (best.end_lineno - best.lineno):
                    best = n
        if best is not None and best.end_lineno - best.lineno < 400:
            body = "".join(lines[best.lineno - 1:best.end_lineno])
            return "".join(imports[:30]) + f"\n# … line {best.lineno} onwards:\n" + body
    lo, hi = max(0, line - 40), min(len(lines), line + 40)
    return "".join(lines[lo:hi])


# ── the last failure ────────────────────────────────────────────────────────

_last: dict = {}
_lock = threading.Lock()


def NoteFailure(tool: str, tb: str) -> str:
    """Keep a crash's traceback. Returns a sentence to add to the tool's result
    when it can be repaired, so the model knows it may offer; '' otherwise."""
    place = Locate(tb)
    with _lock:
        _last.clear()
        _last.update({"tool": tool, "traceback": tb or "", "at": time.time()})
    if place is None:
        return ""
    return (f" (This is a bug in Orion's own code, {place.rel} line {place.line}. You may offer, "
            f"in a few words, to repair it with self_repair; do not repair it unless the user agrees.)")


def LastFailure() -> dict:
    with _lock:
        return dict(_last)


# ── drafting a fix ──────────────────────────────────────────────────────────

@dataclass
class Fix:
    place: Place
    explanation: str
    find: str
    replace: str
    new_source: str
    digest: str          # of the file as read, so a changed file is never overwritten

    def Diff(self) -> str:
        out = difflib.unified_diff(self.find.splitlines(), self.replace.splitlines(),
                                   f"{self.place.rel} (now)", f"{self.place.rel} (fixed)", lineterm="", n=1)
        return "\n".join(out)


def _Digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def Stage(source: str, find: str, replace: str, line: int) -> Optional[str]:
    """`source` with `find` swapped for `replace`. The match must be exact (or
    exact apart from trailing spaces); when it appears more than once, the one
    nearest the failing line is used. None when it isn't there."""
    if not find.strip():
        return None
    spots = [m.start() for m in re.finditer(re.escape(find), source)]
    if not spots:
        loose = "\n".join(l.rstrip() for l in source.split("\n"))
        want = "\n".join(l.rstrip() for l in find.split("\n"))
        if loose.count(want) != 1:
            return None
        source, find = loose, want
        spots = [source.find(find)]
    at = min(spots, key=lambda s: abs(source.count("\n", 0, s) + 1 - line))
    return source[:at] + replace + source[at + len(find):]


def Check(rel: str, source: str) -> Optional[str]:
    """Why `source` can't be written, or None when it parses and compiles."""
    try:
        compile(ast.parse(source, rel), rel, "exec")
    except SyntaxError as e:
        return f"the fixed file would not parse (line {e.lineno}: {e.msg})"
    except Exception as e:
        return f"the fixed file would not compile: {e}"
    return None


_PROMPT = """You are fixing a bug in Orion, a Python desktop assistant.
It crashed in {rel} around line {line}{func}.
Error: {error}
{notes}
Traceback (last part):
{tb}

The code:
```python
{context}
```

Give the SMALLEST change that stops this error without changing what the code is for: a None
check, a missing key default, a safe conversion, a bounds check. Keep the file's style. Never
delete features, never silence every error with a bare except, never touch unrelated lines.
Reply with ONLY this JSON:
{{"explanation": "one plain sentence: what was wrong and what the fix does",
  "find": "the exact lines to replace, copied character for character from the code above",
  "replace": "the new lines"}}
If no safe small fix exists, reply {{"explanation": "why not", "find": "", "replace": ""}}."""


def Draft(tb: str = "", notes: str = "", ask: Optional[Callable[[str], Optional[dict]]] = None) -> Fix | str:
    """A checked fix for `tb` (the last failure when empty), or a sentence saying
    why there isn't one. `ask` is the model call; tests pass their own."""
    tb = tb or LastFailure().get("traceback", "")
    if not tb.strip():
        return "There's no recent error to fix. Tell me what broke, or paste the error."
    place = Locate(tb)
    if place is None:
        return "That error didn't happen in Orion's own code (or only in a file I'm not allowed to change)."
    try:
        source = place.path.read_text(encoding="utf-8")
    except OSError as e:
        return f"I couldn't read {place.rel}: {e}"
    if ask is None:
        from Brain import Gemini

        def ask(prompt):
            return Gemini.AsJson(prompt, tier=Gemini.SMART, timeout_ms=60_000)
    prompt = _PROMPT.format(rel=place.rel, line=place.line,
                            func=f" in {place.func}()" if place.func else "", error=place.error,
                            notes=f"What the user said: {notes}\n" if notes else "",
                            tb=tb.strip()[-2500:], context=Context(source, place.line)[:12000])
    reply = ask(prompt)
    if not isinstance(reply, dict):
        return "I couldn't reach a model to work out a fix."
    find, replace = str(reply.get("find") or ""), str(reply.get("replace") or "")
    why = str(reply.get("explanation") or "").strip() or "Fixes the crash."
    if not find.strip():
        return f"I don't see a safe small fix: {why}"
    if find == replace:
        return "The fix I came up with didn't change anything."
    if max(len(find.splitlines()), len(replace.splitlines())) > MAX_CHANGE_LINES:
        return "The only fix I found was too big to make safely on my own."
    staged = Stage(source, find, replace, place.line)
    if staged is None:
        return "The fix didn't match the code exactly, so I didn't use it."
    problem = Check(place.rel, staged)
    if problem:
        return f"I didn't use the fix: {problem}."
    return Fix(place, why, find, replace, staged, _Digest(source))


# ── applying, and taking back ───────────────────────────────────────────────

def _Module(rel: str) -> str:
    return rel[:-3].replace("/", ".")


def ImportProblem(rel: str) -> Optional[str]:
    """Import the module in a separate process; why it failed, or None."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", QT_QPA_PLATFORM="offscreen")
    try:
        r = subprocess.run([sys.executable, "-c", f"import {_Module(rel)}"], cwd=str(ROOT), env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=IMPORT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None          # slow to import is not broken
    except OSError as e:
        return f"couldn't start Python: {e}"
    if r.returncode == 0:
        return None
    last = [l for l in (r.stderr or r.stdout).strip().splitlines() if l.strip()]
    return last[-1][:200] if last else f"exit code {r.returncode}"


def _LoadHistory() -> list[dict]:
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _SaveHistory(items: list[dict]) -> None:
    REPAIR_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(items[-50:], indent=2), encoding="utf-8")


def Apply(fix: Fix, import_check: Callable[[str], Optional[str]] = ImportProblem) -> str:
    """Back up, write, check it still imports (putting the backup back if not),
    and record it. Returns what happened, in a sentence."""
    from Brain.Undo import PushUndo
    path, rel = fix.place.path, fix.place.rel
    try:
        current = path.read_text(encoding="utf-8")
    except OSError as e:
        return f"Couldn't read {rel}: {e}"
    if _Digest(current) != fix.digest:
        return f"{rel} changed since the fix was drafted, so I left it alone."
    REPAIR_DIR.mkdir(parents=True, exist_ok=True)
    backup = REPAIR_DIR / f"{path.stem}.{time.strftime('%Y%m%d-%H%M%S')}.bak.py"
    shutil.copy2(path, backup)
    path.write_text(fix.new_source, encoding="utf-8")
    problem = import_check(rel)
    if problem:
        shutil.copy2(backup, path)
        return f"The fix broke {rel} ({problem}), so I put the original back."
    entry = {"id": backup.stem.split(".", 1)[1].replace(".bak", ""), "file": rel,
             "backup": backup.name, "line": fix.place.line, "error": fix.place.error,
             "explanation": fix.explanation, "at": time.time(), "status": "applied"}
    history = _LoadHistory()
    history.append(entry)
    _SaveHistory(history)
    PushUndo(f"repaired {path.name}", lambda e=entry: RollBack(e["id"]))
    return f"Repaired {rel}: {fix.explanation} It takes effect when Orion restarts."


def RollBack(repair_id: str = "") -> str:
    """Put a repaired file back as it was (the latest repair when no id)."""
    history = _LoadHistory()
    live = [e for e in history if e.get("status") == "applied"]
    entry = next((e for e in reversed(live) if not repair_id or e.get("id") == repair_id), None)
    if entry is None:
        return "There's no repair to take back."
    backup = REPAIR_DIR / entry["backup"]
    if not backup.is_file():
        return f"The backup for {entry['file']} is missing, so I can't take it back."
    shutil.copy2(backup, ROOT / entry["file"])
    entry["status"] = "rolled back"
    _SaveHistory(history)
    return f"Took back the repair to {entry['file']}. That takes effect when Orion restarts."


def History(limit: int = 5) -> list[dict]:
    return list(reversed(_LoadHistory()))[:limit]


# ── the tool ────────────────────────────────────────────────────────────────

TOOL = {
    "name": "self_repair",
    "description": (
        "Repairs a bug in Orion's own code, with the user's say-so. Use when the user asks you to "
        "fix yourself / repair that / fix the bug, usually after a tool reported a bug in Orion's "
        "own code. 'fix' drafts a small change and puts it on screen for the user to confirm; "
        "nothing changes without that. 'history' lists past repairs; 'rollback' takes the last "
        "one back. Never call 'fix' on your own initiative."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "fix | history | rollback"},
            "error": {"type": "STRING", "description": "An error the user pasted, if any. Empty = the last crash."},
            "notes": {"type": "STRING", "description": "What the user said went wrong, if anything."},
        },
        "required": ["action"],
    },
}


def Run(args: dict, show: Callable[[str, str], None], say: Callable[[str], None],
        draft: Callable[..., Fix | str] = Draft) -> str:
    """`self_repair`. 'fix' works in the background (drafting takes a while)
    and ends at the confirmation banner, so the reply here only says it started."""
    action = str(args.get("action", "fix") or "fix").lower().strip()
    if action in ("history", "list"):
        items = History()
        if not items:
            return "No repairs yet."
        return "Recent repairs, newest first:\n" + "\n".join(
            f"- {e['file']} line {e['line']}: {e['explanation']} ({e['status']})" for e in items)
    if action in ("rollback", "undo", "revert"):
        return RollBack(str(args.get("id", "") or ""))

    tb, notes = str(args.get("error", "") or ""), str(args.get("notes", "") or "")
    if not (tb or LastFailure().get("traceback")):
        return "There's no recent error to fix. Ask the user what broke, or to paste the error."

    def Told(result: str) -> str:
        say(f"[SELF_REPAIR] {result} Tell the user that in one short sentence.")
        return result

    def Work():
        from Brain import Confirm
        fix = draft(tb, notes)
        if isinstance(fix, str):
            say(f"[SELF_REPAIR] {fix} Tell the user that in one short sentence.")
            return
        show(f"FIX — {fix.place.rel}", f"{fix.explanation}\n\n{fix.Diff()}")
        said = Confirm.request("self_repair", f"Apply a fix to {fix.place.path.name}?",
                               f"{fix.explanation} The change is in the panel below.",
                               lambda f=fix: Told(Apply(f)))
        say(f"[SELF_REPAIR] A fix is ready: {fix.explanation} {said}")

    threading.Thread(target=Work, daemon=True, name="self-repair").start()
    return ("Working out a fix now. It can take up to a minute; I'll put it on screen to confirm. "
            "Tell the user that in a few words.")
