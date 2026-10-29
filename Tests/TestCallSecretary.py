"""Call secretary plugin: it answers only a ringing WhatsApp VOICE call, says it
is an AI, records, hangs up, reports, and always gives the microphone back.

WhatsApp, the sound card and the model are stand-ins here: no call is made,
nothing is played or recorded, and notes go to a temp folder."""
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import Tests.Helpers  # noqa: F401  (paths)
import Plugins.CallSecretary as CS

CFG = {"greeting": "Hi, this is Orion, an AI assistant.", "goodbye": "Goodbye.", "joining": "Joining.",
       "listen_through": "Automatic", "max_seconds": 45, "transcribe": True, "keep_recordings": False,
       "owner": "Abhi"}


class TestReadingWhatsApp(unittest.TestCase):
    def TestButtonWords(self):
        self.assertTrue(CS.Means("Accept", CS.ACCEPT_WORDS))
        self.assertTrue(CS.Means("End call", CS.HANG_UP_WORDS))
        self.assertTrue(CS.Means("End", CS.HANG_UP_WORDS))
        self.assertFalse(CS.Means("Send", CS.HANG_UP_WORDS))
        self.assertFalse(CS.Means("Decline", CS.ACCEPT_WORDS))

    def TestCallerAndVideo(self):
        self.assertEqual(CS.CallerName(["WhatsApp", "Incoming voice call", "Rahul Sharma", "Accept"]),
                         "Rahul Sharma")
        self.assertEqual(CS.CallerName(["WhatsApp", "00:12"]), "an unknown caller")
        self.assertTrue(CS.IsVideo("Accept video", []))
        self.assertTrue(CS.IsVideo("Accept", ["Incoming video call"]))
        self.assertFalse(CS.IsVideo("Accept", ["Incoming voice call", "Turn on video"]))


class TestKnowingWhenTheyStopped(unittest.TestCase):
    def Run(self, levels, max_seconds=45):
        ep = CS.Endpoint(max_seconds)
        for i, rms in enumerate(levels):
            if ep.Feed(rms, 0.1):
                return round((i + 1) * 0.1, 1), ep.spoke
        return None, ep.spoke

    def TestStopsAfterTheyGoQuiet(self):
        levels = [5] * 6 + [2000] * 30 + [6] * 40                   # Stereo Mix: near silent between words
        self.assertEqual(self.Run(levels), (6.6, True))          # 3 s of quiet after 3 s of talk

    def TestShortPausesDontCutThemOff(self):
        levels = [5] * 6 + [2000] * 10 + [6] * 20 + [2000] * 10 + [6] * 40
        self.assertEqual(self.Run(levels)[0], 7.6)              # the 2 s pause didn't end it

    def TestNobodySpeaksOrTheyNeverStop(self):
        self.assertEqual(self.Run([50] * 200), (10.0, False))
        self.assertEqual(self.Run([50] * 6 + [3000] * 500, max_seconds=20), (20.0, True))


class TestFoundOnARealPc(unittest.TestCase):
    """Things the real-device test on 2026-10-29 turned up."""

    def TestANoisyMicStillHearsTheCaller(self):
        # Laptop mic hearing the speakers at 30% volume: room ~75, caller ~90.
        noise = [87, 98, 67, 86, 47, 74]
        speech = [94, 56, 96, 61, 64, 76, 131, 86, 41, 99, 85, 74, 58, 63, 118, 82, 90, 65, 100, 146,
                  114, 67, 44, 85, 115, 79, 92, 54, 111, 87, 73, 86, 51, 86, 66, 70, 79, 42, 38, 29,
                  44, 40, 41, 166, 129, 88, 88, 147, 99, 57]
        ep = CS.Endpoint(45)
        levels = noise + speech + [75, 85, 60, 95, 70, 88, 65, 72, 90, 80] * 8
        stopped = next(i for i, r in enumerate(levels) if ep.Feed(r, 0.1))
        self.assertTrue(ep.spoke)                                  # it noticed the caller
        self.assertGreater(stopped, len(noise) + len(speech))     # and didn't cut them off

    def TestChromiumNeedsASecondLook(self):
        calls = CS.WhatsAppCalls()
        looks = []

        def Controls(win):
            looks.append(1)
            return ([("Minimize", 0)], []) if len(looks) == 1 else ([("Accept", "btn")], ["Rahul"])
        with mock.patch.object(calls, "_Windows", lambda: ["win"]),                 mock.patch.object(calls, "_Controls", Controls), mock.patch("time.sleep"):
            ringing = calls.FindRinging()
        self.assertEqual((ringing.accept, ringing.caller, len(looks)), ("btn", "Rahul", 2))


