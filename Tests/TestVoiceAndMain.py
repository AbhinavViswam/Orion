"""Voice helpers and Main.py's wiring (Main is imported, never started)."""
import ast
import unittest

import numpy as np

from Tests.Helpers import App, ROOT, Tree
from Voice import AudioDevices, Echo


class TestVoice(unittest.TestCase):
    def TestBandEnergiesOfSilenceAreLow(self):
        quiet = np.zeros(1600, dtype=np.int16)
        loud = (np.sin(np.linspace(0, 200, 1600)) * 12000).astype(np.int16)
        self.assertLess(float(np.sum(Echo.BandEnergies(quiet, 16000))),
                        float(np.sum(Echo.BandEnergies(loud, 16000))))

    def TestEchoGuardStartsUncalibrated(self):
        g = Echo.EchoGuard()
        self.assertFalse(g.Calibrated)

    def TestPseudoDevicesAreRecognised(self):
        self.assertTrue(AudioDevices.IsPseudo("Microsoft Sound Mapper - Input"))
        self.assertFalse(AudioDevices.IsPseudo("Realtek Microphone"))


class TestMainWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        App()
        import Main
        cls.Main = Main

    def TestFilesItNeedsExist(self):
        self.assertTrue(self.Main.PROMPT_PATH.exists())
        self.assertEqual(self.Main.API_CONFIG_PATH, ROOT / "Config" / "ApiKeys.json")

    def TestUiCallbacksPointAtRealMethods(self):
        """`self.ui.on_x = self.Y` in Main.py: Y must be a method of OrionLive."""
        missing = []
        for n in ast.walk(Tree(ROOT / "Main.py")):
            if isinstance(n, ast.Assign) and len(n.targets) == 1:
                t, v = n.targets[0], n.value
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Attribute) \
                        and t.value.attr == "ui" and isinstance(v, ast.Attribute) \
                        and isinstance(v.value, ast.Name) and v.value.id == "self":
                    if not hasattr(self.Main.OrionLive, v.attr):
                        missing.append(v.attr)
        self.assertEqual(missing, [])

    def TestUiHasWhatMainCalls(self):
        """Every `self.ui.X(...)` Main.py calls exists on OrionUI."""
        from Interface.Window import OrionUI
        missing = set()
        for n in ast.walk(Tree(ROOT / "Main.py")):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and isinstance(n.func.value, ast.Attribute) and n.func.value.attr == "ui":
                if not hasattr(OrionUI, n.func.attr):
                    missing.add(n.func.attr)
        self.assertEqual(sorted(missing), [])

    def TestNoWakeWordLeft(self):
        src = (ROOT / "Main.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("wake_word", src)


if __name__ == "__main__":
    unittest.main()
