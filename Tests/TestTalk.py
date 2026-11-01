"""Orion and JARVIS talking out loud (System/Talk.py): the script, when they may talk, the turns with the phone,
stopping, and the moments Together starts. No phone, Link, voice or model: every one is a stand-in here."""
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import Tests.Helpers  # noqa: F401  (paths)
from System import Talk as K
from System import Together as T


class FakeUi:
    muted = False

    def __init__(self):
        self.log = []

    def WriteLog(self, line):
        self.log.append(line)

    def SetMicMuted(self, m, note=""):
        self.muted = m


class FakeLive:
    def __init__(self):
        self.ui = FakeUi()
        self._is_speaking = False
        self._talking = False
        self.context = []

    def interrupt(self):
        pass

    def AddContext(self, text):
        self.context.append(text)


def Wait(cond, s=3.0):
    end = time.time() + s
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class TestScript(unittest.TestCase):
    def TestParseKeepsOnlyTheirLines(self):
        s = K.Parse("Sure! Here it is:\n**JARVIS:** Evening, Orion.\nOrion: (grins) Welcome back, sir.\n"
                    "Narrator: they smile\n- jarvis - Quite.")
        self.assertEqual(s, [("JARVIS", "Evening, Orion."), ("Orion", "Welcome back, sir."), ("JARVIS", "Quite.")])
        self.assertTrue(K.Good(s))
        self.assertFalse(K.Good([("JARVIS", "a"), ("JARVIS", "b")]))

    def TestTheyNeverCallEachOtherSir(self):
        s = K.Parse("JARVIS: Orion, sir, I've learned he walks at six.\nOrion: Noted, JARVIS, sir.\n"
                    "Orion: Good evening sir, JARVIS.\nJARVIS: Enjoy the walk, sir.")
        self.assertEqual(s, [("JARVIS", "Orion, I've learned he walks at six."), ("Orion", "Noted, JARVIS."),
                             ("Orion", "Good evening JARVIS."), ("JARVIS", "Enjoy the walk, sir.")])
        self.assertIn("NEVER \"sir\"", K.SYSTEM)

    def TestParseCutsLongAndMany(self):
        s = K.Parse("\n".join(f"Orion: {'word ' * 50}" if i == 0 else f"JARVIS: line {i}" for i in range(9)))
        self.assertEqual(len(s), K.MAX_LINES)
        self.assertLessEqual(len(s[0][1].split()), K.MAX_WORDS)

    def TestTemplatesUseOnlyTheFacts(self):
        call = K.Template("call", {"caller": "Rahul"})
        self.assertEqual(call[0], ("JARVIS", "Orion, Rahul is calling."))
        brief = K.Template("briefing", {"battery": 34, "unread": 2})
        self.assertIn("34 percent", brief[1][1])
        self.assertIn("2 unread messages", brief[1][1])
        mem = K.Template("memory", {"from_jarvis": [("drink", "tea after 6")], "from_orion": [("launch", "Friday")]})
        self.assertIn("drink is tea after 6", mem[0][1])
        self.assertIn("launch is Friday", mem[1][1])
        for m in K.MOMENTS:
            self.assertTrue(K.Good(K.Template(m, {})), m)

    def TestCallsNeverWaitForABrain(self):
        with mock.patch.object(K, "_Gemini", side_effect=AssertionError("no brain for calls")):
            self.assertEqual(K.Write("call", {"caller": "Amma"})[0][1], "Orion, Amma is calling.")

    def TestPrivateModeNeverAsksGemini(self):
        with mock.patch.object(K, "_Private", lambda: True), \
             mock.patch.object(K, "_Local", lambda *a: None), \
             mock.patch.object(K, "_Gemini", side_effect=AssertionError("Gemini in private mode")):
            self.assertTrue(K.Good(K.Write("welcome", {})))

    def TestBrainFirstTemplateIfItWritesNonsense(self):
        with mock.patch.object(K, "_Private", lambda: False), mock.patch.object(K, "_Local", lambda *a: None), \
             mock.patch.object(K, "_Gemini", lambda *a: "JARVIS: Evening, Orion.\nOrion: Welcome home, sir."):
            self.assertEqual(K.Write("welcome", {})[1], ("Orion", "Welcome home, sir."))
        with mock.patch.object(K, "_Private", lambda: False), mock.patch.object(K, "_Local", lambda *a: None), \
             mock.patch.object(K, "_Gemini", lambda *a: "I can't do that."):
            self.assertEqual(K.Write("goodnight", {}), K.Template("goodnight", {}))