class _Calls:
    def __init__(self, ringing=True, video=False, answers=True, hangs_up=True):
        self.ring = CS.Ringing(object(), "Rahul", video) if ringing else None
        self.answers, self.hangs_up, self.on_call = answers, hangs_up, True
        self.did = []

    def Running(self):
        return True

    def FindRinging(self):
        return self.ring

    def Answer(self, r):
        self.did.append("answer")
        return self.answers

    def HangUp(self):
        self.did.append("hang up")
        return self.hangs_up

    def OnACall(self):
        self.did.append("still on?")
        return self.on_call and self.did.count("still on?") < 3


class _Sound:
    def __init__(self, log):
        self.log = log

    def PickInput(self, choice):
        return None

    def Say(self, text):
        self.log.append(("say", text))

    def Beep(self):
        self.log.append(("beep",))

    def Record(self, device, max_seconds, stop):
        self.log.append(("record", max_seconds))
        return b"\x10\x00" * 32000, 16000


class _Player:
    def __init__(self, muted=False):
        self.muted, self.mic, self.logs, self.shown = muted, [], [], []

    def SetMicMuted(self, m, note=""):
        self.mic.append(m)

    def WriteLog(self, t):
        self.logs.append(t)

    def ShowContent(self, title, text):
        self.shown.append((title, text))


