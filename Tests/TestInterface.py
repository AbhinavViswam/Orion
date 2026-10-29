"""Interface: reactor, hotkeys, app matching, and the window itself (built
off-screen; nothing is shown and Ultron is not started)."""
import shutil
import tempfile
import unittest
from pathlib import Path

from Tests.Helpers import App, ROOT
from PyQt6.QtGui import QColor, QIcon

from Interface import AppIdentity, Hotkey, Reactor


class TestReactorStates(unittest.TestCase):
    def TestStateMapping(self):
        self.assertEqual(Reactor.StateOf("LISTENING", False, False), Reactor.LISTENING)
        self.assertEqual(Reactor.StateOf("THINKING", False, False), Reactor.THINKING)
        self.assertEqual(Reactor.StateOf("LISTENING", True, False), Reactor.SPEAKING)
        self.assertEqual(Reactor.StateOf("SPEAKING", False, True), Reactor.MUTED)
        self.assertEqual(Reactor.StateOf("INITIALISING", False, False), Reactor.ASLEEP)

    def TestApproachEasesAndNeverOvershoots(self):
        v = 0.0
        for _ in range(200):
            v = Reactor.Approach(v, 1.0, 8.0, 1 / 60)
            self.assertLessEqual(v, 1.0)
        self.assertGreater(v, 0.99)


class TestReactorRendering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        App()

    def _Check(self, r):
        for state, level in ((Reactor.IDLE, 0.0), (Reactor.SPEAKING, 0.8), (Reactor.MUTED, 0.0)):
            for _ in range(30):
                r.step(1 / 60, state, level)
            crisp, bloom = r.render(200, QColor("#dcefff"))
            self.assertEqual((crisp.width(), crisp.height()), (200, 200))
            self.assertFalse(bloom.isNull())

    def TestBuiltInReactorRenders(self):
        self._Check(Reactor.Reactor())

    def TestMainReactorRenders(self):
        self._Check(Reactor.MakeReactor())

    def TestReactorBreathesOnlyWhileSpeaking(self):
        r = Reactor.MakeReactor()
        if not hasattr(r, "breath"):
            self.skipTest("the built-in reactor has no breathing")
        for _ in range(120):
            r.step(1 / 60, Reactor.SPEAKING, 0.6)
        self.assertGreater(r.breath, 0.9)
        for _ in range(240):
            r.step(1 / 60, Reactor.IDLE, 0.0)
        self.assertLess(r.breath, 0.05)

    def TestASlowFrameDoesNotJump(self):
        r = Reactor.Reactor()
        r.step(5.0, Reactor.SPEAKING, 1.0)          # a 5 s stall counts as 0.1 s
        self.assertLess(r.t, 0.2)


class TestHotkey(unittest.TestCase):
    def TestChordLabels(self):
        self.assertTrue(Hotkey.ChordLabel())
        self.assertEqual(Hotkey.ChordLabel(("ctrl", "space")), Hotkey.QtSequence(("ctrl", "space")))


class TestAppIdentity(unittest.TestCase):
    def TestFoldIgnoresCaseAndAccents(self):
        self.assertEqual(AppIdentity.fold("Café"), AppIdentity.fold("CAFE"))

    def TestWholeWordsOnly(self):
        self.assertTrue(AppIdentity.ContainsWord("google chrome", "chrome"))
        self.assertFalse(AppIdentity.ContainsWord("chromecast", "chrome"))

    def TestUnknownAppFallsThrough(self):
        self.assertEqual(AppIdentity.LauncherName("someappnobodyknows"), "someappnobodyknows")


class TestWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        App()
        from Interface import Window
        cls.Window = Window
        cls.w = Window.MainWindow()
        cls.w.resize(1280, 780)
        from PyQt6.QtCore import Qt
        cls.w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        cls.w.show()
        for _ in range(10):
            App().processEvents()

    @classmethod
    def tearDownClass(cls):
        if cls.w.hud._reactor:
            cls.w.hud._reactor.stop()
        cls.w.close()

    def TestConversationDrawerStartsOpen(self):
        self.assertTrue(self.w._right_panel.isVisible())
        self.assertGreater(self.w.hud.right_inset, 0)

    def TestReactorIsRunning(self):
        self.assertIsNotNone(self.Window.ReactorThread)
        self.assertIsNotNone(self.w.hud._reactor)

    def TestMuteButtonShowsAMic(self):
        for muted in (False, True):
            self.w._muted = muted
            self.w.StyleMuteBtn()
            self.assertFalse(self.w._mute_btn.icon().isNull())
            self.assertEqual(self.w._mute_btn.text(), "")

    def TestMicIconDraws(self):
        self.assertIsInstance(self.Window.MicIcon("#ffffff", True), QIcon)

    def TestNoRemoteControlLeft(self):
        src = (ROOT / "Interface" / "Window.py").read_text(encoding="utf-8")
        self.assertNotIn("REMOTE CONTROL", src)

    def TestDesktopShortcutIsCreated(self):
        import sys
        tmp = Path(tempfile.mkdtemp())
        try:
            lnk = tmp / "ULTRON.lnk"
            pyw = Path(sys.executable).parent / "pythonw.exe"
            self.Window.MainWindow.CreateLnkWindows(str(lnk), str(pyw), str(ROOT / "Main.py"),
                                                    str(ROOT), str(ROOT / "Config" / "Ultron.ico"))
            self.assertTrue(lnk.exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