class _Base(unittest.TestCase):
    def setUp(self):
        self.level = "Important moments"
        self.present, self.jarvis = True, True
        self.clock = [datetime(2026, 11, 1, 18, 0).timestamp()]
        self.sent, self.spoken = [], []
        self.live = FakeLive()
        self.together = type("Tg", (), {"call_muted": False, "phone_state": {}})()
        self.k = K.Talk(self.live, self.together,
                        send=lambda kind, **f: self.sent.append((kind, f)) or True,
                        speak=lambda text: self.spoken.append(text) or True,
                        write=lambda m, f: K.Template(m, f),
                        present=lambda: self.present, jarvis_on=lambda: self.jarvis,
                        level=lambda: self.level, now=lambda: self.clock[0])
        self.k.last_at = 0
        self.k._SaveLast = lambda: None
        self.k._WaitQuiet = lambda: None

    def PhoneAnswers(self, ok=True):
        """Plays the phone: each talk_line is answered with talk_done."""
        def Run():
            seen = 0
            while not self.stop:
                lines = [f for kind, f in list(self.sent) if kind == "talk_line"]
                for f in lines[seen:]:
                    self.k.Handle({"type": "talk_done", "talk": f["talk"], "n": f["n"], "ok": ok})
                seen = len(lines)
                time.sleep(0.01)
        self.stop = False
        threading.Thread(target=Run, daemon=True).start()
        self.addCleanup(lambda: setattr(self, "stop", True))


class TestWhen(_Base):
    def TestLevels(self):
        self.assertTrue(K.Allowed("asked", "Only when I ask"))
        self.assertFalse(K.Allowed("memory", "Only when I ask"))
        self.assertTrue(K.Allowed("memory", "Important moments"))
        self.assertFalse(K.Allowed("activity", "Important moments"))
        self.assertTrue(K.Allowed("activity", "Chatty"))
        self.assertFalse(K.Allowed("asked", "Off"))

    def TestOnlyWhenEveryoneIsThere(self):
        self.assertTrue(self.k.CanTalk("memory")[0])
        self.present = False
        self.assertEqual(self.k.CanTalk("memory"), (False, "nobody at the laptop"))
        self.assertTrue(self.k.CanTalk("leaving")[0])           # you just locked it: that's the point
        self.present, self.jarvis = True, False
        self.assertEqual(self.k.CanTalk("memory"), (False, "JARVIS isn't running"))
        self.jarvis = True
        self.together.call_muted = True
        self.assertFalse(self.k.CanTalk("memory")[0])
        self.assertTrue(self.k.CanTalk("call")[0])

    def TestNightAndTheWaitBetweenTalks(self):
        self.clock[0] = datetime(2026, 11, 1, 23, 30).timestamp()
        self.assertEqual(self.k.CanTalk("memory"), (False, "night"))
        self.assertTrue(self.k.CanTalk("goodnight")[0])
        self.assertTrue(self.k.CanTalk("asked")[0])
        self.clock[0] = datetime(2026, 11, 1, 18, 0).timestamp()
        self.k.last_at = self.clock[0] - 4 * 60
        self.assertEqual(self.k.CanTalk("memory"), (False, "talked lately"))
        self.assertTrue(self.k.CanTalk("call")[0])
        self.k.last_at = self.clock[0] - 6 * 60
        self.assertTrue(self.k.CanTalk("memory")[0])            # memory waits only 5 minutes
        self.assertEqual(self.k.CanTalk("battery"), (False, "talked lately"))
        self.level = "Chatty"
        self.k.last_at = self.clock[0] - 9 * 60
        self.assertTrue(self.k.CanTalk("memory")[0])


