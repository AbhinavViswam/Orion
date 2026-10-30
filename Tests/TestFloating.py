"""The floating reactor (Interface/FloatingReactor.py): one Orion, two looks, one renderer."""
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from Tests.Helpers import App


def Pump(ms: int = 50) -> None:
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        App().processEvents()
        time.sleep(0.005)


class TestSettings(unittest.TestCase):
    def setUp(self):
        App()
        from Interface import FloatingReactor as FR
        self.FR = FR
        self.tmp = Path(tempfile.mkdtemp())
        p = mock.patch.object(FR, "SETTINGS", self.tmp / "Floating.json")
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def TestDefaultsStartFloating(self):
        cfg = self.FR.Load()
        self.assertTrue(cfg["start_floating"])
        self.assertEqual(cfg["size"], 150)

    def TestSaveKeepsTheRest(self):
        self.FR.Save(size=200)
        self.FR.Save(x=10, y=20)
        cfg = self.FR.Load()
        self.assertEqual((cfg["size"], cfg["x"], cfg["y"]), (200, 10, 20))

    def TestOnlyOneOrion(self):
        with mock.patch.object(self.FR, "SERVER_NAME", f"Orion.Test.{time.time_ns()}"):
            first = self.FR.ClaimSingleInstance()
            self.assertIsNotNone(first)
            summoned = []
            self.FR.ServeSummons(first, lambda: summoned.append(1))
            self.assertIsNone(self.FR.ClaimSingleInstance())     # the second launch bows out
            Pump(300)
            first.close()
        self.assertEqual(summoned, [1])


class TestTwoLooks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        App()
        from PyQt6.QtCore import Qt
        from Interface import FloatingReactor as FR
        from Interface import Window
        cls.FR = FR
        cls.tmp = Path(tempfile.mkdtemp())
        cls._p = mock.patch.object(FR, "SETTINGS", cls.tmp / "Floating.json")
        cls._p.start()
        cls.w = Window.MainWindow()
        cls.w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        cls.f = FR.FloatingReactor(cls.w)
        cls.f.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        cls.w.AttachFloating(cls.f)

    @classmethod
    def tearDownClass(cls):
        cls.f.hide()
        if cls.w.hud._reactor:
            cls.w.hud._reactor.stop()
        cls.w._quitting = True
        cls.w.close()
        cls._p.stop()
        shutil.rmtree(cls.tmp, True)

    def setUp(self):
        self.w.ShowFloating()
        Pump(60)

    def TestFloatingHidesTheWindow(self):
        self.assertTrue(self.f.isVisible())
        self.assertFalse(self.w.isVisible())
        self.assertTrue(self.w.hud.float_on)

    def TestTheOneRendererKeepsDrawingForTheReactor(self):
        th = self.w.hud._reactor
        self.assertIsNotNone(th)
        Pump(150)
        self.assertTrue(th.visible)                    # the hidden window did not switch it off
        self.assertEqual(th.size, self.f._side)        # drawn at the reactor's small size
        self.assertIsNotNone(self.f._frame)

    def TestWindowHidesTheReactor(self):
        self.w.ShowWindow()
        Pump(60)
        self.assertTrue(self.w.isVisible())
        self.assertFalse(self.f.isVisible())
        self.assertFalse(self.w.hud.float_on)

    def TestClosingTheWindowFloatsInstead(self):
        self.w.ShowWindow()
        Pump(30)
        self.w.close()
        Pump(30)
        self.assertFalse(self.w._quitting)
        self.assertTrue(self.f.isVisible())

    def TestAConfirmBringsTheWindowUp(self):
        self.w._confirm_sig.emit("Delete the file?", "report.docx")
        Pump(60)
        self.assertTrue(self.w.isVisible())
        self.assertFalse(self.f.isVisible())
        self.w.HideConfirmBanner()

    def TestTypingGoesThroughTheWindowsPath(self):
        got = []
        self.w.on_text_command = got.append
        self.f.ToggleTyping()
        self.assertTrue(self.f._input.isVisible())
        self.f._input.setText("what time is it")
        self.f.SendTyped()
        Pump(100)
        self.assertEqual(got, ["what time is it"])
        self.assertFalse(self.f._input.isVisible())
        self.w.on_text_command = None

    def TestMenu(self):
        texts = [a.text() for a in self.f.Menu().actions() if a.text()]
        for want in ("Open Orion window", "Type to Orion", "Stop talking", "Settings", "Reactor size", "Quit Orion"):
            self.assertIn(want, texts)
        self.assertTrue(any(t in texts for t in ("Mute the mic", "Turn the mic on")))

    def TestSizeIsRemembered(self):
        self.f.SetSide(110)
        self.assertEqual(self.FR.Load()["size"], 110)
        self.assertGreaterEqual(self.f.width(), 110)
        self.f.SetSide(150)

    def TestStatusFollowsTheHud(self):
        self.w.ApplyState("THINKING")
        self.assertIn("THINKING", self.f.Status()[0])
        self.w.SetMuted(True, quiet=True)
        self.assertIn("MUTED", self.f.Status()[0])
        self.w.SetMuted(False, quiet=True)

    def TestPaintsWithoutErrors(self):
        self.f.repaint()
        self.w.hud.SetCaption("ai", "It's ten past five.")
        self.assertEqual(self.f.Caption(), "It's ten past five.")
        self.f.repaint()
