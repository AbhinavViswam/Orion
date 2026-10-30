"""JARVIS Link (phone): Orion pairs with the 6-digit code, then its alerts and long jobs reach the phone's wait.

Runs the real JARVIS Link code (../jarvis-link/jarvis_link.py) in this process, on a spare port, with its
settings in a temp folder; Orion's PhoneLink.json goes to a temp folder too. Skipped if Link isn't next to Orion."""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import Tests.Helpers  # noqa: F401  (paths)
from System import AlertBus, PhoneBridge

LINK_DIR = Path(__file__).resolve().parent.parent.parent / "jarvis-link"


def _StartLink():
    os.environ["APPDATA"] = tempfile.mkdtemp()      # Link's own files, never the real ones
    sys.path.insert(0, str(LINK_DIR))
    import jarvis_link as L
    L.PORT = 47897
    server = L.Server(("127.0.0.1", L.PORT), L.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return L, server


@unittest.skipUnless((LINK_DIR / "jarvis_link.py").exists(), "jarvis-link not next to Orion")
class TestPairingAndFrames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._appdata = os.environ.get("APPDATA")
        cls.L, cls.server = _StartLink()
        cls.store = mock.patch.object(PhoneBridge, "STORE", Path(tempfile.mkdtemp()) / "PhoneLink.json")
        cls.port = mock.patch.object(PhoneBridge, "LINK_PORT", cls.L.PORT)
        cls.store.start()
        cls.port.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.stop()
        cls.port.stop()
        if cls._appdata is not None:
            os.environ["APPDATA"] = cls._appdata

    def setUp(self):
        PhoneBridge.Save(None)
        self.L.save_orion(None)

    def Pair(self):
        code = self.L.ORION_PAIRING.start()
        ok, text = PhoneBridge.Pair(code)
        self.assertTrue(ok, text)
        return code

    def TestPairsWithTheRightCode(self):
        self.Pair()
        d = PhoneBridge.Load()
        self.assertEqual(d["link_id"], self.L.AGENT_ID)
        self.assertEqual(PhoneBridge.Sealed("status")["data"]["phones"], 0)
        self.assertIsNotNone(self.L.orion())
        self.assertNotIn("orion", self.L.paired())

    def TestWrongCodesAndNoCode(self):
        ok, text = PhoneBridge.Pair("123456")
        self.assertFalse(ok)
        self.assertIn("isn't showing a code", text)
        code = self.L.ORION_PAIRING.start()
        wrong = f"{(int(code) + 1) % 10 ** 6:06d}"
        for _ in range(4):
            self.assertIn("didn't match", PhoneBridge.Pair(wrong)[1])
        self.assertIn("cancelled", PhoneBridge.Pair(wrong)[1])
        self.assertFalse(PhoneBridge.Pair(code)[0])
        self.assertIsNone(PhoneBridge.Load())
        self.assertIn("6-digit", PhoneBridge.Pair("12")[1])

    def TestLinkNotRunning(self):
        with mock.patch.object(PhoneBridge, "LINK_PORT", 1):
            ok, text = PhoneBridge.Pair("123456")
        self.assertFalse(ok)
        self.assertIn("isn't running", text)
        self.assertFalse(PhoneBridge.Push("note", "x", "y"))        # never raises

    def TestAlertsReachThePhonesWait(self):
        self.Pair()
        with mock.patch("Brain.Settings.GetPluginEnabled", lambda n: True), \
             mock.patch("Brain.Settings.GetPluginSetting", lambda ns, k, d=None: True):
            self.assertTrue(AlertBus.AlertNow("reminder", "Reminder", "Call the dentist"))
        ev = self.L.AGENTS.snapshot()["events"][-1]
        self.assertEqual((ev["kind"], ev["what"], ev["text"]), ("orion", "reminder", "Call the dentist"))
        with mock.patch("Brain.Settings.GetPluginEnabled", lambda n: True), \
             mock.patch("Brain.Settings.GetPluginSetting", lambda ns, k, d=None: k != "send_reminders"):
            self.assertFalse(AlertBus.AlertNow("reminder", "Reminder", "switched off"))
        self.assertTrue(PhoneBridge.Report("dev_agent:1", "the dev agent", "working"))
        ok, text, _ = self.L.act_agents({})
        self.assertIn("Orion is working on the dev agent", text)

    def TestCardShowsInStatus(self):
        self.Pair()
        bridge = PhoneBridge.Bridge()
        bridge.state = lambda: {"muted": True, "speaking": False}
        bridge.SendCard()
        st = self.L.ORION_STATE.status()
        self.assertTrue(st["paired"] and st["running"] and st["muted"])

    def TestDisconnectBothSides(self):
        self.Pair()
        self.assertIn("Disconnected", PhoneBridge.Unpair())
        self.assertIsNone(PhoneBridge.Load())
        self.assertIsNone(self.L.orion())
        # Forgotten on Link's side: Orion's next frame says so.
        self.Pair()
        self.L.save_orion(None)
        with self.assertRaises(PhoneBridge.NotPaired):
            PhoneBridge.Sealed("status")

    def TestBridgeConnectsByItself(self):
        self.Pair()
        bridge = PhoneBridge.Bridge()
        with mock.patch.object(PhoneBridge, "WAIT_S", 1):
            bridge.Start()
            for _ in range(50):
                if bridge.connected:
                    break
                time.sleep(0.1)
        bridge.Stop()
        self.assertTrue(bridge.connected)
        self.assertTrue(bridge.Status()[1])
        self.assertTrue(bridge.Dot())


class TestWords(unittest.TestCase):
    def TestPlainDropsTheModelsInstructions(self):
        text = AlertBus.Plain("[SYSTEM_ALERT] CPU usage has been critically high (95%) for several seconds. "
                              "Warn the user in their language. [SYSTEM_ALERT] RAM is at 91%. Warn the user.")
        self.assertEqual(text, "CPU usage has been critically high (95%) for several seconds. RAM is at 91%.")

    def TestMonitorText(self):
        self.assertEqual(AlertBus.MonitorText("[MONITOR_ALERT] AI news\nHeadline: A model\nsnippet\nSource: Verge"),
                         ("Watching: AI news", "A model (Verge)"))

    def TestNotPairedStatus(self):
        with mock.patch.object(PhoneBridge, "Load", lambda: None):
            b = PhoneBridge.Bridge()
            self.assertEqual(b.Status(), ("Not connected", False))
            self.assertIsNone(b.Dot())


if __name__ == "__main__":
    unittest.main()
