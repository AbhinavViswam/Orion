"""Clipboard help and crash alerts (System/ClipboardWatch.py, System/CrashWatch.py).

No clipboard, speaker or model is touched: the watchers are fed text directly,
and crash records go to a temp folder."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import Tests.Helpers  # noqa: F401  (paths)
from Tests.Helpers import ROOT
from System import ClipboardWatch as CW
from System import CrashWatch

OWN_FILE = ROOT / "Actions" / "OpenApp.py"
PY_ERROR = ('Traceback (most recent call last):\n'
            '  File "C:\\work\\shop\\app.py", line 12, in <module>\n'
            '    total = order["count"]\n'
            "KeyError: 'count'")
OWN_ERROR = ('Traceback (most recent call last):\n'
             f'  File "{OWN_FILE}", line 5, in Open\n'
             '    x = y\n'
             "NameError: name 'y' is not defined")
JS_ERROR = ("TypeError: Cannot read properties of undefined (reading 'map')\n"
            "    at List (webpack:///src/List.jsx:14:22)\n"
            "    at renderWithHooks (react-dom.development.js:16305:18)")


class TestClassify(unittest.TestCase):
    def TestErrors(self):
        self.assertEqual(CW.Classify(PY_ERROR), "error")
        self.assertEqual(CW.Classify(JS_ERROR), "error")

    def TestLinks(self):
        self.assertEqual(CW.Classify("https://www.youtube.com/watch?v=dQw4w9WgXcQ"), "youtube")
        self.assertEqual(CW.Classify("https://youtu.be/dQw4w9WgXcQ"), "youtube")
        self.assertEqual(CW.Classify("https://github.com/python/cpython"), "github")

    def TestOrdinaryTextIsIgnored(self):
        for text in ("hello there, how are you", "def Total(x):\n    return x + 1\n",
                     '{"a": 1, "b": 2, "c": 3}', "SELECT * FROM users WHERE id = 1",
                     "An Error: happened in the meeting notes today", "short",
                     "watch this https://youtu.be/dQw4w9WgXcQ it's great"):
            self.assertIsNone(CW.Classify(text), text)

    def TestHugeClipboardIsIgnored(self):
        self.assertIsNone(CW.Classify(PY_ERROR + "\n" + "x" * CW.MAX_CHARS))


class TestOffers(unittest.TestCase):
    def TestOwnErrorOffersSelfRepair(self):
        spoken, note = CW.Offer("error", OWN_ERROR)
        self.assertIn("my own code", spoken)
        self.assertIn("OpenApp.py", spoken)
        self.assertIn("self_repair", note)

    def TestOtherErrorOffersALook(self):
        spoken, note = CW.Offer("error", PY_ERROR)
        self.assertIn("Want me to look", spoken)
        self.assertIn("computer_control", note)

    def TestNoteNeverCarriesTheClipboard(self):
        for kind, text in (("error", PY_ERROR), ("youtube", "https://youtu.be/dQw4w9WgXcQ"),
                           ("github", "https://github.com/a/b")):
            _, note = CW.Offer(kind, text)
            self.assertNotIn("count", note if kind == "error" else "")
            self.assertNotIn(text, note)


class TestWatcherPacing(unittest.TestCase):
    def setUp(self):
        self.found = []
        self.w = CW.ClipboardWatch(on_found=lambda *a: self.found.append(a))

    def TestOffersOnce(self):
        self.assertEqual(self.w.Consider(PY_ERROR, now=1000), "error")
        self.assertIsNone(self.w.Consider(PY_ERROR, now=5000))       # same text again: never
        self.assertEqual(len(self.found), 1)

    def TestOneOfferAMinute(self):
        self.w.Consider(PY_ERROR, now=1000)
        self.assertIsNone(self.w.Consider("https://github.com/a/b", now=1030))
        self.assertEqual(self.w.Consider("https://github.com/a/b", now=1061), "github")

    def TestCooldownPerKind(self):
        self.w.Consider(PY_ERROR, now=1000)
        self.assertIsNone(self.w.Consider(JS_ERROR, now=1100))         # errors: 3 minutes
        self.assertEqual(self.w.Consider(JS_ERROR, now=1000 + CW.COOLDOWN["error"]), "error")

    def TestCallbackErrorDoesNotEscape(self):
        w = CW.ClipboardWatch(on_found=lambda *a: 1 / 0)
        self.assertEqual(w.Consider(PY_ERROR, now=1000), "error")


class TestCrashWatch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(CrashWatch, "CRASH_DIR", self.tmp),
                        mock.patch.object(CrashWatch, "LAST_FILE", self.tmp / "LastCrash.json"),
                        mock.patch.object(CrashWatch, "_said", {}),
                        mock.patch.object(CrashWatch, "_pending", [])]
        for p in self.patches:
            p.start()
        self.told = []
        CrashWatch.SetAnnouncer(lambda s, n: self.told.append((s, n)))

    def tearDown(self):
        CrashWatch.SetAnnouncer(None)
        for p in self.patches:
            p.stop()

    def TestOwnCrashIsAnnouncedAndKept(self):
        from System import SelfRepair
        self.assertTrue(CrashWatch.Report(OWN_ERROR, "thread x", now=100))
        self.assertEqual(len(self.told), 1)
        self.assertIn("OpenApp.py", self.told[0][0])
        self.assertIn("self_repair", self.told[0][1])
        self.assertEqual(SelfRepair.LastFailure().get("traceback"), OWN_ERROR)
        self.assertTrue((self.tmp / "LastCrash.json").exists())

    def TestSameCrashOnlyOnceInTenMinutes(self):
        CrashWatch.Report(OWN_ERROR, now=100)
        self.assertFalse(CrashWatch.Report(OWN_ERROR, now=200))
        self.assertTrue(CrashWatch.Report(OWN_ERROR, now=100 + CrashWatch.REPEAT_SECONDS))
        self.assertEqual(len(self.told), 2)

    def TestOtherCodeIsNotAnnounced(self):
        self.assertFalse(CrashWatch.Report(PY_ERROR, now=100))
        self.assertEqual(self.told, [])

    def TestCrashBeforeAVoiceWaits(self):
        CrashWatch.SetAnnouncer(None)
        CrashWatch.Report(OWN_ERROR, now=100)
        self.assertEqual(self.told, [])
        CrashWatch.SetAnnouncer(lambda s, n: self.told.append((s, n)))
        self.assertEqual(len(self.told), 1)

    def TestFatalLogBecomesATraceback(self):
        fatal = ("Windows fatal exception: access violation\n\n"
                 "Thread 0x0001 (most recent call first):\n"
                 '  File "C:\\x\\threading.py", line 300 in wait\n\n'
                 "Current thread 0x0002 (most recent call first):\n"
                 f'  File "{OWN_FILE}", line 5 in Open\n'
                 f'  File "{ROOT / "Main.py"}", line 10 in main\n')
        tb = CrashWatch.FatalAsTraceback(fatal)
        self.assertTrue(tb.startswith("Traceback (most recent call last):"))
        self.assertLess(tb.index("Main.py"), tb.index("OpenApp.py"))   # newest last
        self.assertNotIn("threading.py", tb)
        self.assertEqual(CrashWatch.FatalAsTraceback("nothing useful"), "")

    def TestReportNeverRaises(self):
        CrashWatch.SetAnnouncer(lambda s, n: 1 / 0)
        self.assertTrue(CrashWatch.Report(OWN_ERROR, now=100))
        self.assertFalse(CrashWatch.Report("", now=100))


class TestBugTracebacks(unittest.TestCase):
    def TestOnlyPlainBugsAreKept(self):
        import Main

        def Raise(e):
            try:
                raise e
            except BaseException as x:
                return x
        group = BaseExceptionGroup("tasks", [Raise(KeyError("k")), Raise(ConnectionResetError()),
                                             Raise(TimeoutError()), Raise(asyncio.CancelledError())])
        tbs = Main.BugTracebacks(group)
        self.assertEqual(len(tbs), 1)
        self.assertIn("KeyError", tbs[0])
        self.assertEqual(Main.BugTracebacks(Raise(OSError("net"))), [])


if __name__ == "__main__":
    unittest.main()
