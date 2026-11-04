"""The fix round of 2026-11-04 (ISSUES.md), the parts outside Main.py / Window.py: safe settings and memory
files, code edits that never cut a file down, Skill Forge's scan and its wait for a yes, no injection, the
phone's yes for the right question, private mode in Telegram and Gemini, the engines, and the small fixes.

Settings, memory, Telegram, the phone link and every file a fix writes go to temp folders. No model, network,
microphone, camera, keystrokes or scheduled task is used: each of those is a stand-in here."""
import asyncio
import base64
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import Tests.Helpers  # noqa: F401  (paths)
from Tests.Helpers import ROOT
from Brain import Confirm, Memory, Privacy, Settings, Undo


def _WaitFor(cond, seconds=5.0):
    end = time.monotonic() + seconds
    while not cond() and time.monotonic() < end:
        time.sleep(0.02)
    return cond()


class _TempConfig(unittest.TestCase):
    """Settings and memory in a throwaway folder (the same way TestBrain does it)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        saved = (Settings.CONFIG_DIR, Settings.CONFIG_FILE, Memory.MEMORY_PATH)
        Settings.CONFIG_DIR = self.tmp
        Settings.CONFIG_FILE = self.tmp / "ApiKeys.json"
        Memory.MEMORY_PATH = self.tmp / "LongTerm.json"

        def Back():
            Settings.CONFIG_DIR, Settings.CONFIG_FILE, Memory.MEMORY_PATH = saved
        self.addCleanup(Back)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        Undo.clear()
        self.addCleanup(Undo.clear)


# ── H2: settings saves ──────────────────────────────────────────────────────

class TestSettingsFile(_TempConfig):
    def TestEightThreadsLoseNoKeys(self):
        def Writer(n):
            for i in range(10):
                Settings.PatchConfig(**{f"t{n}_{i}": i})
        threads = [threading.Thread(target=Writer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        data = Settings.LoadApiKeys()
        self.assertEqual(len([k for k in data if k.startswith("t")]), 80)
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])          # no temp files left behind

    def TestABrokenFileKeepsTheLastGoodCopy(self):
        Settings.PatchConfig(gemini_api_key="k" * 30)
        Privacy.Save(on=True, kinds=["relationships"])
        Settings.SaveBrainMode("local")
        self.assertTrue(Privacy.Get()["on"])                          # a good read first
        Settings.CONFIG_FILE.write_text('{"gemini_api_key": "kkk', encoding="utf-8")   # half written
        self.assertEqual(Settings.GetGeminiKey(), "k" * 30)
        self.assertTrue(Privacy.Get()["on"])                          # privacy stays on
        self.assertTrue(Privacy.Active())
        self.assertEqual(Settings.GetBrainMode(), "local")            # not "auto"

    def TestABrokenFileFallsBackToTheBackupFile(self):
        Privacy.Save(on=True)
        self.assertTrue((self.tmp / "ApiKeys.bak.json").exists())
        Settings.CONFIG_FILE.write_text("garbage", encoding="utf-8")
        Settings._last_good.pop(str(Settings.CONFIG_FILE), None)      # as after a restart
        self.assertTrue(Privacy.Get()["on"])

    def TestAWriteOverABrokenFileKeepsEverything(self):
        Settings.PatchConfig(gemini_api_key="k" * 30, user_name="Abhi")
        Settings.LoadApiKeys()
        Settings.CONFIG_FILE.write_text("{broken", encoding="utf-8")
        Settings.SaveVoice("Puck")
        data = json.loads(Settings.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual((data["gemini_api_key"], data["user_name"]), ("k" * 30, "Abhi"))
        self.assertEqual((self.tmp / "ApiKeys.broken.json").read_text(encoding="utf-8"), "{broken")

    def TestScreenProcessorSavesThroughSettings(self):
        # M15: its own key save no longer wipes the others.
        from Actions import ScreenProcessor
        Settings.PatchConfig(gemini_api_key="k" * 30, privacy={"on": True})
        ScreenProcessor.SaveConfigKey("camera_index", 2)
        data = Settings.LoadApiKeys()
        self.assertEqual((data["gemini_api_key"], data["camera_index"], data["privacy"]["on"]),
                         ("k" * 30, 2, True))


# ── H3 / H10 / L26: memory saves and what Gemini gets ────────────────────────

class TestMemoryFile(_TempConfig):
    def TestABrokenFileIsNotWrittenOverWithEmpty(self):
        Memory.remember("sister", "Dilsha", "relationships")
        Memory.remember("tea", "black", "preferences")                # the backup now has the sister
        Memory.MEMORY_PATH.write_text('{"relationships": {"sis', encoding="utf-8")
        mem = Memory.LoadMemory()
        self.assertIn("sister", mem["relationships"])                 # read from LongTerm.bak.json
        Memory.remember("city", "Kochi", "identity")
        mem = Memory.LoadMemory()
        self.assertIn("sister", mem["relationships"])
        self.assertIn("city", mem["identity"])
        broken = list(self.tmp.glob("LongTerm.broken-*.json"))
        self.assertEqual(len(broken), 1)                              # the unreadable file is set aside
        self.assertEqual(broken[0].read_text(encoding="utf-8"), '{"relationships": {"sis')

    def TestNoBackupStillKeepsTheBrokenFile(self):
        Memory.MEMORY_PATH.write_text("not json", encoding="utf-8")
        Memory.remember("x", "1")
        broken = list(self.tmp.glob("LongTerm.broken-*.json"))
        self.assertEqual([b.read_text(encoding="utf-8") for b in broken], ["not json"])

    def TestConcurrentUpdatesKeepAll(self):
        def Writer(n):
            for i in range(5):
                Memory.UpdateMemory({"notes": {f"n{n}_{i}": {"value": str(i)}}})
        threads = [threading.Thread(target=Writer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(Memory.LoadMemory()["notes"]), 40)

    def TestLocalSessionsNeverGoToGemini(self):
        Memory.SaveSessionSummary("We planned the Goa trip.")
        Memory.SaveSessionSummary("A private on-device chat about money.", local=True)
        out = Privacy.ForGemini(Memory.LoadMemory())                  # private mode off
        self.assertEqual([s["summary"] for s in out["sessions"]], ["We planned the Goa trip."])
        Privacy.Save(on=True)
        out = Privacy.ForGemini(Memory.LoadMemory())
        self.assertNotIn("money", json.dumps(out))
        got = Memory.PopLastSession(skip_local=True)
        self.assertEqual(got["summary"], "We planned the Goa trip.")
        self.assertIsNone(Memory.PopLastSession(skip_local=True))    # only the local one is left
        self.assertEqual(len(Memory.LoadMemory()["sessions"]), 1)     # ...and it's still there

    def TestAMultiLinePrivateValueIsHiddenWhole(self):
        Memory.remember("sister", "Dilsha\nlives in Kochi\nphone 98470", "relationships")
        Memory.remember("tea", "black", "preferences")
        text = Memory.SearchMemory("")
        keep, hidden = Privacy.SplitMemory(text, ["relationships"])
        self.assertNotIn("Kochi", keep)
        self.assertNotIn("98470", keep)
        self.assertIn("tea: black", keep.lower())
        self.assertIn("Kochi", hidden)
        # L26 the other way: a value split over lines in recall text is still caught.
        keep, hidden = Privacy.SplitMemory("Everything:\nrelationships/Sister: Dilsha\n  lives in Kochi\n"
                                           "preferences/Tea: black", ["relationships"])
        self.assertNotIn("Kochi", keep)
        self.assertIn("preferences/Tea: black", keep)


# ── H4 / H5 / M16: code helper ───────────────────────────────────────────────

class _Model:
    def __init__(self, text):
        self.text, self.prompts = text, []

    def generate_content(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(text=self.text)


class TestCodeHelper(_TempConfig):
    def setUp(self):
        super().setUp()
        from Actions import CodeHelper
        from Actions import FileController
        self.CH = CodeHelper
        self.file = self.tmp / "app.py"
        # The temp folder lives in AppData, which the real path rule refuses.
        p = mock.patch.object(FileController, "IsSensitivePath", lambda _p: False)
        p.start()
        self.addCleanup(p.stop)

    def TestRunAndBuildWaitForConfirm(self):
        self.file.write_text("print(1)", encoding="utf-8")
        asked = []
        with mock.patch.object(Confirm, "request", lambda key, title, detail, run: asked.append(run) or "PENDING"), \
                mock.patch.object(Confirm, "PendingTitle", lambda: ""), \
                mock.patch.object(self.CH, "RunFile", lambda *a: "Output:\n1"):
            self.assertEqual(self.CH.RunAction(str(self.file), [], 5, None), "PENDING")
            self.assertEqual(asked[0](), "Output:\n1")      # runs only once confirmed

    def TestHasErrorReadsTheExitCode(self):
        self.assertFalse(self.CH.HasError("Output:\n3 passed, 0 failed, 0 errors"))
        self.assertTrue(self.CH.HasError("Stderr:\nboom\n\nExit code: 1"))
        self.assertTrue(self.CH.HasError("Traceback (most recent call last):\n  x"))
        self.assertTrue(self.CH.HasError("Timed out after 30s."))

    def TestOptimizeRefusesABigFile(self):
        big = "x = 1\n" * 2500                                        # 15,000 chars
        self.file.write_text(big, encoding="utf-8")
        model = _Model("x = 1\n")
        with mock.patch.object(self.CH, "GetGemini", lambda *a, **k: model):
            said = self.CH.OptimizeAction(str(self.file), "", "python", "", None)
        self.assertIn("Nothing was changed", said)
        self.assertEqual(model.prompts, [])
        self.assertEqual(self.file.read_text(encoding="utf-8"), big)

    def TestOptimizeKeepsABackupAndAnUndo(self):
        old = "def f():\n    return  1\n"
        self.file.write_text(old, encoding="utf-8")
        model = _Model("def f():\n    return 1\n")
        with mock.patch.object(self.CH, "GetGemini", lambda *a, **k: model):
            said = self.CH.OptimizeAction(str(self.file), "", "python", "", None)
        self.assertIn(old, model.prompts[0])                          # the model saw the whole file
        self.assertEqual(self.file.read_text(encoding="utf-8"), "def f():\n    return 1")
        self.assertEqual((self.tmp / "app.py.bak").read_text(encoding="utf-8"), old)
        self.assertIn(".bak", said)
        Undo.UndoLast()
        self.assertEqual(self.file.read_text(encoding="utf-8"), old)

    def _ScreenDebug(self, reply):
        shot = self.tmp / "shot.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n")
        with mock.patch.object(self.CH, "_TakeScreenshot", lambda: shot), \
                mock.patch.object(self.CH.Gemini, "call", lambda *a, **k: SimpleNamespace(text=reply)):
            return self.CH.ScreenDebugAction("why does it crash", str(self.file), None)

    def TestScreenDebugNeverWritesASnippet(self):
        old = "".join(f"line_{i} = {i}\n" for i in range(30))
        self.file.write_text(old, encoding="utf-8")
        said = self._ScreenDebug("The bug is on line 3.\n```python\nline_3 = 4\n```")
        self.assertIn("did not change", said)
        self.assertEqual(self.file.read_text(encoding="utf-8"), old)
        self.assertFalse((self.tmp / "app.py.bak").exists())

    def TestScreenDebugWritesAWholeFileWithABackup(self):
        old = "".join(f"line_{i} = {i}\n" for i in range(10))
        self.file.write_text(old, encoding="utf-8")
        fixed = old.replace("line_3 = 3", "line_3 = 4")
        said = self._ScreenDebug(f"Fixed:\n```python\n{fixed}```")
        self.assertIn("Fixed code", said)
        self.assertEqual(self.file.read_text(encoding="utf-8"), fixed.strip())
        self.assertEqual((self.tmp / "app.py.bak").read_text(encoding="utf-8"), old)

    def TestScreenDebugNeverWritesAFileItSawOnlyPartOf(self):
        old = "".join(f"value_number_{i} = {i}\n" for i in range(300))   # over the 4000 chars it sends
        self.file.write_text(old, encoding="utf-8")
        said = self._ScreenDebug(f"```python\n{old}```")
        self.assertIn("did not change", said)
        self.assertEqual(self.file.read_text(encoding="utf-8"), old)

    def TestRunArgsArriveAsAString(self):
        # M16
        self.assertEqual(self.CH._ParseArgs('--name "John Doe" -v'), ["--name", "John Doe", "-v"])
        self.assertEqual(self.CH._ParseArgs(["a", 2]), ["a", "2"])
        self.assertEqual(self.CH._ParseArgs(""), [])
        self.assertEqual(self.CH._ParseArgs(r"C:\temp\in.txt"), [r"C:\temp\in.txt"])


# ── H6 / L12: Skill Forge ────────────────────────────────────────────────────

GOOD_SKILL = textwrap.dedent('''\
    import platform
    PLUGIN = {"name": "coin_flip", "description": "Flips a coin when the user asks to flip a coin.",
              "parameters": {"type": "OBJECT", "properties": {"times": {"type": "INTEGER"}}}}

    def run(parameters, player=None, session_memory=None):
        return f"It landed on heads on {platform.system()}."
''')


class TestSkillForge(unittest.TestCase):
    def setUp(self):
        from System import SkillForge
        self.SF = SkillForge
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name, value in (("PLUGINS_DIR", self.tmp / "Plugins"), ("REMOVED_DIR", self.tmp / "Removed")):
            p = mock.patch.object(SkillForge, name, value)
            p.start()
            self.addCleanup(p.stop)
        (self.tmp / "Plugins").mkdir()

    def TestTheBypassesAreStopped(self):
        bad = [
            "from os import system\nsystem('dir')\n",
            "import os as o\no.remove('x')\n",
            "import os\ngetattr(os, 'sys' + 'tem')('dir')\n",
            "from shutil import rmtree\nrmtree('x')\n",
            "p = 'Phone' + 'Link.json'\n",
            "from os import remove as r\nr('x')\n",
            "import os\nf = os.system\n",
            "s = ''.join(['Api', 'Keys', '.json'])\n",
            "import importlib\nimportlib.import_module('os')\n",
        ]
        for extra in bad:
            self.assertTrue(self.SF.ScanCode(GOOD_SKILL + extra, "coin_flip").problems, extra)
        self.assertEqual(self.SF.ScanCode(GOOD_SKILL, "coin_flip").problems, [])

    def TestDraftDoesNotRunTheCode(self):
        marker = self.tmp / "ran.txt"
        code = GOOD_SKILL + f"open({str(marker)!r}, 'w').write('ran')\n"
        self.assertEqual(self.SF.ScanCode(code, "coin_flip").problems, [])    # it passes the scan...
        with mock.patch.object(self.SF, "TestRun", side_effect=AssertionError("ran before the yes")):
            skill = self.SF.Draft("flip a coin for me",
                                  ask=lambda p: {"name": "coin_flip", "code": code, "example": {}})
        self.assertIsInstance(skill, self.SF.Skill)
        time.sleep(0.2)
        self.assertFalse(marker.exists())                                      # ...but never ran

    def TestNotFromThePhoneOrTelegram(self):
        from System.PhoneTasks import NOT_FOR_PHONE
        self.assertTrue({"skill_forge", "self_repair"} <= NOT_FOR_PHONE)

    def TestBusyIsNotLeftStuck(self):
        # L12
        class Broken:
            reserved = set()

            def Names(self):
                raise RuntimeError("registry gone")
        said = self.SF.Run({"action": "forge", "goal": "flip a coin for me"}, Broken(),
                           show=lambda t, x: None, say=lambda s: None, draft=lambda g, n, t: "no")
        self.assertIn("couldn't read the tool list", said)
        self.assertFalse(self.SF._busy.locked())

    def TestAFailedTestRunAddsNothing(self):
        skill = self.SF.Skill("coin_flip", GOOD_SKILL, "Flips.", {}, "", [], "flip a coin")
        said = self.SF.TestAndInstall(skill, None, test=lambda c, e: (False, "it crashed"))
        self.assertIn("didn't add it", said)
        self.assertFalse((self.tmp / "Plugins" / "coin_flip.py").exists())


# ── H7 / L21: computer control ───────────────────────────────────────────────

class TestComputerControl(unittest.TestCase):
    def setUp(self):
        from Actions import ComputerControl
        self.CC = ComputerControl

    def TestFocusTitleNeverEntersTheScript(self):
        payload = 'x"); Remove-Item C:\\important -Recurse; ("'
        runs = []

        def Run(cmd, **kw):
            runs.append((cmd, kw))
            return SimpleNamespace(stdout="True\n", stderr="", returncode=0)
        with mock.patch.object(self.CC, "_GetOs", lambda: "windows"), \
                mock.patch.object(self.CC.subprocess, "run", Run), mock.patch.object(self.CC.time, "sleep"):
            said = self.CC.FocusWindow(payload)
        cmd, kw = runs[0]
        self.assertNotIn("Remove-Item", " ".join(cmd))
        self.assertEqual(kw["env"]["ORION_FOCUS_TITLE"], payload)
        self.assertIn("Focused window", said)

    def TestFocusSaysSoWhenNoWindowMatched(self):
        with mock.patch.object(self.CC, "_GetOs", lambda: "windows"), \
                mock.patch.object(self.CC.subprocess, "run",
                                  lambda *a, **k: SimpleNamespace(stdout="False\n", stderr="", returncode=0)):
            self.assertIn("Could not find", self.CC.FocusWindow("Notepad"))

    def TestNonAsciiIsNotTypedThroughTypewrite(self):
        # L21
        gui = mock.MagicMock()
        clip = mock.MagicMock()
        clip.paste.return_value = "old"
        with mock.patch.object(self.CC, "pyautogui", gui, create=True), \
                mock.patch.object(self.CC, "pyperclip", clip, create=True), \
                mock.patch.object(self.CC, "RequirePyperclip", lambda: True), \
                mock.patch.object(self.CC, "_GetOs", lambda: "windows"), \
                mock.patch.object(self.CC.time, "sleep"):
            how = self.CC._TypeText("café ñ", 0)
            self.CC._TypeText("plain", 0)
        self.assertEqual(how, " (clipboard)")
        clip.copy.assert_any_call("café ñ")
        self.assertEqual([c.args[0] for c in gui.typewrite.call_args_list], ["plain"])
        clip.copy.assert_called_with("old")                           # the clipboard is put back


# ── H8 / M9 / L7: the phone link ─────────────────────────────────────────────

class TestPhoneConfirm(unittest.TestCase):
    def setUp(self):
        from System import PhoneBridge
        self.PB = PhoneBridge
        self.shown = []
        # Stand-ins first, so they outlast the cleanups: the mirror's own threads never reach a real Link.
        for p in (mock.patch.object(PhoneBridge, "Sealed", lambda *a, **k: {}),
                  mock.patch.object(PhoneBridge, "_Call", side_effect=PhoneBridge.LinkDown("test"))):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(time.sleep, 0.2)                     # the mirror's threads finish first
        Confirm.bind(lambda t, d: self.shown.append(t), lambda: None)
        self.addCleanup(Confirm.bind, None, None)
        self.addCleanup(Confirm.resolve, False)
        self.bridge = PhoneBridge.Bridge()
        self.bridge.connected = True
        self.bridge.tasks = SimpleNamespace(running=1)       # a phone request is running: mirror it
        self.bridge.WrapConfirm(Confirm)

    def TestAStaleYesDoesNotApproveANewerQuestion(self):
        first, second = threading.Event(), threading.Event()
        Confirm.request("a", "Delete the old notes", "", lambda: (first.set(), "a")[1])
        old_cid = self.bridge.confirm_id
        self.assertTrue(old_cid)
        Confirm.request("b", "Shut down the laptop", "", lambda: (second.set(), "b")[1])
        self.assertNotEqual(self.bridge.confirm_id, old_cid)
        self.bridge.OnTask({"kind": "confirm_answer", "id": old_cid, "yes": True})   # the old question's yes
        time.sleep(0.2)
        self.assertFalse(first.is_set() or second.is_set())
        self.assertEqual(Confirm.PendingTitle(), "Shut down the laptop")            # still waiting

    def TestTheYesOnlyAnswersTheQuestionItWasShown(self):
        done = threading.Event()
        Confirm.request("b", "Shut down the laptop", "", lambda: (done.set(), "b")[1])
        cid = self.bridge.confirm_id
        self.bridge.confirm_pid = Confirm.PendingId() - 1                  # shown an older one
        self.bridge.OnTask({"kind": "confirm_answer", "id": cid, "yes": True})
        time.sleep(0.2)
        self.assertFalse(done.is_set())
        self.bridge.confirm_pid = Confirm.PendingId()
        self.bridge.OnTask({"kind": "confirm_answer", "id": cid, "yes": True})
        self.assertTrue(done.wait(2))


class TestPhoneLinkReplies(unittest.TestCase):
    def setUp(self):
        from System import PhoneBridge
        self.PB = PhoneBridge
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for p in (mock.patch.object(PhoneBridge, "STORE", self.tmp / "PhoneLink.json"),
                  mock.patch.object(PhoneBridge, "BACKOFF_S", (0.01,))):
            p.start()
            self.addCleanup(p.stop)
        PhoneBridge.Save({"link_id": "abc", "key": base64.b64encode(b"k" * 32).decode()})

    def _Run(self, garbled):
        bridge = self.PB.Bridge()
        calls = []

        def Call(obj, timeout=8):
            if threading.current_thread().name != "bridge-loop":
                raise self.PB.LinkDown("not this test's")
            calls.append(obj)
            if len(calls) >= garbled:
                bridge._stop.set()
            return {"box": base64.b64encode(b"\x00" * 40).decode()}      # won't decrypt
        with mock.patch.object(self.PB, "_Call", Call):
            t = threading.Thread(target=bridge._Loop, daemon=True, name="bridge-loop")
            t.start()
            t.join(5)
        self.assertFalse(t.is_alive())
        return calls

    def TestOneGarbledReplyKeepsThePairing(self):
        with mock.patch.object(self.PB, "_Call", lambda o, timeout=8: {"box": "bm9wZQ=="}):
            with self.assertRaises(self.PB.Garbled):
                self.PB.Sealed("status")
        self.assertFalse(issubclass(self.PB.Garbled, self.PB.NotPaired))
        self._Run(self.PB.GARBLED_MAX - 1)
        self.assertIsNotNone(self.PB.Load())

    def TestManyInARowDropIt(self):
        self._Run(self.PB.GARBLED_MAX)
        self.assertIsNone(self.PB.Load())

    def TestAHugeFrameIsNotRead(self):
        # L7
        class Sock:
            def __init__(self, data):
                self.data, self.asked = data, 0

            def recv(self, n):
                self.asked += n
                chunk, self.data = self.data[:n], self.data[n:]
                return chunk
        s = Sock(struct.pack(">I", self.PB.MAX_FRAME + 1) + b"x" * 100)
        self.assertIsNone(self.PB._Recv(s))
        self.assertEqual(s.asked, 4)                                   # only the length was read
        body = json.dumps({"ok": True}).encode()
        self.assertEqual(self.PB._Recv(Sock(struct.pack(">I", len(body)) + body)), {"ok": True})


# ── H9 / M7 / L6: Telegram ───────────────────────────────────────────────────

from Tests.TestTelegram import LinkCase, Msg, OWNER  # noqa: E402
from System import TelegramLink as TL  # noqa: E402


class TestTelegramFixes(LinkCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(TL, "SEND_GAP_S", 0)
        p.start()
        self.addCleanup(p.stop)

    def TestPrivateAlertsStayOnTheLaptop(self):
        self.Pair()
        self.settings["alert_calls"] = True
        self.private = True
        n = len(self.api.sent)
        with mock.patch.object(TL, "Api", self.api):
            self.assertTrue(TL.AlertNow("call", "Missed call from Mum", "She wants the bank papers."))
        sent = self.Texts()[n:]
        self.assertEqual(len(sent), 1)
        self.assertIn("private alert on the laptop", sent[0])
        self.assertNotIn("Mum", sent[0])
        self.assertNotIn("bank", sent[0])
        self.settings["details"] = True                       # "Send private answers here" on
        with mock.patch.object(TL, "Api", self.api):
            TL.AlertNow("call", "Missed call from Mum", "She wants the bank papers.")
        self.assertIn("Mum", self.Texts()[-1])

    def TestPatchesDontUndoEachOther(self):
        def Writer(n):
            for i in range(10):
                TL.Patch(**{f"k{n}_{i}": i})
        threads = [threading.Thread(target=Writer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len([k for k in TL.Load() if k.startswith("k")]), 80)

    def TestGuessesAreCountedOneAtATime(self):
        # L6: five wrong guesses then the right code, all in one batch: the code is gone by then.
        from System import TelegramRules as R
        ok, _ = self.link.SaveToken("123456789:" + "A" * 35)
        self.assertTrue(ok)
        code = self.link.code
        wrong = f"{(int(code) + 1) % 10 ** 6:06d}"
        batch = [Msg(wrong, uid=i, frm=7) for i in range(R.MAX_WRONG_CODES)] + [Msg(code, uid=99, frm=7)]
        self.link.HandleAll(batch, now=1_000_000)
        self.assertIsNone(TL.Load().get("owner"))
        self.assertIsNone(self.link.Code())


# ── H13 / M20 / L27: voices ──────────────────────────────────────────────────

class TestVoices(unittest.TestCase):
    def TestElevenlabsMp3Decodes(self):
        import av
        import numpy as np
        from Engines.TtsElevenlabs import DecodeMp3
        buf = io.BytesIO()
        with av.open(buf, "w", format="mp3") as out:
            st = out.add_stream("libmp3lame", rate=24000, layout="mono")
            t = np.arange(12000) / 24000
            frame = av.AudioFrame.from_ndarray((np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16).reshape(1, -1),
                                               format="s16", layout="mono")
            frame.rate = 24000
            for p in st.encode(frame):
                out.mux(p)
            for p in st.encode(None):
                out.mux(p)
        pcm = DecodeMp3(buf.getvalue())
        self.assertGreater(len(pcm), 20000)                            # about half a second of 24 kHz audio
        self.assertEqual(len(pcm) % 2, 0)

    def TestPiperRepairsAPartialVoice(self):
        from Engines import TtsPiper
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        voice = "en_US-ryan-high"
        with mock.patch.object(TtsPiper, "_VOICE_DIR", tmp):
            (tmp / f"{voice}.onnx").write_bytes(b"model")
            self.assertFalse(TtsPiper.Complete(voice))                # no .onnx.json yet
            fetched = []
            with mock.patch.object(TtsPiper, "FetchFile",
                                   lambda url, target: (fetched.append(target.name), target.write_text("{}"))):
                TtsPiper.download(voice)
            self.assertEqual(fetched, [f"{voice}.onnx.json"])          # only the missing one
            self.assertTrue(TtsPiper.Complete(voice))

    def TestPiperDownloadCutOffLeavesNothing(self):
        from Engines import TtsPiper
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        seen = {}

        class Cut(io.RawIOBase):
            def readinto(self, b):
                raise OSError("connection reset")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def Open(url, timeout=None):
            seen["timeout"] = timeout
            return Cut()
        with mock.patch("urllib.request.urlopen", Open):
            with self.assertRaises(OSError):
                TtsPiper.FetchFile("https://example.invalid/v.onnx", tmp / "v.onnx")
        self.assertTrue(seen["timeout"])
        self.assertEqual(list(tmp.iterdir()), [])                      # no .onnx, no .part

    def TestSystemVoicePathsAreQuoted(self):
        from Engines import TtsSystem
        scripts = []

        def Run(cmd, *a, **k):
            scripts.append(cmd[-1])
            return SimpleNamespace(returncode=0, stderr=b"")
        with mock.patch.object(TtsSystem, "Powershell", lambda: "powershell"), \
                mock.patch.object(TtsSystem, "_run", Run):
            TtsSystem.SystemTTS({})._Windows(r"C:\Users\O'Brien\t.txt", r"C:\Users\O'Brien\o.wav")
        s = scripts[0]
        self.assertIn(r"'C:\Users\O''Brien\t.txt'", s)
        self.assertIn(r"'C:\Users\O''Brien\o.wav'", s)
        self.assertEqual(s.count("'") % 2, 0)


# ── H14 / L18: YouTube ───────────────────────────────────────────────────────

class TestYoutube(unittest.TestCase):
    def TestTranscriptWithTheNewApi(self):
        from Actions import YoutubeVideo as Y

        class Snippet:
            def __init__(self, text):
                self.text = text

        class Transcript:
            def fetch(self):
                return [Snippet("hello"), Snippet("world")]

        class Listing:
            def find_manually_created_transcript(self, langs):
                raise LookupError("none")

            def find_generated_transcript(self, langs):
                return Transcript()

        class Api:                                                     # 1.x: no list_transcripts, .list() on an instance
            def list(self, video_id):
                return Listing()
        with mock.patch.object(Y, "YouTubeTranscriptApi", Api), mock.patch.object(Y, "_TRANSCRIPT_OK", True):
            self.assertEqual(Y.GetTranscript("dQw4w9WgXcQ"), "hello world")
        if Y._TRANSCRIPT_OK:                                           # the one installed is the 1.x kind
            self.assertTrue(hasattr(Y.YouTubeTranscriptApi, "list"))

    def TestTheWholeUrlIsOpened(self):
        from Actions import YoutubeVideo as Y
        opened = []
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s"
        with mock.patch.object(Y, "IsMac", lambda: False), mock.patch.object(Y, "IsLinux", lambda: False), \
                mock.patch("os.startfile", opened.append, create=True):
            Y.OpenUrl(url)
        self.assertEqual(opened, [url])


# ── H15 / L19: reminders ─────────────────────────────────────────────────────

class TestReminders(unittest.TestCase):
    def setUp(self):
        from Actions import Reminder
        self.R = Reminder
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(Reminder, "ScriptsDir", lambda: self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def TestTheScriptCompilesAndUsesAToast(self):
        path = self.R.WriteNotifyScript("ORIONReminder_test", "Call mum", "windows")
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        self.assertNotIn("win10toast", text)
        self.assertNotIn("msg.exe", text)
        self.assertIsNone(re.search(r"[\"']msg[\"']", text))
        self.assertIn("ToastNotificationManager", text)
        self.assertIn("MessageBoxW", text)                              # the fallback
        self.assertIn("ORION_REMINDER_MSG", text)

    def TestTwoInTheSameMinuteGetTheirOwnTask(self):
        names = []

        def Schedule(dt, name, script, msg):
            names.append(name)
            return name
        when = time.strftime("%Y-%m-%d", time.localtime(time.time() + 2 * 86400))
        with mock.patch.object(self.R, "ScheduleWindows", Schedule), mock.patch.object(self.R, "_GetOs", lambda: "windows"):
            for msg in ("one", "two"):
                self.assertIn("Reminder set", self.R.reminder({"date": when, "time": "09:30", "message": msg}))
        self.assertEqual(len(set(names)), 2)


# ── H16: game updater ────────────────────────────────────────────────────────

class TestGameUpdater(unittest.TestCase):
    def TestItImportsAsAScriptFromActions(self):
        # Like the daily task: the file run from Actions\ (main logic not run: run_name isn't __main__).
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        r = subprocess.run([sys.executable, "-I", "-c",
                            "import runpy; runpy.run_path('GameUpdater.py', run_name='gu_check'); print('IMPORTED')"],
                           cwd=str(ROOT / "Actions"), env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertIn("IMPORTED", r.stdout)

    def TestShutdownWaitsForTheConfirm(self):
        from Actions import GameUpdater as G
        shown = []
        Confirm.bind(lambda t, d: shown.append(t), lambda: None)
        self.addCleanup(Confirm.bind, None, None)
        self.addCleanup(Confirm.resolve, False)
        with mock.patch.object(G, "WatchAndShutdown", side_effect=AssertionError("armed before the yes")), \
                mock.patch.object(G, "_SystemShutdown", side_effect=AssertionError("shut down")):
            said = G.ArmShutdown(Path("C:/Steam"))
        self.assertIn("CONFIRMATION_PENDING", said)
        self.assertEqual(shown, ["Shut down the PC when the game download finishes"])


# ── H17 / M18 / M19: the local brain's pipeline ──────────────────────────────

class _Cfg:
    system_instruction = "You are ORION."
    tools = [{"function_declarations": [
        {"name": "web_search", "description": "Searches the web.",
         "parameters": {"type": "OBJECT", "properties": {"query": {"type": "STRING"}}}},
        {"name": "open_app", "description": "Opens an application.",
         "parameters": {"type": "OBJECT", "properties": {"app_name": {"type": "STRING"}}}},
    ]}]


class _FR:
    def __init__(self, id, name, result):
        self.id, self.name, self.response = id, name, {"result": result}


class TestPipeline(unittest.TestCase):
    def TestTheToolCapEnds(self):
        from Engines.Base import FunctionCall
        from Engines.EnginePipeline import PipelineEngine

        class Wanderer:                                               # calls a new tool every time, tools or not
            caps = {"tools": True, "tool_format": "openai", "vision": False}

            def __init__(self):
                self.tools = []

            async def Chat(self, messages, tools=None, **kw):
                self.tools.append(tools)
                n = len(self.tools)
                yield {"tool_calls": [FunctionCall(f"id{n}", "web_search", {"query": f"q{n}"})]}

        llm = Wanderer()
        ran = []

        async def Go():
            eng = PipelineEngine(llm, config=_Cfg(), log=lambda m: None)
            done = asyncio.Event()

            async def LikeMain():
                async for r in eng.receive():
                    if r.tool_call:
                        frs = [_FR(fc.id, fc.name, "nothing found") for fc in r.tool_call.function_calls]
                        ran.extend(fc.id for fc in r.tool_call.function_calls)
                        await eng.send_tool_response(function_responses=frs)
                    if r.server_content and r.server_content.turn_complete:
                        done.set()
                        return
            task = asyncio.create_task(LikeMain())
            await eng.send_client_content(turns={"role": "user", "parts": [{"text": "find it"}]})
            try:
                await asyncio.wait_for(done.wait(), 10)
            finally:
                task.cancel()
                await eng.close()
            return done.is_set(), eng
        ended, eng = asyncio.run(Go())
        self.assertTrue(ended, f"the turn never ended after {len(llm.tools)} model calls")
        self.assertLessEqual(len(ran), 5)
        self.assertIsNone(llm.tools[-1])                               # no tools offered after the cap
        self.assertLessEqual(len(llm.tools), 7)

    def TestTalkingDuringAToolCallKeepsTheHistoryValid(self):
        from Engines.Base import FunctionCall
        from Engines.EnginePipeline import PipelineEngine

        class Llm:
            caps = {"tools": True, "tool_format": "openai", "vision": False}

            def __init__(self):
                self.seen = []

            async def Chat(self, messages, tools=None, **kw):
                self.seen.append(json.loads(json.dumps(messages)))
                if len(self.seen) == 1:
                    yield {"tool_calls": [FunctionCall("c1", "open_app", {"app_name": "spotify"})]}
                else:
                    yield {"text": "Okay."}

        llm = Llm()

        async def Go():
            eng = PipelineEngine(llm, config=_Cfg(), log=lambda m: None)
            got = []

            async def Read():
                async for r in eng.receive():
                    got.append(r)
            task = asyncio.create_task(Read())
            await eng.send_client_content(turns={"role": "user", "parts": [{"text": "open spotify"}]})
            await asyncio.sleep(0.1)
            self.assertTrue(any(r.tool_call for r in got))
            await eng.send_client_content(turns={"role": "user", "parts": [{"text": "never mind"}]})
            await asyncio.sleep(0.1)
            calls = len(llm.seen)
            await eng.send_tool_response(function_responses=[_FR("c1", "open_app", "Opened Spotify.")])
            await asyncio.sleep(0.1)
            task.cancel()
            await eng.close()
            return eng, calls
        eng, calls = asyncio.run(Go())
        self.assertEqual(len(llm.seen), 2)
        msgs = llm.seen[1]
        # Every tool call has its answer before the next user message.
        for i, m in enumerate(msgs):
            for tc in m.get("tool_calls") or []:
                nxt = next(j for j in range(i + 1, len(msgs)) if msgs[j]["role"] != "tool") \
                    if any(msgs[j]["role"] != "tool" for j in range(i + 1, len(msgs))) else len(msgs)
                answered = [x for x in msgs[i + 1:nxt] if x.get("tool_call_id") == tc["id"]]
                self.assertEqual(len(answered), 1, msgs)
        self.assertEqual(msgs[-1], {"role": "user", "content": "never mind"})
        self.assertEqual(calls, len(llm.seen))                          # the late result didn't start a reply
        self.assertTrue(eng._history[-1]["content"].startswith("[Late tool result] open_app"))

    def TestABigNewestMessageKeepsTheQuestion(self):
        from Engines.EnginePipeline import PipelineEngine
        eng = PipelineEngine(SimpleNamespace(caps={}), config=_Cfg())
        big = "describe this: " + "x" * 30_000
        eng._history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello."},
                        {"role": "user", "content": big}]
        msgs = eng.Messages()
        self.assertEqual(msgs[-1]["content"], big)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(len(eng._history), 1)          # what is never sent again isn't kept

    def TestANoteAsksForNoReply(self):
        from Engines.EnginePipeline import PipelineEngine
        eng = PipelineEngine(SimpleNamespace(caps={}), config=_Cfg())
        started = []
        eng.QueueGeneration = lambda: started.append(1)

        async def Go():
            await eng.send_client_content(turns={"role": "user", "parts": [{"text": "JARVIS said hi"}]},
                                          turn_complete=False)
            self.assertEqual((started, eng._history), ([], []))
            await eng.send_client_content(turns={"role": "user", "parts": [{"text": "hello"}]})
        asyncio.run(Go())
        self.assertEqual(started, [1])
        self.assertEqual([h["content"] for h in eng._history], ["JARVIS said hi", "hello"])


# ── H18: call secretary ──────────────────────────────────────────────────────

class TestCallSecretaryFailure(unittest.TestCase):
    def setUp(self):
        import Plugins.CallSecretary as CS
        self.CS = CS
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for p in (mock.patch.object(CS, "DATA_DIR", self.tmp), mock.patch.object(CS, "NOTES_FILE", self.tmp / "Notes.json"),
                  mock.patch.object(Privacy, "On", lambda: False), mock.patch.object(CS, "_OnDevice", lambda: False)):
            p.start()
            self.addCleanup(p.stop)
        Confirm.bind(lambda t, d: None, lambda: None)
        self.addCleanup(Confirm.bind, None, None)
        self.addCleanup(Confirm.resolve, False)

    def TestAFailureAfterAcceptHangsUpBeforeTheMicOpens(self):
        from Tests.TestCallSecretary import CFG, _Calls, _Sound
        order = []

        class Calls(_Calls):
            def HangUp(self):
                order.append("hang up")
                return True

        class Sound(_Sound):
            def Record(self, device, max_seconds, stop):
                raise RuntimeError("the sound card went away")

        class Player:
            muted = False

            def SetMicMuted(self, m, note=""):
                order.append(f"mic {'off' if m else 'on'}")

            def WriteLog(self, t):
                pass
        said = []
        sec = self.CS.Secretary(calls=Calls(), sound=Sound([]), pause=lambda s: None,
                                settings=lambda: dict(CFG), summarise=lambda *a: None)
        sec.Take(Player(), said.append)
        self.assertTrue(_WaitFor(lambda: not sec.busy.locked()), "the call never finished")
        self.assertEqual(order, ["mic off", "hang up", "mic on"])
        self.assertIn("failed", said[-1])
        self.assertEqual(Confirm.PendingTitle(), "")                   # the take-over banner is gone


# ── M8: phone memory sync ────────────────────────────────────────────────────

class TestTogetherSync(_TempConfig):
    def setUp(self):
        super().setUp()
        from System import Together as T
        self.T = T
        for p in (mock.patch.object(T, "SYNC_FILE", self.tmp / "PhoneTogether.json"),
                  mock.patch.object(T, "ACTIVITY_FILE", self.tmp / "Activity.json")):
            p.start()
            self.addCleanup(p.stop)

    def TestConcurrentMergesKeepEverything(self):
        def Phone(n):
            for i in range(5):
                self.T.Merge([{"category": "notes", "key": f"p{n}_{i}", "value": f"v{i}",
                               "updated": "2026-11-01T10:00:00"}])
        threads = [threading.Thread(target=Phone, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        notes = Memory.LoadMemory()["notes"]
        self.assertEqual(len([k for k in notes if k.startswith("p")]), 30)
        self.assertEqual(len(self.T.State()["from_phone"]), 30)

    def TestOrionsOwnWriteDuringAMergeIsKept(self):
        # Orion remembers something while a phone merge is between its load and its save.
        real, side = Memory.LoadMemory, []

        def Load():
            m = real()
            if threading.current_thread().name == "merge" and not side:
                t = threading.Thread(target=Memory.remember, args=("orion_fact", "mine"))
                side.append(t)
                t.start()
                t.join(0.5)              # blocks here only if the merge holds the memory lock
            return m
        with mock.patch.object(Memory, "LoadMemory", Load):
            th = threading.Thread(target=self.T.Merge, name="merge",
                                  args=([{"category": "notes", "key": "phone_fact", "value": "theirs",
                                          "updated": "2026-11-01T10:00:00"}],))
            th.start()
            th.join(5)
            side[0].join(5)
        notes = Memory.LoadMemory()["notes"]
        self.assertIn("phone_fact", notes)
        self.assertIn("orion_fact", notes, "the phone merge wrote over Orion's own new memory")


# ── M10 / M11: browser ───────────────────────────────────────────────────────

class TestBrowser(unittest.TestCase):
    def setUp(self):
        from Actions import BrowserControl as B
        from Brain import Outcome
        self.B, self.Outcome = B, Outcome
        p = mock.patch.object(B, "_registry", B._SessionRegistry())
        p.start()
        self.addCleanup(p.stop)

    def TestAnUnverifiedOpenIsRemembered(self):
        with mock.patch.object(self.B, "OpenNative",
                               lambda u, b: self.Outcome.unverified("browser_control", "handed to Windows")):
            self.B.BrowserControl({"action": "go_to", "url": "skyscanner.com"})
        self.assertEqual(self.B._registry.PopNativeUrl(), "https://skyscanner.com")
        with mock.patch.object(self.B, "OpenNative", lambda u, b: self.Outcome.failed("browser_control", "no")):
            self.B.BrowserControl({"action": "go_to", "url": "skyscanner.com"})
        self.assertEqual(self.B._registry.PopNativeUrl(), "")

    def TestOnlyWebLinksReachTheShell(self):
        for bad in ("file:///C:/Windows/System32/calc.exe", "--disable-web-security", "javascript:alert(1)",
                    "ms-settings:privacy", "C:\\Windows\\notepad.exe"):
            with self.assertRaises(ValueError, msg=bad):
                self.B.NormalizeUrl(bad)
        self.assertEqual(self.B.NormalizeUrl("instagram"), "https://instagram.com")
        with mock.patch("os.startfile", side_effect=AssertionError("reached the shell"), create=True), \
                mock.patch.object(self.B.subprocess, "Popen", side_effect=AssertionError("reached a browser")), \
                mock.patch.object(self.B.subprocess, "run", side_effect=AssertionError("reached a browser")):
            said = self.B.OpenNative("file:///C:/Windows/System32/calc.exe", None)
            said2 = self.B.OpenNative("--headless", "chrome")
        self.assertTrue(said.startswith(f"status={self.Outcome.FAILED}"), said)
        self.assertTrue(said2.startswith(f"status={self.Outcome.FAILED}"), said2)


# ── M12 / M13: files and the dev agent ──────────────────────────────────────

class TestFilePaths(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        Undo.clear()
        self.addCleanup(Undo.clear)
        from Actions import FileController
        self.real_sensitive = FileController.IsSensitivePath
        # The temp folder lives in AppData, which the real path rule refuses.
        p = mock.patch.object(FileController, "IsSensitivePath", lambda _p: False)
        p.start()
        self.addCleanup(p.stop)

    def TestSensitiveFoldersAreRefused(self):
        from Actions import FileController as F
        home = Path.home()
        with mock.patch.object(F, "IsSensitivePath", self.real_sensitive):
            for bad in (home / ".ssh" / "id_rsa", home / "AppData" / "Roaming" / "x.txt",
                        home / "Documents" / "WindowsPowerShell" / "profile.ps1"):
                self.assertFalse(F.IsSafePath(bad), bad)
            self.assertTrue(F.IsSafePath(home / "Desktop" / "notes.txt"))

    def TestRunnableFilesWaitForConfirm(self):
        from Actions import FileController as F
        asked = []
        with mock.patch.object(Confirm, "request", lambda key, title, detail, run: asked.append(key) or "PENDING"), \
                mock.patch.object(Confirm, "PendingTitle", lambda: ""):
            said = F.file_controller({"action": "write", "path": str(self.tmp), "name": "a.bat",
                                      "content": "shutdown /s"})
        self.assertEqual(said, "PENDING")
        self.assertFalse((self.tmp / "a.bat").exists())
        self.assertIn("Written", F.file_controller({"action": "write", "path": str(self.tmp),
                                                     "name": "a.txt", "content": "hi"}))

    def TestRenameStaysInItsFolder(self):
        from Actions import FileController as F
        (self.tmp / "sub").mkdir()
        f = self.tmp / "sub" / "a.txt"
        f.write_text("x", encoding="utf-8")
        for bad in ("..\\b.txt", "../b.txt", "c/b.txt", "C:\\b.txt", "..", "C:b.txt"):
            self.assertIn("not a plain file name", F.RenameFile(str(f), new_name=bad), bad)
        self.assertTrue(f.exists())
        self.assertIn("Renamed", F.RenameFile(str(f), new_name="b.txt"))
        self.assertTrue((self.tmp / "sub" / "b.txt").exists())

    def TestDevAgentPathsStayInTheProject(self):
        from Actions import DevAgent as D
        for bad in ("../evil.py", "..\\..\\evil.py", "C:/Windows/evil.py", "sub/../../evil.py"):
            with self.assertRaises(ValueError, msg=bad):
                D.InProject(self.tmp, bad)
        self.assertEqual(D.InProject(self.tmp, "pkg/main.py"), (self.tmp / "pkg" / "main.py").resolve())

    def TestDevAgentNeverInstallsIntoOrion(self):
        from Actions import DevAgent as D
        runs = []
        with mock.patch.object(D, "ProjectPython", lambda d, create=False: None), \
                mock.patch.object(D.subprocess, "run", lambda cmd, **k: runs.append(cmd)):
            said = D.InstallDependencies(["requests"], self.tmp)
        self.assertIn("did not", said)
        self.assertEqual(runs, [])
        py = self.tmp / ".venv" / "Scripts" / "python.exe"
        with mock.patch.object(D, "ProjectPython", lambda d, create=False: py), \
                mock.patch.object(D.subprocess, "run",
                                  lambda cmd, **k: runs.append(cmd) or SimpleNamespace(returncode=1, stdout="", stderr="")):
            D.InstallDependencies(["requests"], self.tmp)
        self.assertTrue(runs)
        self.assertTrue(all(cmd[0] == str(py) for cmd in runs))
        self.assertNotIn(sys.executable, [cmd[0] for cmd in runs])


# ── M17 / L23 / L25: Gemini and the local model ─────────────────────────────

class TestGeminiAndLocal(unittest.TestCase):
    def TestAOneShotLetsTheModelGo(self):
        from System import LocalBrain
        released = threading.Event()
        with mock.patch.object(LocalBrain, "ONE_SHOT_IDLE_S", 0.05), \
                mock.patch.object(LocalBrain, "Release", released.set), \
                mock.patch.object(LocalBrain, "ACTIVE", False):
            LocalBrain.OneShotStarted()
            LocalBrain.OneShotDone()
            self.assertTrue(released.wait(2))
        released.clear()
        with mock.patch.object(LocalBrain, "ONE_SHOT_IDLE_S", 0.05), \
                mock.patch.object(LocalBrain, "Release", released.set), \
                mock.patch.object(LocalBrain, "ACTIVE", True):        # a local chat: stays loaded
            LocalBrain.OneShotStarted()
            LocalBrain.OneShotDone()
            self.assertFalse(released.wait(0.3))

    def TestTheLocalRungIsReleasedAfterAOneShot(self):
        from Brain import Gemini
        from System import LocalBrain
        did = []
        with mock.patch.object(LocalBrain, "ACTIVE", False), \
                mock.patch.object(LocalBrain, "OneShotStarted", lambda: did.append("start")), \
                mock.patch.object(LocalBrain, "OneShotDone", lambda: did.append("done")), \
                mock.patch.object(Gemini, "_LocalCall", side_effect=RuntimeError("model down")):
            with self.assertRaises(RuntimeError):
                Gemini.ProviderCall(Gemini.LOCAL_RUNG, "hi", None, 1000)
        self.assertEqual(did, ["start", "done"])

    def TestANewKeyIsUsedWithoutARestart(self):
        from Brain import Gemini
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        f = tmp / "ApiKeys.json"
        saved = (Gemini._cached_key, Gemini._cached_key_mtime)

        def Back():
            Gemini._cached_key, Gemini._cached_key_mtime = saved
        self.addCleanup(Back)
        with mock.patch.object(Gemini, "_KEY_FILE", f):
            f.write_text(json.dumps({"gemini_api_key": "first-key"}), encoding="utf-8")
            os.utime(f, (1_000_000, 1_000_000))
            self.assertEqual(Gemini.api_key(), "first-key")
            f.write_text(json.dumps({"gemini_api_key": "second-key"}), encoding="utf-8")
            os.utime(f, (2_000_000, 2_000_000))
            self.assertEqual(Gemini.api_key(), "second-key")
            f.write_text("{half", encoding="utf-8")
            os.utime(f, (3_000_000, 3_000_000))
            self.assertEqual(Gemini.api_key(), "second-key")            # unreadable: keeps the one it had

    def TestLiveModelFollowsMain(self):
        from Brain import Gemini
        fake_main = SimpleNamespace(LIVE_MODEL="models/test-live-model")
        with mock.patch.dict(sys.modules, {"Main": fake_main}):
            if getattr(sys.modules.get("__main__"), "LIVE_MODEL", None):
                self.skipTest("__main__ has its own LIVE_MODEL")
            self.assertEqual(Gemini._LiveModel(), "models/test-live-model")


# ── L8 / L9: self-repair ─────────────────────────────────────────────────────

class TestSelfRepairFixes(unittest.TestCase):
    def TestAnImportTimeoutIsNotFine(self):
        from System import SelfRepair
        with mock.patch.object(SelfRepair.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("python", 60)):
            self.assertIn("longer than", SelfRepair.ImportProblem("Actions/Reminder.py"))

    def TestRollbackAsksFirst(self):
        from System import SelfRepair
        asked = []
        with mock.patch.object(SelfRepair, "_LoadHistory", lambda: [{"status": "applied"}]), \
                mock.patch.object(SelfRepair, "RollBack", side_effect=AssertionError("rolled back unasked")), \
                mock.patch.object(Confirm, "request", lambda key, title, detail, run: asked.append(title) or "asked"):
            self.assertEqual(SelfRepair.Run({"action": "rollback"}, show=lambda t, x: None, say=lambda s: None),
                             "asked")
        self.assertEqual(asked, ["Take back the last repair?"])

    def TestGuardFilesAreNeverOffered(self):
        from System import SelfRepair
        for rel in ("Brain/Privacy.py", "System/TelegramLink.py", "System/PhoneBridge.py", "Brain/Settings.py"):
            tb = (f'Traceback (most recent call last):\n  File "{ROOT / rel}", line 5, in X\n    y()\n'
                  f"KeyError: 'x'")
            self.assertIsNone(SelfRepair.Locate(tb), rel)
        tb = f'Traceback (most recent call last):\n  File "{ROOT / "Actions" / "Reminder.py"}", line 5, in X\nKeyError: 1'
        self.assertEqual(SelfRepair.Locate(tb).rel, "Actions/Reminder.py")


# ── L10 / L11 / L13 / L14 / L15 / L16 / L17 / L20 / L22: the small ones ─────────────

class TestSmallFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        Undo.clear()
        self.addCleanup(Undo.clear)

    def TestGeminiImagesStaysInImages(self):
        from Plugins import GeminiImages as G
        (self.tmp / "image_jobs.json").write_text(json.dumps([{"id": "A1", "name": "..\\..\\evil"},
                                                               {"id": "A2", "name": ".."}]), encoding="utf-8")
        for bad in ("../outside", "..\\outside", "C:\\Windows"):
            with self.assertRaises(ValueError, msg=bad):
                G._load_jobs(self.tmp, "", True, bad)
        jobs, out = G._load_jobs(self.tmp, "", True, "batch 1")
        self.assertEqual([j["name"] for j in jobs], ["evil"])
        self.assertEqual(out, (self.tmp / "images" / "batch 1").resolve())

    def TestInstallsHaveADeadline(self):
        from System import Installer
        seen, logs = {}, []

        def Run(cmd, **k):
            seen.update(k)
            raise subprocess.TimeoutExpired(cmd, k.get("timeout"))
        with mock.patch.object(Installer.subprocess, "run", Run):
            self.assertFalse(Installer.Pip("numpy", logs.append))
        self.assertTrue(seen.get("timeout"))
        self.assertIn("stopped it", logs[-1])
        self.assertIn('HERE / "requirements.txt"', (ROOT / "Setup.py").read_text(encoding="utf-8"))

    def TestFfmpegFailureIsReported(self):
        from Actions import FileProcessor as FP
        video = self.tmp / "clip.mp4"
        video.write_bytes(b"\x00" * 10)

        def Run(cmd, **k):
            if cmd[:2] == ["ffmpeg", "-version"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="header\nInvalid data found")
        with mock.patch.object(FP.subprocess, "run", Run):
            said = FP.ProcessVideo(video, "extract_audio", {})
        self.assertIn("failed", said)
        self.assertIn("Invalid data", said)
        self.assertNotIn("Saved", said)

    def TestFileProcessorRunsOurPython(self):
        from Actions import FileProcessor as FP
        script = self.tmp / "s.py"
        script.write_text("print(1)", encoding="utf-8")
        runs = []
        # Running waits for the on-screen confirm; once confirmed it uses our Python.
        with mock.patch.object(FP.subprocess, "run",
                               lambda cmd, **k: runs.append(cmd) or SimpleNamespace(stdout="1", stderr="")), \
                mock.patch.object(Confirm, "request", lambda key, title, detail, run: run()), \
                mock.patch.object(Confirm, "PendingTitle", lambda: ""):
            FP.ProcessCode(script, "run", {})
        self.assertEqual(runs[0][0], sys.executable)

    def TestSendMessagePlatformsMatchWholeWords(self):
        from Actions import SendMessage as SM
        self.assertIs(SM.ResolvePlatform("Signal"), SM.SendSignal)
        self.assertIs(SM.ResolvePlatform("ig"), SM.SendInstagram)
        self.assertIs(SM.ResolvePlatform("WhatsApp Desktop"), SM.SendWhatsapp)

    def TestFlightUrlHasNoFixedRoute(self):
        from Actions import FlightFinder as FF
        url = FF.BuildGoogleFlightsUrl("New York", "Tokyo & Osaka", "2026-12-01")
        self.assertNotIn("tfs=", url)
        self.assertIn("Tokyo+%26+Osaka", url)

    def TestWifiSaysItNeedsAdmin(self):
        from Actions import ComputerSettings as CS
        with mock.patch.object(CS, "_OS", "Windows"), \
                mock.patch.object(CS.subprocess, "run",
                                  lambda *a, **k: SimpleNamespace(returncode=1, stdout="",
                                                                  stderr="Disable-NetAdapter : Access is denied.")):
            with self.assertRaises(RuntimeError) as e:
                CS.ToggleWifi()
        self.assertIn("administrator", str(e.exception))

    def TestWordsMatchWholeWords(self):
        from Actions import ComputerSettings as CS
        self.assertNotEqual(CS.DetectAction("show the clock")["action"], "lock_screen")
        self.assertNotEqual(CS.DetectAction("close 2 processes")["action"], "volume_set")
        self.assertEqual(CS.DetectAction("set volume to 30"), {"action": "volume_set", "value": 30})
        self.assertEqual(CS.DetectAction("lock the screen")["action"], "lock_screen")

    def _Desktop(self):
        from Actions import Desktop as D
        for name in ("a.txt", "b.jpg", "c.pdf"):
            (self.tmp / name).write_text(name, encoding="utf-8")
        return D

    def TestOrganizeCanBeUndone(self):
        D = self._Desktop()
        with mock.patch.object(D, "GetDesktop", lambda: self.tmp):
            D.OrganizeDesktop()
            self.assertFalse((self.tmp / "a.txt").exists())
            Undo.UndoLast()
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["a.txt", "b.jpg", "c.pdf"])

    def TestCleanCanBeUndone(self):
        D = self._Desktop()
        with mock.patch.object(D, "GetDesktop", lambda: self.tmp):
            D.CleanDesktop()
            self.assertFalse((self.tmp / "b.jpg").exists())
            Undo.UndoLast()
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["a.txt", "b.jpg", "c.pdf"])

    def TestWallpaperDownloadHasADeadline(self):
        from Actions import Desktop as D
        seen = {}

        def Open(url, timeout=None):
            seen["timeout"] = timeout
            raise OSError("offline")
        with mock.patch("urllib.request.urlopen", Open):
            said = D.SetWallpaperFromUrl("https://example.invalid/w.jpg")
            local = D.SetWallpaperFromUrl("file:///C:/Windows/w.jpg")
        self.assertEqual(seen["timeout"], 30)
        self.assertIn("could not be downloaded", said)
        self.assertIn("only http and https", local)

    def TestTopicSlugs(self):
        from Actions.BackgroundMonitor import Slug
        self.assertTrue(Slug("東京"))
        self.assertNotEqual(Slug("東京"), Slug("大阪"))
        self.assertTrue(Slug("!!!").startswith("t_"))
        self.assertNotEqual(Slug("!!!"), Slug("???"))
        self.assertEqual(Slug("Bitcoin price"), "bitcoin_price")

    def TestWeatherDeclaresTime(self):
        from Actions import WeatherReport
        self.assertIn("time", WeatherReport.TOOL["parameters"]["properties"])


if __name__ == "__main__":
    unittest.main()