class TestTurns(_Base):
    def TestTheyTakeTurns(self):
        self.PhoneAnswers()
        self.assertTrue(self.k.Perform([("JARVIS", "Evening, Orion."), ("Orion", "Welcome back, sir."),
                                        ("JARVIS", "Good to be home.")], "welcome"))
        lines = [f for kind, f in self.sent if kind == "talk_line"]
        self.assertEqual([(f["n"], f["text"]) for f in lines], [(0, "Evening, Orion."), (2, "Good to be home.")])
        self.assertEqual(self.spoken, ["Welcome back, sir."])
        self.assertEqual(self.sent[-1][0], "talk_end")
        self.assertFalse(self.live._talking)                     # the microphone is back
        self.assertIn("SYS: JARVIS: Evening, Orion.", self.live.ui.log)

    def TestAPhoneThatCantSpeakEndsIt(self):
        self.PhoneAnswers(ok=False)
        self.assertFalse(self.k.Perform([("JARVIS", "Hello."), ("Orion", "Hi.")], "asked"))
        self.assertEqual(self.spoken, [])

    def TestStopFromThePhone(self):
        holding = threading.Event()
        self.k.speak = lambda text: holding.wait(2) or True
        t = threading.Thread(target=lambda: self.k.Perform([("Orion", "One."), ("JARVIS", "Two.")], "asked"))
        t.start()
        self.assertTrue(Wait(lambda: self.k.talk_id))
        self.k.Handle({"type": "talk_stop", "talk": self.k.talk_id})
        holding.set()
        t.join(3)
        self.assertNotIn("talk_line", [kind for kind, _ in self.sent])
        self.assertEqual(self.sent[-1][0], "talk_stop")

    def TestAMomentWaitsForTheRightTime(self):
        self.PhoneAnswers()
        self.present = False
        self.assertTrue(self.k.Moment("welcome", {"laptop": ["flights to Delhi"]}))
        time.sleep(0.1)
        self.assertEqual(self.k.Tick(), None)
        self.assertIn("welcome", self.k.pending)
        self.present = True
        self.assertEqual(self.k.Tick(), "welcome")
        self.assertTrue(Wait(lambda: not self.k.busy and self.spoken))
        self.assertIn("1 thing", self.spoken[0])
        self.assertEqual(self.k.last_at, self.clock[0])

    def TestBriefingNeverLost(self):
        given = []
        self.jarvis = False                                      # can't talk: the briefing goes at once
        self.k.Moment("briefing", {}, after=lambda: given.append(1))
        self.assertEqual(given, [1])
        self.jarvis = True
        self.PhoneAnswers()
        self.k.Moment("briefing", {"battery": 50}, after=lambda: given.append(2))
        self.assertTrue(Wait(lambda: given == [1, 2]))
        self.assertTrue(self.spoken)

    def TestOldMomentsAreDropped(self):
        self.present = False
        self.k.Moment("battery", {"battery": 12})
        self.clock[0] += 31 * 60
        self.k.Tick()
        self.assertNotIn("battery", self.k.pending)

    def TestABriefingStuckBehindAnotherTalkStillHappens(self):
        given = []
        self.k.busy = True                                       # another talk is going on
        self.k.Moment("briefing", {}, after=lambda: given.append(1))
        self.assertIn("briefing", self.k.pending)
        self.clock[0] += 3 * 60
        self.k.Tick()
        self.assertEqual(given, [1])

    def TestAskedFromThePhone(self):
        self.PhoneAnswers()
        self.k.Handle({"type": "talk_request", "moment": "call", "facts": "Rahul", "urgent": True})
        self.assertTrue(Wait(lambda: self.spoken))
        self.assertEqual([f["text"] for k, f in self.sent if k == "talk_line"][0], "Orion, Rahul is calling.")

    def TestConflictLeavesANoteForTheAnswer(self):
        self.PhoneAnswers()
        self.k.Moment("conflict", {"category": "projects", "key": "meeting", "mine": "3 PM", "theirs": "4 PM"})
        self.assertTrue(Wait(lambda: self.live.context))
        self.assertIn("save_memory", self.live.context[0])


