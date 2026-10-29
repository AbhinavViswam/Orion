"""Learning from corrections: rules are kept, replaced, forgotten, undone, put
into the prompt, and caught from speech without the model."""
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import Tests.Helpers  # noqa: F401  (paths)


class _TempMemory(unittest.TestCase):
    def setUp(self):
        import Brain.Memory as M
        from Brain import Undo
        self.M, self.Undo = M, Undo
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = M.MEMORY_PATH
        M.MEMORY_PATH = self.tmp / "LongTerm.json"
        Undo.clear()
        self.out = mock.patch("builtins.print", lambda *a, **k: None)   # memory prints emoji
        self.out.start()

    def tearDown(self):
        self.out.stop()
        self.M.MEMORY_PATH = self._saved
        self.Undo.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def Rules(self):
        from Brain import Rules
        return [r["value"] for r in Rules.All()]


class TestLearning(_TempMemory):
    def TestKeepAndRepeat(self):
        from Brain import Rules
        self.assertTrue(Rules.Learn("never read my emails aloud").startswith("Kept"))
        self.assertEqual(self.Rules(), ["Never read my emails aloud."])
        self.assertTrue(Rules.Learn("Never read my emails aloud.").startswith("Already"))
        self.assertEqual(len(self.Rules()), 1)

    def TestReplaceAndForget(self):
        from Brain import Rules
        Rules.Learn("Call me boss.")
        Rules.Learn("Call me chief.", replaces="call me boss")
        self.assertEqual(self.Rules(), ["Call me chief."])
        self.assertTrue(Rules.Learn("", replaces="calling me chief").startswith("Removed"))
        self.assertEqual(self.Rules(), [])
        self.assertTrue(Rules.Learn("", replaces="emails").startswith("No rule"))

    def TestUndoTakesItBack(self):
        from Brain import Rules
        Rules.Learn("Call me boss.")
        Rules.Learn("Call me chief.", replaces="call me boss")
        self.Undo.UndoLast()
        self.assertEqual(self.Rules(), ["Call me boss."])

    def TestOldestGoesWhenFull(self):
        from Brain import Rules
        for i in range(Rules.MAX_STORED + 1):
            r = Rules.Learn(f"Rule number {i} about topic{i}")
        self.assertIn("oldest rule was forgotten", r)
        self.assertEqual(len(self.Rules()), Rules.MAX_STORED)

    def TestTheyGoIntoThePromptNotTheFacts(self):
        from Brain import Rules
        Rules.Learn("Never read my emails aloud.")
        memory = self.M.LoadMemory()
        self.assertIn("[YOUR RULES", Rules.PromptBlock(memory))
        self.assertIn("Never read my emails aloud.", Rules.PromptBlock(memory))
        self.assertNotIn("emails", self.M.FormatMemoryForPrompt(memory))
        self.assertEqual(Rules.PromptBlock({"rules": {}}), "")


class TestSpeech(unittest.TestCase):
    def TestSpokenRules(self):
        from Brain import Rules
        self.assertEqual(Rules.FromSpeech("From now on, call me boss"), "Call me boss.")
        self.assertEqual(Rules.FromSpeech("Ultron, always remember to ask before closing apps"),
                         "Ask before closing apps.")
        for not_a_rule in ("from now on remind me at 7", "from now on, can you be quieter?",
                           "next time I'm at the shop remind me", "call me boss", "from now on"):
            self.assertIsNone(Rules.FromSpeech(not_a_rule), not_a_rule)

    def TestDroppingOne(self):
        from Brain import Rules
        self.assertEqual(Rules.DroppedInSpeech("stop calling me boss"), "call me boss")
        self.assertEqual(Rules.DroppedInSpeech("forget the rule about emails"), "emails")
        self.assertIsNone(Rules.DroppedInSpeech("stop the music"))


class TestFastPath(_TempMemory):
    def Say(self, text):
        from Brain import Rules
        from System.FastCommands import Find
        m = Find(text)
        self.assertIsNotNone(m, text)
        self.assertEqual(m.tool, "learn_rule")
        return m.reply(Rules.Learn(**m.args))

    def TestAConversation(self):
        self.assertEqual(self.Say("from now on call me boss"), "Got it. I'll do that from now on.")
        self.assertEqual(self.Say("From now on, call me boss."), "I already do that.")
        self.assertEqual(self.Say("stop calling me boss"), "Okay, I won't call you that any more.")
        self.assertEqual(self.Rules(), ["Do not call the user boss."])
        self.assertEqual(self.Say("from now on, call me chief"), "Got it. I'll do that from now on.")
        self.assertEqual(self.Rules(), ["Call me chief."])          # no contradicting pair left

    def TestStopCallingMeSirWithNoRuleYet(self):
        # "sir" is the built-in default, not a stored rule: it becomes one.
        self.assertEqual(self.Say("don't call me sir anymore"), "Okay, I won't call you that any more.")
        self.assertEqual(self.Rules(), ["Do not call the user sir."])

    def TestTheModelsToolWorksToo(self):
        import Main
        live = Main.UltronLive.__new__(Main.UltronLive)
        live.ui = mock.MagicMock()
        live.ui.muted = False
        live._action_registry = mock.MagicMock()
        live._plugin_registry = mock.MagicMock()
        live._plugin_registry.scheduling.return_value = None
        live._action_registry.scheduling.return_value = None
        fc = Main.types.FunctionCall(id="1", name="learn_rule", args={"rule": "Keep answers short"})
        fr = asyncio.run(live._ExecuteTool(fc))
        self.assertTrue(fr.response["result"].startswith("Kept"))
        self.assertEqual(self.Rules(), ["Keep answers short."])
        self.assertIn("learn_rule", {t["name"] for t in Main.TOOL_DECLARATIONS})


if __name__ == "__main__":
    unittest.main()