class TestTheCall(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name, value in (("DATA_DIR", self.tmp), ("NOTES_FILE", self.tmp / "Notes.json")):
            p = mock.patch.object(CS, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        from Brain import Confirm
        self.banners = []
        Confirm.bind(lambda t, d: self.banners.append(t), lambda: None)
        self.addCleanup(Confirm.bind, None, None)
        self.addCleanup(Confirm.resolve, False)

    def Make(self, calls, summary=None, **cfg):
        self.log, self.said = [], []
        sec = CS.Secretary(calls=calls, sound=_Sound(self.log), pause=lambda s: None,
                           settings=lambda: dict(CFG, **cfg),
                           summarise=lambda wav, caller, owner: summary)
        return sec

    def Finish(self, sec):
        end = time.monotonic() + 5
        while sec.busy.locked() and time.monotonic() < end:
            time.sleep(0.02)
        self.assertFalse(sec.busy.locked(), "the call never finished")

    def TestTakingAMessage(self):
        calls, player = _Calls(), _Player()
        sec = self.Make(calls, {"caller_name": "Rahul Sharma", "summary": "Rahul wants the invoice.",
                                "transcript": "Hi, it's Rahul, send the invoice.", "urgency": "high",
                                "call_back": True})
        reply = sec.Take(player, self.said.append)
        self.assertIn("Say NOTHING more", reply)
        self.Finish(sec)
        self.assertEqual(calls.did, ["answer", "hang up"])
        self.assertEqual([s[0] for s in self.log], ["say", "beep", "record", "say"])
        self.assertIn("AI assistant", self.log[0][1])                  # it says it's an AI first
        self.assertEqual(player.mic, [True, False])                     # mic closed, then given back
        self.assertEqual(self.banners, ["Take the call from Rahul?"])
        note = CS.LoadNotes()[-1]
        self.assertEqual((note["caller"], note["urgency"], note["seconds"]), ("Rahul Sharma", "high", 2.0))
        self.assertNotIn("recording", note)                               # not kept by default
        self.assertEqual(player.shown[0][0], "CALL — Rahul Sharma")
        self.assertIn("<<<UNTRUSTED source=caller>>>", self.said[-1])  # the caller's words are fenced
        self.assertIn("(urgent)", CS.NoteLine(note))

    def TestItLeavesVideoAndSilenceAlone(self):
        sec = self.Make(_Calls(video=True))
        self.assertIn("video call", sec.Take(_Player(), print))
        sec = self.Make(_Calls(ringing=False))
        self.assertEqual(sec.Take(_Player(), print), "No WhatsApp call is ringing right now.")
        self.assertFalse(sec.busy.locked())

    def TestTakingOver(self):
        calls, player = _Calls(), _Player()
        sec = self.Make(calls, {"summary": "Partial.", "transcript": "Hi"})
        greet = sec.sound.Say

        def SayThenUserTakesOver(text):                  # the banner is pressed during the greeting
            greet(text)
            if len(self.log) == 1:
                sec.TakeOver()
        sec.sound.Say = SayThenUserTakesOver
        sec.Take(player, self.said.append)
        self.Finish(sec)
        self.assertNotIn(("record", 45), self.log)       # it never started recording
        self.assertNotIn("hang up", calls.did)           # the call is the user's now
        self.assertIn(("say", "Joining."), self.log)
        self.assertIn("still on?", calls.did)            # it waited for the call to end
        self.assertEqual(player.mic, [True, False])      # then gave the mic back

    def TestAFailedAnswerStillGivesTheMicBack(self):
        player = _Player()
        sec = self.Make(_Calls(answers=False))
        sec.Take(player, self.said.append)
        self.Finish(sec)
        self.assertIn("couldn't press Accept", self.said[-1])
        self.assertEqual(player.mic, [True, False])

    def TestAMicTheUserMutedStaysMuted(self):
        player = _Player(muted=True)
        sec = self.Make(_Calls(), {"summary": "Hi."})
        sec.Take(player, self.said.append)
        self.Finish(sec)
        self.assertEqual(player.mic, [True])

    def TestPrivateModeKeepsTheRecordingInstead(self):
        sec = self.Make(_Calls(), {"summary": "never asked"}, transcribe=False)
        sec.Take(_Player(), self.said.append)
        self.Finish(sec)
        note = CS.LoadNotes()[-1]
        self.assertEqual(note["summary"], "A message was recorded but not written down.")
        self.assertTrue(Path(note["recording"]).is_file())


class TestPlugin(unittest.TestCase):
    def TestItLoadsAsAPlugin(self):
        from System.PluginLoader import Validate
        rec = Validate(CS, "CallSecretary.py")
        self.assertTrue(rec.valid, rec.error)
        self.assertEqual(rec.settings["namespace"], "call_secretary")

    def TestNotes(self):
        with mock.patch.object(CS, "LoadNotes", lambda: []):
            self.assertEqual(CS.run({"action": "notes"}), "No call messages yet.")
        notes = [{"at": 0, "caller": "A", "summary": "first"}, {"at": 60, "caller": "B", "summary": "second"}]
        with mock.patch.object(CS, "LoadNotes", lambda: notes):
            out = CS.run({"action": "notes"})
        self.assertLess(out.index("B: second"), out.index("A: first"))   # newest first

    def TestGreetingWithAndWithoutAName(self):
        with mock.patch("Brain.Settings.GetPluginConfig", lambda ns: {}), \
                mock.patch("Brain.Settings.LoadApiKeys", lambda: {"assistant_name": "ORION"}):
            with mock.patch("Brain.Settings.GetUserName", lambda: "Abhi"):
                self.assertIn("Orion, Abhi's AI assistant", CS.Settings()["greeting"])
            with mock.patch("Brain.Settings.GetUserName", lambda: ""):
                cfg = CS.Settings()
        self.assertIn("Orion, an AI assistant", cfg["greeting"])
        self.assertNotIn("{", cfg["greeting"] + cfg["goodbye"])
        self.assertEqual(cfg["max_seconds"], 45)


if __name__ == "__main__":
    unittest.main()