class TestFromTogether(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.memory = {c: {} for c in T.CATEGORIES}
        self.patches = [mock.patch.object(T, "ACTIVITY_FILE", d / "Activity.json"),
                        mock.patch.object(T, "SYNC_FILE", d / "PhoneTogether.json"),
                        mock.patch.object(T, "On", lambda key: True),
                        mock.patch("Brain.Memory.LoadMemory", lambda: self.memory),
                        mock.patch("Brain.Memory.SaveMemory", lambda m: None),
                        mock.patch("Brain.Privacy.Get", lambda: {"on": False, "kinds": [], "updated": ""})]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.moments = []
        live = FakeLive()
        self.t = T.Together(live, send=lambda kind, **f: True)
        self.t.SendLater = lambda kind, **f: None
        self.t.talk = type("K", (), {"Moment": lambda _s, m, f=None, after=None: self.moments.append((m, f)),
                                     "PhoneFacts": lambda _s: {}, "last_phone_event": 0,
                                     "Handle": lambda _s, ev: self.moments.append(("handled", ev))})()

    def TestBackHomeOnlyAfterLeaving(self):
        self.t.On_home({"at_home": True})
        self.assertEqual(self.moments, [])                       # JARVIS just started: not a welcome
        self.t.On_home({"at_home": False})
        T.AddActivity("laptop", "flights to Delhi")
        self.t.On_home({"at_home": True})
        self.assertEqual(self.moments[0][0], "welcome")
        self.assertEqual(self.moments[0][1]["laptop"], ["flights to Delhi"])

    def TestBatteryOnceWhenItDrops(self):
        self.t.On_phone_state({"battery": 40, "charging": False})
        self.t.On_phone_state({"battery": 14, "charging": False})
        self.t.On_phone_state({"battery": 12, "charging": False})
        self.assertEqual([m for m, _ in self.moments], ["battery"])

    def TestGoodnightOncePerEvening(self):
        with mock.patch.object(T, "datetime") as dt:
            dt.now.return_value = datetime(2026, 11, 1, 22, 15)
            dt.fromisoformat = datetime.fromisoformat
            self.assertTrue(self.t.Goodnight("ok good night Orion"))
            self.assertFalse(self.t.Goodnight("good night again"))
            dt.now.return_value = datetime(2026, 11, 1, 15, 0)
        self.assertEqual([m for m, _ in self.moments], ["goodnight"])

    def TestTalkEventsGoToTalk(self):
        self.t.Handle({"type": "talk_done", "talk": "k1", "n": 0, "ok": True})
        self.assertEqual(self.moments[0][0], "handled")

    def TestMemoryLearnedAndConflicts(self):
        from datetime import timedelta
        hour_ago = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        T.SetState(stamps={"projects/meeting": hour_ago})        # Orion set it itself an hour ago
        self.memory["projects"]["meeting"] = {"value": "3 PM", "updated": hour_ago[:10]}
        self.t.On_memory_offer({"first": False, "items": [
            {"category": "projects", "key": "meeting", "value": "4 PM", "updated": T.Now(), "source": "jarvis"},
            {"category": "preferences", "key": "drink", "value": "tea after 6", "updated": T.Now(), "source": "jarvis"}]})
        kinds = [m for m, _ in self.moments]
        self.assertIn("memory", kinds)
        self.assertIn("conflict", kinds)
        mem = dict(self.moments)["memory"]
        self.assertIn(("drink", "tea after 6"), mem["from_jarvis"])


if __name__ == "__main__":
    unittest.main()
