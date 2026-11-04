import subprocess
import sys
import json
import re
import time
from pathlib import Path


def GetBaseDir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR           = GetBaseDir()
API_CONFIG_PATH    = BASE_DIR / "Config" / "ApiKeys.json"
DESKTOP            = Path.home() / "Desktop"
MAX_BUILD_ATTEMPTS = 3
# Model choice lives in core/gemini.py, and so does the timeout and the
# fallback ladder. Writing a model name here is what left this file hanging
# forever whenever that one alias was unwell.
from Brain import Gemini


def GetApiKey() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def GetGemini(tier: str = Gemini.SMART):
    """Writing and fixing code is the reasoning tier; a 60s deadline because a
    whole file can come back."""
    class _W:
        def generate_content(self, contents):
            resp = Gemini.call(contents, tier=tier, timeout_ms=60000)
            if resp is None:
                raise RuntimeError("every Gemini model on the ladder failed")
            return resp

    return _W()


def CleanCode(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def ResolveSavePath(output_path: str, language: str) -> Path:
    ext_map = {
        "python": ".py", "py": ".py",
        "javascript": ".js", "js": ".js",
        "typescript": ".ts", "ts": ".ts",
        "html": ".html", "css": ".css",
        "java": ".java", "cpp": ".cpp", "c": ".c",
        "bash": ".sh", "shell": ".sh", "powershell": ".ps1",
        "sql": ".sql", "json": ".json", "rust": ".rs", "go": ".go",
    }
    if output_path:
        p = Path(output_path).expanduser()
        p = p if p.is_absolute() else DESKTOP / p
        _CheckWritable(p)
        return p
    ext = ext_map.get((language or "python").lower(), ".py")
    return DESKTOP / f"orion_code{ext}"


def _CheckWritable(path: Path) -> None:
    """Same rule as file_controller: inside home, never AppData / Startup /
    dot-folders / the PowerShell profile. Raises ValueError otherwise."""
    from Actions.FileController import IsSafePath
    if not IsSafePath(path):
        raise ValueError(f"I can't write code to {path} - only inside your own "
                         f"folders, not system or app-data folders.")


def _ConfirmRun(title: str, run) -> str:
    """Running code the model chose waits for the user's on-screen confirm."""
    from Brain import Confirm
    if Confirm.PendingTitle():
        return ("Not run: another confirmation is already waiting on screen - "
                "the user has to answer that one first.")
    return Confirm.request("code_run", title[:120],
                           "Orion will run this code on your computer. Confirm only if you asked for it.",
                           run)


def _ReadFile(file_path: str) -> tuple[str, str]:
    if not file_path:
        return "", "No file path provided."
    p = Path(file_path)
    if not p.exists():
        return "", f"File not found: {file_path}"
    try:
        return p.read_text(encoding="utf-8"), ""
    except Exception as e:
        return "", f"Could not read file: {e}"


def _SaveFile(path: Path, content: str) -> str:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Saved to: {path}"
    except Exception as e:
        return f"Could not save: {e}"


def _SaveWithUndo(path: Path, content: str, label: str) -> str:
    """Save over a file, keeping a .bak copy and an undo of the old contents."""
    previous = None
    try:
        if path.exists():
            previous = path.read_text(encoding="utf-8")
            try:
                path.with_name(path.name + ".bak").write_text(previous, encoding="utf-8")
            except Exception as e:
                print(f"[Code] backup failed: {e}")
    except Exception as e:
        return f"Could not read the old file, so I left it alone: {e}"
    status = _SaveFile(path, content)
    if status.startswith("Saved"):
        try:
            from Brain.Undo import PushUndo
            from Actions.FileController import UndoWrite
            PushUndo(f"{label} {path.name}", UndoWrite(path, previous))
        except Exception as e:
            print(f"[Code] undo push failed: {e}")
        if previous is not None:
            status += f" (old version kept as {path.name}.bak; say undo to restore it)"
    return status


def _ParseArgs(args) -> list:
    """`args` arrives as a STRING from the tool call; accept a list too."""
    if not args:
        return []
    if isinstance(args, (list, tuple)):
        return [str(a) for a in args]
    import shlex
    try:
        if sys.platform != "win32":
            return shlex.split(str(args))
        # Non-posix keeps Windows backslashes; drop the quotes it leaves on.
        return [a[1:-1] if len(a) > 1 and a[0] == a[-1] and a[0] in "\"'" else a
                for a in shlex.split(str(args), posix=False)]
    except ValueError:
        return str(args).split()


def _preview(code: str, lines: int = 10) -> str:
    all_lines = code.splitlines()
    preview   = "\n".join(all_lines[:lines])
    suffix    = f"\n... ({len(all_lines) - lines} more lines)" if len(all_lines) > lines else ""
    return preview + suffix


def HasError(output: str) -> bool:
    """Did the run fail? Judged by the exit code and a traceback, not by the
    word "error" anywhere in the output ("0 failed" is a passing run)."""
    low = output.lower()
    if re.search(r"^exit code: (?!0\b)", output, re.MULTILINE | re.IGNORECASE):
        return True
    if "traceback (most recent call last)" in low:
        return True
    return low.startswith(("timed out", "interpreter not found",
                           "execution error", "no interpreter"))


def _TakeScreenshot() -> Path | None:
    try:
        import pyautogui
        screenshot_path = Path.home() / "Desktop" / f"orion_debug_{int(time.time())}.png"
        screenshot = pyautogui.screenshot()
        screenshot.save(str(screenshot_path))
        print(f"[Code] 📸 Screenshot: {screenshot_path}")
        return screenshot_path
    except Exception as e:
        print(f"[Code] ⚠️ Screenshot failed: {e}")
        return None


def ImageToBase64(path: Path) -> str:
    import base64
    return base64.b64encode(path.read_bytes()).decode("utf-8")


_VALID_INTENTS = {"write", "edit", "explain", "run", "build", "screen_debug", "optimize"}


def DetectIntent(description: str, file_path: str, code: str) -> str:
    """
    Language-independent intent detection — NO fixed keyword list.
    Whatever language the user speaks, the description is classified by
    Gemini. If the API is unreachable, it falls back to language-agnostic
    structural hints (does the file exist on disk, was code provided).
    """
    desc        = (description or "").strip()
    file_exists = bool(file_path) and Path(file_path).exists()

    if desc:
        try:
            ctx = []
            if file_path:
                ctx.append(f"a file path is provided (exists on disk: {file_exists})")
            if code:
                ctx.append("an inline code snippet is provided")
            prompt = (
                "Classify a coding assistant request into exactly ONE intent word.\n"
                "The request may be written in ANY language.\n\n"
                f"Request: {desc}\n"
                + (f"Context: {'; '.join(ctx)}\n" if ctx else "")
                + "\nIntents:\n"
                "  write        = create new code from scratch\n"
                "  edit         = modify an existing file\n"
                "  explain      = describe what given code/file does\n"
                "  run          = execute an existing file\n"
                "  build        = write code, run it, and iterate until it works\n"
                "  screen_debug = analyze an error currently visible on the user's screen\n"
                "  optimize     = refactor / clean up / speed up existing code\n\n"
                "Reply with ONLY the intent word, nothing else."
            )
            ans = GetGemini().generate_content(prompt).text.strip().lower()
            ans = ans.strip("`'\". \n")
            if ans in _VALID_INTENTS:
                return ans
        except Exception as e:
            print(f"[Code] Intent classification failed ({e}) — structural fallback")

    # Structural fallback — not tied to any language
    if file_exists:
        return "edit" if desc else "explain"
    if code:
        return "explain"
    return "write"

def _Write(description: str, language: str, output_path: str, player=None) -> tuple[str, Path]:
    lang  = language or "python"
    model = GetGemini()

    prompt = f"""You are an expert {lang} developer.
Write clean, working, well-commented {lang} code for the description below.

Rules:
- Output ONLY the code. No explanation, no markdown, no backticks.
- Add helpful inline comments.
- Handle errors and edge cases properly.
- Use modern best practices.

Description: {description}

Code:"""

    response = model.generate_content(prompt)
    code     = CleanCode(response.text)
    path     = ResolveSavePath(output_path, lang)
    status   = _SaveWithUndo(path, code, "wrote")
    if not status.startswith("Saved"):
        raise RuntimeError(status)
    return code, path


def FixCode(code: str, error_output: str, description: str) -> str:
    model  = GetGemini()
    prompt = f"""You are an expert debugger.
The code below failed with the following error. Fix it.
Return ONLY the corrected code — no explanation, no markdown, no backticks.

Original goal: {description}

Error:
{error_output[:2000]}

Broken code:
{code}

Fixed code:"""

    response = model.generate_content(prompt)
    return CleanCode(response.text)


def RunFile(path: Path, args: list, timeout: int) -> str:
    interpreters = {
        ".py":  [sys.executable],
        ".js":  ["node"],
        ".ts":  ["ts-node"],
        ".sh":  ["bash"],
        ".ps1": ["powershell", "-File"],
        ".rb":  ["ruby"],
        ".php": ["php"],
    }
    interp = interpreters.get(path.suffix.lower())
    if not interp:
        return f"No interpreter for {path.suffix}."

    try:
        result = subprocess.run(
            interp + [str(path)] + (args or []),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(path.parent)
        )
        output = result.stdout.strip()
        error  = result.stderr.strip()
        parts  = []
        if output: parts.append(f"Output:\n{output}")
        if error:  parts.append(f"Stderr:\n{error}")
        if result.returncode != 0:
            parts.append(f"Exit code: {result.returncode}")
        return "\n\n".join(parts) if parts else "Executed with no output."

    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s."
    except FileNotFoundError:
        return f"Interpreter not found: {interp[0]}."
    except Exception as e:
        return f"Execution error: {e}"


def Build(description, language, output_path, args, timeout, speak=None, player=None) -> str:
    if not description:
        return "Please describe what you want me to build, sir."

    if player:
        player.WriteLog("[Code] Build started...")

    lang = language or "python"

    try:
        code, path = _Write(description, lang, output_path, player)
        print(f"[Code] ✅ Written: {path}")
    except Exception as e:
        msg = f"Could not write initial code: {e}"
        if speak: speak(msg)
        return msg

    # Writing is undoable; running is not. The run-and-fix loop waits for the
    # user's on-screen confirm, then speaks its result.
    return _ConfirmRun(f"Run the code just built: {path.name}",
                       lambda: _BuildLoop(code, path, description, args, timeout, speak, player)) + \
        f"\n\nCode written to {path}.\nPreview:\n{_preview(code)}"


def _BuildLoop(code, path, description, args, timeout, speak=None, player=None) -> str:
    last_output = ""
    for attempt in range(1, MAX_BUILD_ATTEMPTS + 1):
        print(f"[Code] 🔄 Attempt {attempt}/{MAX_BUILD_ATTEMPTS}")
        if player:
            player.WriteLog(f"[Code] Attempt {attempt}...")

        last_output = RunFile(path, args, timeout)

        if not HasError(last_output):
            msg = (
                f"Build complete, sir. "
                f"The code is working after {attempt} attempt{'s' if attempt > 1 else ''}. "
                f"Saved to {path}."
            )
            if speak: speak(msg)
            return f"{msg}\n\nOutput:\n{last_output}"

        print(f"[Code] ⚠️ Error on attempt {attempt}, fixing...")
        if player:
            player.WriteLog(f"[Code] Fixing (attempt {attempt})...")

        try:
            code = FixCode(code, last_output, description)
            _SaveFile(path, code)
        except Exception as e:
            msg = f"Could not fix code on attempt {attempt}: {e}"
            if speak: speak(msg)
            return msg

    msg = (
        f"I was unable to build a working version after {MAX_BUILD_ATTEMPTS} attempts, sir. "
        f"The last error was: {last_output[:200]}"
    )
    if speak: speak(msg)
    return f"{msg}\n\nLast code saved to: {path}"

def WriteAction(description, language, output_path, player) -> str:
    if not description:
        return "Please describe what you want me to write, sir."
    if player:
        player.WriteLog("[Code] Writing code...")
    try:
        code, path = _Write(description, language, output_path, player)
        print(f"[Code] ✅ Written: {path}")
        return f"Code written. Saved to: {path}\n\nPreview:\n{_preview(code)}"
    except Exception as e:
        return f"Could not generate code: {e}"


def EditAction(file_path, instruction, player) -> str:
    if not file_path:
        return "Please provide a file path to edit, sir."
    if not instruction:
        return "Please describe what change to make, sir."

    content, err = _ReadFile(file_path)
    if err:
        return err

    if player:
        player.WriteLog("[Code] Editing file...")

    model  = GetGemini()
    prompt = f"""You are an expert code editor.
Apply the following change to the code below.
Return ONLY the complete updated code — no explanation, no markdown, no backticks.

Change: {instruction}

Original code:
{content}

Updated code:"""

    try:
        response = model.generate_content(prompt)
        edited   = CleanCode(response.text)
    except Exception as e:
        return f"Could not edit code: {e}"

    if not edited:
        return "The edit came back empty, so I left the file unchanged."
    try:
        _CheckWritable(Path(file_path))
    except ValueError as e:
        return str(e)
    status = _SaveWithUndo(Path(file_path), edited, "edited")
    print(f"[Code] ✅ Edited: {file_path}")
    return f"File edited. {status}\n\nPreview:\n{_preview(edited)}"


def ExplainAction(file_path, code, player) -> str:
    if file_path and not code:
        code, err = _ReadFile(file_path)
        if err:
            return err
    if not code:
        return "Please provide code or a file path to explain, sir."

    if player:
        player.WriteLog("[Code] Analyzing code...")

    model  = GetGemini()
    prompt = f"""Explain what this code does in simple, clear language.
Focus on: what it does, how it works, and any important details.
Be concise — 3 to 6 sentences maximum.

Code:
{code[:4000]}

Explanation:"""

    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        return f"Could not explain code: {e}"


def RunAction(file_path, args, timeout, player) -> str:
    if not file_path:
        return "Please provide a file path to run, sir."
    p = Path(file_path)
    if not p.exists():
        return f"File not found: {file_path}"

    def Go() -> str:
        if player:
            player.WriteLog(f"[Code] Running {p.name}...")
        return RunFile(p, args, timeout)

    shown_args = (" " + " ".join(args)) if args else ""
    return _ConfirmRun(f"Run {p.name}{shown_args}", Go)


_OPTIMIZE_LIMIT = 12000   # chars the model sees whole


def OptimizeAction(file_path, code, language, output_path, player) -> str:

    if file_path and not code:
        code, err = _ReadFile(file_path)
        if err:
            return err
    if not code:
        return "Please provide code or a file path to optimize, sir."
    # The whole file must fit in the prompt, or the result is a cut-down file.
    if len(code) > _OPTIMIZE_LIMIT:
        return (f"That code is {len(code)} characters, more than the {_OPTIMIZE_LIMIT} I can "
                f"optimize in one go without losing part of it. Nothing was changed. "
                f"Try a smaller file or one function at a time.")

    if player:
        player.WriteLog("[Code] Optimizing code...")

    lang  = language or "python"
    model = GetGemini()

    prompt = f"""You are an expert {lang} developer and code reviewer.
Optimize the following code for:
1. Performance — eliminate unnecessary operations, use efficient data structures
2. Readability — clear variable names, proper formatting, logical structure
3. Best practices — modern {lang} patterns, error handling, type hints if applicable
4. Remove dead code, redundant comments, and unnecessary complexity

Return ONLY the optimized code — no explanation, no markdown, no backticks.

Original code:
{code}

Optimized code:"""

    try:
        response  = model.generate_content(prompt)
        optimized = CleanCode(response.text)
    except Exception as e:
        return f"Could not optimize code: {e}"

    if not optimized:
        return "The optimized code came back empty, so nothing was changed."

    try:
        if file_path:
            save_path = Path(file_path)
            _CheckWritable(save_path)
        else:
            save_path = ResolveSavePath(output_path, lang)
    except ValueError as e:
        return str(e)

    status = _SaveWithUndo(save_path, optimized, "optimized")
    print(f"[Code] ✅ Optimized: {save_path}")

    original_lines  = len(code.splitlines())
    optimized_lines = len(optimized.splitlines())
    diff = original_lines - optimized_lines

    return (
        f"Code optimized. {status}\n"
        f"Lines: {original_lines} → {optimized_lines} "
        f"({'−' if diff > 0 else '+'}{abs(diff)} lines)\n\n"
        f"Preview:\n{_preview(optimized)}"
    )


def _LooksLikeWholeFile(block: str, original: str) -> bool:
    """True only if the model saw the whole file and the block is about as long."""
    if len(original) > 4000:          # the prompt only carried the first 4000 chars
        return False
    want = len(original.strip().splitlines())
    got  = len(block.strip().splitlines())
    if want == 0:
        return False
    return max(3, int(want * 0.8)) <= got <= want * 2 + 20


def ScreenDebugAction(description, file_path, player, speak=None) -> str:

    if player:
        player.WriteLog("[Code] Taking screenshot for analysis...")

    print("[Code] 📸 Capturing screen for debug...")


    screenshot_path = _TakeScreenshot()
    if not screenshot_path:
        return "Could not take screenshot, sir. Please make sure PyAutoGUI is installed."


    file_content = ""
    if file_path:
        file_content, err = _ReadFile(file_path)
        if err:
            print(f"[Code] ⚠️ Could not read file: {err}")

    try:
        from google.genai import types

        image_bytes  = screenshot_path.read_bytes()
        image_base64 = ImageToBase64(screenshot_path)

        user_question = description or "What error or problem do you see on the screen? How can it be fixed?"

        context = ""
        if file_content:
            context = f"\n\nAdditionally, here is the related file content:\n```\n{file_content[:4000]}\n```"

        analysis_prompt = f"""You are an expert programmer and debugger analyzing a screenshot.

User's question: {user_question}{context}

Please:
1. Identify any errors, exceptions, or problems visible on the screen
2. Explain what is causing the problem in simple terms
3. Provide a concrete fix or solution
4. If there's code visible, show the corrected version

Be specific and actionable. If you see an error message, quote it exactly."""

        contents = [
            types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
            analysis_prompt,
        ]

        response = Gemini.call(contents, tier=Gemini.SMART, timeout_ms=45_000)
        if response is None:
            return "Sir, I couldn't reach Gemini to analyse that screenshot."

        analysis = (response.text or "").strip()
        print(f"[Code] ✅ Screen analysis complete")

        try:
            screenshot_path.unlink()
        except Exception:
            pass

        if file_path and file_content:

            code_match = re.search(r"```[a-zA-Z]*\n(.*?)```", analysis, re.DOTALL)
            if code_match:
                fixed_code = code_match.group(1).strip()
                try:
                    _CheckWritable(Path(file_path))
                    writable = True
                except ValueError:
                    writable = False
                if writable and _LooksLikeWholeFile(fixed_code, file_content):
                    status = _SaveWithUndo(Path(file_path), fixed_code, "screen-debug fixed")
                    analysis += f"\n\n✅ Fixed code: {status}"
                    print(f"[Code] ✅ Fixed code saved: {file_path}")
                else:
                    # Only a snippet: writing it would replace the whole file.
                    analysis += (f"\n\nI did not change {Path(file_path).name}: the fix above "
                                 f"is only part of the file, so apply it by hand.")

        return analysis

    except Exception as e:

        try:
            screenshot_path.unlink()
        except Exception:
            pass
        return f"Screen analysis failed: {e}"


def CodeHelper(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None
) -> str:
    """
    Called from Main.py.

    parameters:
        action      : write | edit | explain | run | build | screen_debug | optimize | auto
        description : What the code should do / what change to make / what problem to analyze
        language    : Programming language (default: python)
        output_path : Where to save — user specifies full path or filename
        file_path   : Path to existing file (edit / explain / run / build / optimize)
        code        : Raw code string (explain/optimize without a file)
        args        : CLI argument list for run/build
        timeout     : Execution timeout in seconds (default: 30)
    """
    p           = parameters or {}
    action      = p.get("action", "auto").lower().strip()
    description = p.get("description", "").strip()
    language    = p.get("language", "python").strip()
    output_path = p.get("output_path", "").strip()
    file_path   = p.get("file_path", "").strip()
    code        = p.get("code", "").strip()
    args        = _ParseArgs(p.get("args", []))
    timeout     = int(p.get("timeout", 30))

    if action == "auto":
        action = DetectIntent(description, file_path, code)
        print(f"[Code] 🤖 Auto-detected: {action}")

    if action == "write":
        return WriteAction(description, language, output_path, player)

    elif action == "edit":
        return EditAction(
            file_path,
            description or p.get("instruction", ""),
            player
        )

    elif action == "explain":
        return ExplainAction(file_path, code, player)

    elif action == "run":
        return RunAction(file_path, args, timeout, player)

    elif action == "build":
        return Build(description, language, output_path, args, timeout, speak, player)

    elif action == "optimize":
        return OptimizeAction(file_path, code, language, output_path, player)

    elif action == "screen_debug":
        return ScreenDebugAction(description, file_path, player, speak)

    else:
        return f"Unknown action: '{action}'. Use write, edit, explain, run, build, optimize, or screen_debug."


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "code_helper",
    "description": "Writes, edits, explains, runs, or builds code files.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "write | edit | explain | run | build | auto (default: auto)"
            },
            "description": {
                "type": "STRING",
                "description": "What the code should do or what change to make"
            },
            "language": {
                "type": "STRING",
                "description": "Programming language (default: python)"
            },
            "output_path": {
                "type": "STRING",
                "description": "Where to save the file"
            },
            "file_path": {
                "type": "STRING",
                "description": "Path to existing file for edit/explain/run/build"
            },
            "code": {
                "type": "STRING",
                "description": "Raw code string for explain"
            },
            "args": {
                "type": "STRING",
                "description": "CLI arguments for run/build"
            },
            "timeout": {
                "type": "INTEGER",
                "description": "Execution timeout in seconds (default: 30)"
            }
        },
        "required": [
            "action"
        ]
    },
    "handler": CodeHelper,
}
