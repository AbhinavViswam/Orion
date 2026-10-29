"""Fast commands: what counts as one, and that each path runs it once and only
once (typed, Gemini Live voice, and My own setup)."""
import asyncio
import types as pytypes
import unittest
from unittest import mock

import Tests.Helpers  # noqa: F401  (paths)
from System.FastCommands import Find


class TestMatching(unittest.TestCase):
    def Tool(self, text):
        m = Find(text)
        return None if m is None else (m.tool, m.args)

    def TestEverydayCommands(self):
        cases = {
            "volume up": ("computer_settings", {"action": "volume_up"}),
            "Turn the volume down please": ("computer_settings", {"action": "volume_down"}),
            "Ultron, mute.": ("computer_settings", {"action": "mute"}),
            "set volume to 40": ("computer_settings", {"action": "volume_set", "value": "40"}),
            "volume 30%": ("computer_settings", {"action": "volume_set", "value": "30"}),
            "take a screenshot": ("computer_settings", {"action": "screenshot"}),
            "lock the computer": ("computer_settings", {"action": "lock_screen"}),
            "hey ultron open notepad please": ("open_app", {"app_name": "Notepad"}),
            "launch visual studio code": ("open_app", {"app_name": "Visual Studio Code"}),
        }
        for text, want in cases.items():
            self.assertEqual(self.Tool(text), want, text)

    def TestTimeIsAnsweredHere(self):
        m = Find("what time is it?")
        self.assertIsNone(m.tool)
        self.assertTrue(m.reply("").startswith("It's "))

    def TestAnythingElseGoesToTheModel(self):
        for text in ("don't mute", "turn the volume up when the song ends", "volume up 10",
                     "launch chrome and go to youtube", "open youtube", "open my email",
                     "tell me a joke", "start a timer for 5 minutes", "what time is it in tokyo",
                     "open the door", "", "please", "mute the call and tell john i'm late"):
            self.assertIsNone(Find(text), text)

    def TestAFailureIsReportedNotHidden(self):
        m = Find("mute")
        self.assertEqual(m.reply('status=failed action=computer_settings reason="no audio device"'),
                         "That didn't work: no audio device.")


def _Live():
    """An UltronLive with just what the fast path touches."""
    import Main
    live = Main.UltronLive.__new__(Main.UltronLive)
    live._asst_name = "ULTRON"
    live._fast_turn = None
    live._fast_n = 0
    live._engine = None
    live._interrupted = False
    live._session_log = []
    live.ui = mock.MagicMock()
    live.ran = []

    async def ExecuteTool(fc):
        live.ran.append((fc.name, dict(fc.args)))
        return Main.types.FunctionResponse(id=fc.id, name=fc.name, response={"result": "status=done"})
    live.ExecuteTool = ExecuteTool
    live.session = mock.MagicMock()
    live.session.send_client_content = mock.AsyncMock()
    return live


class TestPaths(unittest.TestCase):
    def setUp(self):
        p = mock.patch("Main.GetFastCommandsEnabled", lambda: True)
        p.start()
        self.addCleanup(p.stop)

    def TestTypedCommandNeverReachesTheModel(self):
        live = _Live()
        asyncio.run(live.TypedCommand("volume up"))
        self.assertEqual(live.ran, [("computer_settings", {"action": "volume_up"})])
        live.session.send_client_content.assert_not_called()
        live.ui.WriteLog.assert_called_with("ULTRON: Volume up.")

    def TestTypedQuestionGoesToTheModel(self):
        live = _Live()
        asyncio.run(live.TypedCommand("what is the capital of France"))
        self.assertEqual(live.ran, [])
        live.session.send_client_content.assert_awaited_once()

    def TestLiveVoiceRunsOnceAndSkipsTheModelsCall(self):
        live = _Live()
        asyncio.run(live.CheckFastTurn("take a screenshot"))
        asyncio.run(live.CheckFastTurn("take a screenshot"))        # second event, same turn
        self.assertEqual(live.ran, [("computer_settings", {"action": "screenshot"})])
        self.assertEqual(live._fast_turn, "Screenshot taken.")

    def TestLiveLeavesTheTimeToTheModel(self):
        live = _Live()
        asyncio.run(live.CheckFastTurn("what time is it"))
        self.assertEqual((live.ran, live._fast_turn), ([], ""))

    def TestLiveDoesNothingInMyOwnSetup(self):
        live = _Live()
        live._engine = object()               # the pipeline handles it before the model
        asyncio.run(live.CheckFastTurn("mute"))
        self.assertEqual(live.ran, [])

    def TestSwitchedOff(self):
        live = _Live()
        with mock.patch("Main.GetFastCommandsEnabled", lambda: False):
            asyncio.run(live.TypedCommand("mute"))
        self.assertEqual(live.ran, [])
        live.session.send_client_content.assert_awaited_once()


class TestPipeline(unittest.TestCase):
    def TestMyOwnSetupAnswersWithoutTheModel(self):
        from Engines.EnginePipeline import PipelineEngine
        eng = PipelineEngine.__new__(PipelineEngine)
        eng._stt = pytypes.SimpleNamespace(transcribe=mock.AsyncMock(return_value="mute"))
        eng._mic, eng._mic_bytes = [b"\0\0" * 1600], 3200
        emitted, spoken = [], []

        async def Emit(msg):
            emitted.append(msg)

        async def Speak(text):
            spoken.append(text)
        eng.Emit, eng._Speak = Emit, Speak
        eng.send_client_content = mock.AsyncMock()
        eng.fast_path = mock.AsyncMock(return_value="Muted.")
        eng._Report = lambda *a: None
        asyncio.run(eng.FlushMicrophone())
        eng.fast_path.assert_awaited_once_with("mute")
        eng.send_client_content.assert_not_called()
        self.assertEqual(spoken, ["Muted."])


if __name__ == "__main__":
    unittest.main()
