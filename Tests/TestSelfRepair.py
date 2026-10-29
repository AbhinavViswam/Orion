"""Self-repair and Skill Forge: nothing changes without a yes, every change is
checked first and can be taken back, and unsafe code never gets in.

Everything runs in temp folders: the real Ultron files, Plugins/ and Config/
are never touched, and no model is called (a stand-in answers instead)."""
import importlib.util
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import traceback
import unittest
from pathlib import Path
from unittest import mock

import Tests.Helpers  # noqa: F401  (paths)
from Brain import Undo
from System import SelfRepair, SkillForge

BROKEN = textwrap.dedent('''\
    def Total(order):
        return order["count"] + 1
''')
FIND = '    return order["count"] + 1\n'
FIXED = '    return order.get("count", 0) + 1\n'


def _Crash(path: Path) -> str:
    """Run the broken file for real and return its traceback."""
    spec = importlib.util.spec_from_file_location("broken_for_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        mod.Total({})
    except KeyError:
        return traceback.format_exc()
    raise AssertionError("it should have crashed")


class _InTemp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "app"
        (self.root / "Actions").mkdir(parents=True)
        self.file = self.root / "Actions" / "Orders.py"
        self.file.write_text(BROKEN, encoding="utf-8")
        for name, value in (("ROOT", self.root), ("REPAIR_DIR", self.root / "Config" / "Repairs"),
                            ("HISTORY_FILE", self.root / "Config" / "Repairs" / "History.json")):
            p = mock.patch.object(SelfRepair, name, value)
            p.start()
            self.addCleanup(p.stop)
        Undo.clear()
        self.addCleanup(Undo.clear)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tb = _Crash(self.file)

    def Ask(self, find=FIND, replace=FIXED, why="A missing count crashed it; it now counts as 0."):
        return lambda prompt: {"explanation": why, "find": find, "replace": replace}


class TestFindingTheBug(_InTemp):
    def TestItFindsOurFileAndLine(self):
        place = SelfRepair.Locate(self.tb)
        self.assertEqual((place.rel, place.line, place.func), ("Actions/Orders.py", 2, "Total"))
        self.assertTrue(place.error.startswith("KeyError"))

    def TestLibrariesAndProtectedFilesAreNeverOffered(self):
        lib = self.root / ".venv" / "Lib" / "site-packages" / "x.py"
        lib.parent.mkdir(parents=True)
        lib.write_text("x = 1\n")
        (self.root / "System").mkdir()
        guard = self.root / "System" / "SelfRepair.py"
        guard.write_text("x = 1\n")
        for path in (lib, guard):
            tb = f'Traceback (most recent call last):\n  File "{path}", line 1, in <module>\nValueError: no'
            self.assertIsNone(SelfRepair.Locate(tb), path)

    def TestACrashOnlyProducesAnOffer(self):
        hint = SelfRepair.NoteFailure("orders", self.tb)
        self.assertIn("Actions/Orders.py", hint)
        self.assertIn("do not repair it unless the user agrees", hint)
        self.assertEqual(self.file.read_text(encoding="utf-8"), BROKEN)       # nothing touched
        self.assertEqual(SelfRepair.NoteFailure("web", "ValueError: outside"), "")


class TestDraftingAndApplying(_InTemp):
    def TestFixAppliesAndUndoTakesItBack(self):
        fix = SelfRepair.Draft(self.tb, ask=self.Ask())
        self.assertIsInstance(fix, SelfRepair.Fix)
        self.assertIn('+    return order.get("count", 0) + 1', fix.Diff())
        self.assertEqual(self.file.read_text(encoding="utf-8"), BROKEN)       # drafting writes nothing
        said = SelfRepair.Apply(fix, import_check=lambda rel: None)
        self.assertTrue(said.startswith("Repaired Actions/Orders.py"), said)
        self.assertIn('order.get("count", 0)', self.file.read_text(encoding="utf-8"))
        self.assertEqual(SelfRepair.History()[0]["status"], "applied")
        Undo.UndoLast()
        self.assertEqual(self.file.read_text(encoding="utf-8"), BROKEN)
        self.assertEqual(SelfRepair.History()[0]["status"], "rolled back")

    def TestAFixThatBreaksTheImportIsPutBack(self):
        fix = SelfRepair.Draft(self.tb, ask=self.Ask())
        said = SelfRepair.Apply(fix, import_check=lambda rel: "NameError: x")
        self.assertIn("put the original back", said)
        self.assertEqual(self.file.read_text(encoding="utf-8"), BROKEN)
        self.assertEqual(SelfRepair.History(), [])

    def TestTheRealImportCheck(self):
        self.assertIsNone(SelfRepair.ImportProblem("Actions/Orders.py"))
        self.file.write_text("raise RuntimeError('broken on import')\n", encoding="utf-8")
        self.assertIn("broken on import", SelfRepair.ImportProblem("Actions/Orders.py"))

    def TestAFileChangedSinceTheDraftIsLeftAlone(self):
        fix = SelfRepair.Draft(self.tb, ask=self.Ask())
        self.file.write_text(BROKEN + "# edited by hand\n", encoding="utf-8")
        self.assertIn("changed since", SelfRepair.Apply(fix, import_check=lambda rel: None))

    def TestBadFixesAreRefusedBeforeWriting(self):
        cases = {
            "didn't match": self.Ask(find="    return nothing_like_this\n"),
            "would not parse": self.Ask(replace="        return order.get(\n"),
            "too big": self.Ask(replace="".join(f"        x{i} = {i}\n" for i in range(60))),
            "safe small fix": self.Ask(find="", why="it needs a redesign"),
        }
        for want, ask in cases.items():
            said = SelfRepair.Draft(self.tb, ask=ask)
            self.assertIsInstance(said, str, want)
            self.assertIn(want, said)
        self.assertIn("couldn't reach", SelfRepair.Draft(self.tb, ask=lambda p: None))
        self.assertEqual(self.file.read_text(encoding="utf-8"), BROKEN)

    def TestTrailingSpacesDontStopAMatch(self):
        self.file.write_text(BROKEN.replace("+ 1\n", "+ 1   \n"), encoding="utf-8")
        tb = _Crash(self.file)
        self.assertIsInstance(SelfRepair.Draft(tb, ask=self.Ask()), SelfRepair.Fix)


class TestTheTool(_InTemp):
    def TestFixWaitsForTheBannerYes(self):
        from Brain import Confirm
        shown, said, banners = [], [], []
        Confirm.bind(lambda t, d: banners.append(t), lambda: None)
        self.addCleanup(Confirm.bind, None, None)
        SelfRepair.NoteFailure("orders", self.tb)
        draft = lambda tb, notes: SelfRepair.Draft(tb, notes, ask=self.Ask())
        with mock.patch.object(SelfRepair, "ImportProblem", lambda rel: None):
            reply = SelfRepair.Run({"action": "fix"}, show=lambda t, x: shown.append(t),
                                   say=said.append, draft=draft)
            self.assertIn("Working out a fix", reply)
            self._WaitFor(lambda: banners)
            self.assertEqual(banners, ["Apply a fix to Orders.py?"])
            self.assertEqual(shown, ["FIX — Actions/Orders.py"])
            self.assertEqual(self.file.read_text(encoding="utf-8"), BROKEN)   # not before the yes
            Confirm.resolve(True)
            self._WaitFor(lambda: len(said) >= 2)
        self.assertIn('order.get("count", 0)', self.file.read_text(encoding="utf-8"))
        self.assertIn("Repaired", said[-1])

    def TestNoRecentErrorMeansNothingToDo(self):
        with mock.patch.object(SelfRepair, "_last", {}):
            self.assertIn("no recent error", SelfRepair.Run({"action": "fix"}, print, print))
        self.assertEqual(SelfRepair.Run({"action": "history"}, print, print), "No repairs yet.")

    @staticmethod
    def _WaitFor(cond, seconds=5.0):
        end = time.monotonic() + seconds
        while not cond() and time.monotonic() < end:
            time.sleep(0.02)


GOOD = textwrap.dedent('''\
    import platform
    PLUGIN = {"name": "coin_flip", "description": "Flips a coin when the user asks to flip a coin.",
              "parameters": {"type": "OBJECT", "properties": {"times": {"type": "INTEGER"}}}}

    def run(parameters, player=None, session_memory=None):
        times = int(parameters.get("times", 1) or 1)
        picks = ["heads"] * times
        picks.remove("heads") if times > 1 else None
        return f"It landed on {'heads'.replace('h', 'H')} on {platform.system()}."
''')


class TestTheSafetyScan(unittest.TestCase):
    def TestAGoodPluginPasses(self):
        scan = SkillForge.ScanCode(GOOD, "coin_flip")
        self.assertEqual(scan.problems, [])

    def TestDangerousCodeIsStopped(self):
        bad = {
            "import subprocess\n": "subprocess",
            "import os\nos.remove('x')\n": "os.remove",
            "from pathlib import Path\nPath('x').unlink()\n": "unlink",
            "import shutil\nshutil.rmtree('x')\n": "rmtree",
            "eval('1')\n": "eval",
            "open('Config/ApiKeys.json').read()\n": "apikeys.json",
            "import not_a_real_package_xyz\n": "isn't installed",
            "from Brain import Memory\n": "only Gemini",
            "import Interface.Window\n": "only Brain.Gemini",
            "().__class__.__subclasses__()\n": "__subclasses__",
        }
        for extra, want in bad.items():
            problems = " | ".join(SkillForge.ScanCode(GOOD + extra, "coin_flip").problems)
            self.assertIn(want, problems, extra)
        self.assertIn("PLUGIN['name'] must be 'other'", SkillForge.ScanCode(GOOD, "other").problems)

    def TestItSaysWhatTheSkillUses(self):
        code = GOOD + "import requests\nfrom Brain import Gemini\nopen('out.txt', 'w')\n"
        self.assertEqual(SkillForge.ScanCode(code, "coin_flip").uses,
                         ["the internet", "writes files", "the AI model"])


class TestTheTestRun(unittest.TestCase):
    def TestItRunsInItsOwnProcess(self):
        ok, answer = SkillForge.TestRun(GOOD, {"times": 2})
        self.assertTrue(ok, answer)
        self.assertTrue(answer.startswith("It landed on Heads"))

    def TestAPoliteFailureIsStillAFailure(self):
        # Found with real Gemini: a bitcoin skill "passed" while saying it couldn't get the price.
        polite = GOOD.replace('return f"It landed', 'return "I was unable to retrieve it." or f"It landed')
        ok, why = SkillForge.TestRun(polite, {})
        self.assertFalse(ok)
        self.assertIn("didn't work", why)
        self.assertFalse(SkillForge.LooksFailed("100 degrees Celsius is 212 degrees Fahrenheit."))

    def TestFailuresAreCaught(self):
        crash = GOOD.replace("times = int(", "times = 1 / 0 + int(")
        self.assertIn("ZeroDivisionError", SkillForge.TestRun(crash, {})[1])
        not_text = GOOD.replace('return f"It landed', 'return 5 or f"It landed')
        self.assertIn("must return a sentence", SkillForge.TestRun(not_text, {})[1])
        slow = GOOD.replace("times = int(", "import time; time.sleep(30); times = int(")
        self.assertIn("longer than", SkillForge.TestRun(slow, {}, timeout=3)[1])


class TestForging(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.plugins = self.tmp / "Plugins"
        self.plugins.mkdir()
        for name, value in (("PLUGINS_DIR", self.plugins), ("REMOVED_DIR", self.tmp / "Removed")):
            p = mock.patch.object(SkillForge, name, value)
            p.start()
            self.addCleanup(p.stop)
        Undo.clear()
        self.addCleanup(Undo.clear)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(sys.modules.pop, "Plugins.coin_flip", None)

    def TestAFailedAttemptIsSentBackWithTheReason(self):
        prompts = []
        replies = iter([{"name": "coin_flip", "code": "import subprocess\n" + GOOD, "example": {}},
                        {"name": "coin_flip", "code": GOOD, "example": {"times": 1}, "summary": "Flips a coin."}])

        def ask(prompt):
            prompts.append(prompt)
            return next(replies)
        skill = SkillForge.Draft("flip a coin for me", ask=ask)
        self.assertIsInstance(skill, SkillForge.Skill)
        self.assertIn("it imports subprocess", prompts[1])
        self.assertEqual((skill.name, skill.summary), ("coin_flip", "Flips a coin."))
        always_bad = lambda p: {"name": "coin_flip", "code": "eval('1')\n" + GOOD, "example": {}}
        self.assertIn("calls eval()", SkillForge.Draft("flip a coin for me", ask=always_bad))
        self.assertIn("already a tool", SkillForge.Draft("flip a coin", "web_search", {"web_search"}, ask=ask))

    def TestAGoalItCantDoSafelyIsRefused(self):
        said = SkillForge.Draft("delete everything in my temp folder",
                                ask=lambda p: {"refused": "Skills may not delete files."})
        self.assertEqual(said, "I can't make that skill safely: Skills may not delete files.")

    def TestInstallLoadsItAndUndoTakesItOut(self):
        from System.PluginLoader import DiscoverPlugins
        registry = DiscoverPlugins(self.plugins, {"web_search"}, logger=lambda m: None)
        changed = []
        skill = SkillForge.Skill("coin_flip", GOOD, "Flips a coin.", {}, "It landed on Heads.", [], "flip a coin")
        said = SkillForge.Install(skill, registry, after=lambda: changed.append(1))
        self.assertEqual(said, "Added the skill coin_flip: Flips a coin.")
        self.assertTrue(registry.has("coin_flip"))
        self.assertIn("coin_flip", {d["name"] for d in registry.GetToolDeclarations()})
        self.assertEqual([s["name"] for s in SkillForge.Forged()], ["coin_flip"])
        Undo.UndoLast()
        self.assertFalse(registry.has("coin_flip"))
        self.assertFalse((self.plugins / "coin_flip.py").exists())
        self.assertEqual(len(list((self.tmp / "Removed").glob("coin_flip.*.py"))), 1)   # moved, not deleted
        self.assertEqual(changed, [1, 1])

    def TestHandWrittenPluginsAreNeverRemoved(self):
        (self.plugins / "mine.py").write_text(GOOD.replace("coin_flip", "mine"), encoding="utf-8")
        self.assertIn("no forged skill", SkillForge.Remove("mine", None))
        self.assertTrue((self.plugins / "mine.py").exists())

    def TestTheToolWaitsForTheBannerYes(self):
        from Brain import Confirm
        from System.PluginLoader import DiscoverPlugins
        registry = DiscoverPlugins(self.plugins, set(), logger=lambda m: None)
        said, banners = [], []
        Confirm.bind(lambda t, d: banners.append(t), lambda: None)
        self.addCleanup(Confirm.bind, None, None)
        skill = SkillForge.Skill("coin_flip", GOOD, "Flips a coin.", {}, "Heads.", [], "flip a coin")
        reply = SkillForge.Run({"action": "forge", "goal": "flip a coin for me"}, registry,
                               show=lambda t, x: None, say=said.append, draft=lambda g, n, t: skill)
        self.assertIn("Building the skill", reply)
        TestTheTool._WaitFor(lambda: banners)
        self.assertEqual(banners, ["Add the skill 'coin_flip'?"])
        self.assertFalse((self.plugins / "coin_flip.py").exists())            # not before the yes
        Confirm.resolve(True)
        TestTheTool._WaitFor(lambda: registry.has("coin_flip"))
        self.assertTrue((self.plugins / "coin_flip.py").read_text(encoding="utf-8").startswith(SkillForge.MARK))


class TestWiring(unittest.TestCase):
    def TestBothToolsAreDeclaredAndAnswered(self):
        import asyncio
        import Main
        names = {t["name"] for t in Main.TOOL_DECLARATIONS}
        self.assertTrue({"self_repair", "skill_forge"} <= names)
        live = Main.UltronLive.__new__(Main.UltronLive)
        live.ui = mock.MagicMock()
        live.ui.muted = False
        live._action_registry = mock.MagicMock()
        live._action_registry.has.return_value = False
        live._action_registry.scheduling.return_value = None
        live._plugin_registry = mock.MagicMock()
        live._plugin_registry.scheduling.return_value = None
        with mock.patch.object(SelfRepair, "History", lambda: []):
            fc = Main.types.FunctionCall(id="1", name="self_repair", args={"action": "history"})
            self.assertEqual(asyncio.run(live._ExecuteTool(fc)).response["result"], "No repairs yet.")
        with mock.patch.object(SkillForge, "Forged", lambda: []):
            fc = Main.types.FunctionCall(id="2", name="skill_forge", args={"action": "list"})
            self.assertIn("haven't forged", asyncio.run(live._ExecuteTool(fc)).response["result"])

    def TestACrashingActionCarriesTheOffer(self):
        from System.ActionLoader import ActionRegistry, ActionRecord
        here = Path(SelfRepair.__file__).parent.parent / "Actions" / "OpenApp.py"
        tb = f'Traceback (most recent call last):\n  File "{here}", line 5, in Open\nKeyError: \'x\''

        def Boom(parameters):
            raise KeyError("x")
        reg = ActionRegistry.__new__(ActionRegistry)
        reg._actions = {"boom": ActionRecord(name="boom", handler=Boom, valid=True)}
        reg._logger = lambda m: None
        with mock.patch("traceback.format_exc", lambda: tb), mock.patch("traceback.print_exc"):
            out = reg.run("boom", {})
        self.assertIn("Actions/OpenApp.py", out)


if __name__ == "__main__":
    unittest.main()
