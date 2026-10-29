"""The settings window, the top bar, and usage tracking."""
import shutil
import tempfile
import unittest
from pathlib import Path

from Tests.Helpers import App
from Brain import Usage


class TestUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = Usage.USAGE_PATH
        Usage.USAGE_PATH = self.tmp / "Usage.json"
        Usage.Reset()

    def tearDown(self):
        Usage.USAGE_PATH = self._saved
        Usage.Reset()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def TestTokensAddUpForToday(self):
        Usage.AddTokens(100, 80, 20)
        Usage.AddTokens(50)
        t = Usage.Today()
        self.assertEqual(t["tokens"], 150)
        self.assertEqual(t["turns"], 2)
        self.assertEqual(Usage.SessionTokens(), 150)

    def TestNothingIsCountedForZero(self):
        Usage.AddTokens(0)
        Usage.AddTokens(None)
        self.assertEqual(Usage.Today()["turns"], 0)

    def TestItSurvivesARestart(self):
        Usage.AddTokens(42)
        Usage.Reset()
        self.assertEqual(Usage.Today()["tokens"], 42)

    def TestRecentDaysIncludeEmptyDays(self):
        Usage.AddTokens(10)
        days = Usage.RecentDays(14)
        self.assertEqual(len(days), 14)
        self.assertEqual(days[-1]["tokens"], 10)
        self.assertEqual(sum(d["tokens"] for d in days[:-1]), 0)

    def TestToolStats(self):
        Usage.RecordTool("open_app", 200, True)
        Usage.RecordTool("open_app", 400, False)
        Usage.RecordTool("web_search", 1000, True)
        stats = {s["name"]: s for s in Usage.ToolStats(7)}
        self.assertEqual(stats["open_app"]["calls"], 2)
        self.assertEqual(stats["open_app"]["failed"], 1)
        self.assertEqual(stats["open_app"]["avg_ms"], 300)
        self.assertEqual(Usage.ToolStats(7)[0]["name"], "open_app")   # busiest first

    def TestABrokenFileStartsFresh(self):
        Usage.USAGE_PATH.write_text("not json", encoding="utf-8")
        Usage.Reset()
        self.assertEqual(Usage.Today()["tokens"], 0)
        Usage.AddTokens(5)
        self.assertEqual(Usage.Today()["tokens"], 5)


class TestSettingsWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        App()
        from PyQt6.QtCore import Qt
        from Interface import Window
        cls.Window = Window
        cls.w = Window.MainWindow()
        cls.w.get_plugins = lambda: []
        cls.w.resize(1280, 800)
        cls.w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        cls.w.show()
        for _ in range(10):
            App().processEvents()

    @classmethod
    def tearDownClass(cls):
        if cls.w.hud._reactor:
            cls.w.hud._reactor.stop()
        cls.w.close()

    def TestEverySectionOpens(self):
        self.w.OpenSettings()
        ov = self.w._settings_overlay
        for key in ("general", "brain", "voice", "look", "memory", "plugins", "usage", "system", "about"):
            ov.Show(key)
            App().processEvents()
            page = ov._pages[key]
            text = " ".join(l.text() for l in page.findChildren(self.Window.QLabel))
            self.assertNotIn("could not open", text, key)
        ov.close()
        App().processEvents()
        self.assertIsNone(self.w._settings_overlay)

    def TestBrainIsCalledBrain(self):
        self.w.OpenSettings("brain")
        ov = self.w._settings_overlay
        self.assertEqual(ov._title.text(), "Brain")
        ov.close()

    def TestGaugesOpenSystem(self):
        self.w.OpenSettings("system")
        self.assertEqual(self.w._settings_overlay._title.text(), "System")
        self.w._settings_overlay.close()

    def TestTopBar(self):
        self.w.UpdateMetrics()
        self.assertTrue(self.w._net_lbl.text())
        labels = " ".join(l.text() for l in self.w.findChildren(self.Window.QLabel))
        self.assertNotIn("Made by Abhinav Viswam", labels)

    def TestSwitchPaints(self):
        s = self.Window.ToggleSwitch(True)
        self.assertTrue(s.isChecked())
        s.grab()


if __name__ == "__main__":
    unittest.main()
