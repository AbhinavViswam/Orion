"""The 2026-11-04 fix round (ISSUES.md), UI and Main.py side. Built off-screen with a
temp config: the real Config/ApiKeys.json is never read for writing. No camera, mic
or network — the camera is a fake cv2 and Main is imported, never started."""
import ast
import asyncio
import concurrent.futures
import contextlib
import io
import json
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from Tests.Helpers import App, ROOT, Tree

_TMP = None
_PATCHES = []


def setUpModule():
    # Every config read/write in this file goes to a temp ApiKeys.json
    global _TMP
    App()
    from Brain import Settings
    from Interface import Window
    _TMP = Path(tempfile.mkdtemp())
    cfg = _TMP / "ApiKeys.json"
    cfg.write_text(json.dumps({"assistant_name": "Friday", "user_name": "Dilsha"}), encoding="utf-8")
    _PATCHES[:] = [mock.patch.object(Settings, "CONFIG_FILE", cfg),
                   mock.patch.object(Settings, "CONFIG_DIR", _TMP),
                   mock.patch.object(Window, "API_FILE", cfg),
                   mock.patch.object(Window, "CONFIG_DIR", _TMP)]
    for p in _PATCHES:
        p.start()


def tearDownModule():
    for p in reversed(_PATCHES):
        p.stop()
    shutil.rmtree(_TMP, True)


def Pump(n=20):
    for _ in range(n):
        App().processEvents()


def Cfg() -> dict:
    return json.loads((_TMP / "ApiKeys.json").read_text(encoding="utf-8"))


class _WindowBase(unittest.TestCase):
    """One real MainWindow, shown off-screen."""
    activatable = False     # the off-screen platform only activates a window that is "shown"

    @classmethod
    def setUpClass(cls):
        App()
        from PyQt6.QtCore import Qt
        from Interface import Window
        cls.Window = Window
        cls.w = Window.MainWindow()
        cls.w.resize(1280, 780)
        if not (cls.activatable and App().platformName() == "offscreen"):
            cls.w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        cls.w.show()
        Pump()

    @classmethod
    def tearDownClass(cls):
        cls.w.StopCameraStream()
        if cls.w.hud._reactor:
            cls.w.hud._reactor.stop()
        cls.w._quitting = True
        cls.w.close()
        Pump()


# ── H1 ──────────────────────────────────────────────────────────────────────
class TestSetupKeepsSettings(_WindowBase):
    def TestNewKeyKeepsEverythingElse(self):
        from Brain.Settings import PatchConfig
        PatchConfig(voice="Puck", telegram_enabled=True, push_to_talk_enabled=True,
                    gemini_api_key="old-key")
        self.w.OnSetupDone("  new-key-1234567890  ", "windows")
        c = Cfg()
        self.assertEqual(c["gemini_api_key"], "new-key-1234567890")
        self.assertEqual(c["os_system"], "windows")
        for k, v in (("assistant_name", "Friday"), ("user_name", "Dilsha"), ("voice", "Puck"),
                     ("telegram_enabled", True), ("push_to_talk_enabled", True)):
            self.assertEqual(c.get(k), v, k)
        self.assertTrue(self.w._ready)
        self.assertEqual(self.w._assistant_name, "Friday")


# ── H12 ─────────────────────────────────────────────────────────────────────
class TestEscape(_WindowBase):
    activatable = True      # shortcuts need an active window

    def setUp(self):
        from PyQt6.QtTest import QTest
        self.QTest = QTest
        self.w.on_interrupt = mock.MagicMock()
        self.w.activateWindow()
        if not QTest.qWaitForWindowActive(self.w, 1000):
            self.skipTest("the off-screen window can't become active, so shortcuts don't fire")

    def _Esc(self, target):
        from PyQt6.QtCore import Qt
        self.QTest.keyClick(target, Qt.Key.Key_Escape)
        Pump()

    def TestEscClosesSettingsWithoutInterrupting(self):
        self.w.OpenSettings("about")
        Pump()
        ov = self.w._settings_overlay
        self.assertTrue(ov is not None and ov.isVisible())
        self._Esc(ov.focusWidget() or ov)
        self.assertIsNone(self.w._settings_overlay)
        self.w.on_interrupt.assert_not_called()

    def TestEscWithNothingOpenInterrupts(self):
        self.assertIsNone(getattr(self.w, "_settings_overlay", None))
        self.w._clipboard_panel.hide()
        self._Esc(self.w)
        self.w.on_interrupt.assert_called_once()


# ── M2 ──────────────────────────────────────────────────────────────────────
class TestHoldHintFromAThread(_WindowBase):
    def TestItGoesThroughTheSignal(self):
        from Interface.Window import OrionUI
        ui = OrionUI.__new__(OrionUI)
        ui._win = self.w
        self.w.ShowHoldHint("")
        t = threading.Thread(target=ui.SetHoldHint, args=("CTRL+SPACE",))
        t.start()
        t.join(2)
        # Nothing touched the HUD from that thread...
        self.assertEqual(self.w.hud.hold_hint, "")
        Pump()
        # ...the GUI thread did, once the queued signal ran
        self.assertEqual(self.w.hud.hold_hint, "CTRL+SPACE")

    def TestMainNeverTouchesTheHudDirectly(self):
        src = (ROOT / "Main.py").read_text(encoding="utf-8")
        self.assertNotIn("ShowHoldHint", src)
        self.assertNotIn("hold_hint", src)


# ── M4 ──────────────────────────────────────────────────────────────────────
class TestClipboardHelpSetting(_WindowBase):
    def _Copy(self, text):
        from PyQt6.QtWidgets import QApplication
        self.w._clipboard_panel.hide()
        QApplication.clipboard().setText(text)
        Pump()
        self.w.OnClipboardChanged()
        Pump()
        return self.w._clipboard_panel.isVisible()

    def TestOffShowsNoPanel(self):
        with mock.patch("Brain.Settings.GetClipboardHelpEnabled", lambda: False):
            self.assertFalse(self._Copy("hunter2-my-secret-password"))

    def TestOnShowsThePanel(self):
        with mock.patch("Brain.Settings.GetClipboardHelpEnabled", lambda: True):
            self.assertTrue(self._Copy("Traceback (most recent call last): boom"))
        self.w._clipboard_panel.hide()


# ── M5 ──────────────────────────────────────────────────────────────────────
class TestAssistantName(_WindowBase):
    def TestLogUsesTheConfiguredName(self):
        self.assertEqual(self.w._log._ai_name_lc, "friday")

    def TestRenamedLinesAreStyledAsAi(self):
        from Interface.Window import LogWidget
        log = LogWidget()
        log._ai_name_lc = self.w._log._ai_name_lc
        log.Enqueue("Friday: hello there")
        self.assertEqual(log._tag, "ai")
        log._tmr.stop()

    def TestRenameAppliesAtOnceAndReconnects(self):
        self.w.QueueReconnect = mock.MagicMock()
        try:
            self.w.ApplyNameUpdate("Edith", "Dilsha")
            self.assertEqual(self.w._log._ai_name_lc, "edith")
            self.assertEqual(Cfg()["assistant_name"], "Edith")
            self.w.QueueReconnect.assert_called_once()
        finally:
            del self.w.QueueReconnect
            self.w.ApplyNameUpdate("Friday", "Dilsha")


# ── M6 ──────────────────────────────────────────────────────────────────────
class _FakeCap:
    live = []           # captures open right now
    made = 0

    def __init__(self, *a):
        _FakeCap.made += 1
        self._open = True
        _FakeCap.live.append(self)

    def isOpened(self):
        return self._open

    def read(self):
        return False, None

    def release(self):
        self._open = False
        if self in _FakeCap.live:
            _FakeCap.live.remove(self)


class TestCameraStartStop(_WindowBase):
    def setUp(self):
        fake = types.SimpleNamespace(VideoCapture=_FakeCap, CAP_DSHOW=700, CAP_ANY=0,
                                     IMWRITE_JPEG_QUALITY=1, imencode=lambda *a: (True, b""))
        p = mock.patch.dict(sys.modules, {"cv2": fake})
        p.start()
        self.addCleanup(p.stop)
        _FakeCap.live.clear()
        _FakeCap.made = 0

    def _Wait(self, cond, secs=3.0):
        end = time.monotonic() + secs
        while time.monotonic() < end and not cond():
            Pump(2)
            time.sleep(0.02)
        return cond()

    def TestQuickRestartKeepsOneStream(self):
        w = self.w
        w.StartCameraStream()
        first_stop, first = w._cam_stop, w._cam_thread
        w.StartCameraStream()                     # straight away
        second_stop, second = w._cam_stop, w._cam_thread
        self.assertIsNot(first_stop, second_stop)
        self.assertTrue(first_stop.is_set())       # the old stream stays stopped
        self.assertFalse(second_stop.is_set())
        self.assertTrue(self._Wait(lambda: not first.is_alive()))
        self.assertTrue(self._Wait(lambda: _FakeCap.made == 2))
        self.assertTrue(second.is_alive())
        self.assertEqual(len(_FakeCap.live), 1)    # one capture, not two
        Pump()
        self.assertEqual(w._hud_cam_stack.currentIndex(), 1)   # the old thread didn't hide the HUD feed

        w.StopCameraStream()
        self.assertTrue(self._Wait(lambda: not second.is_alive()))
        self.assertEqual(_FakeCap.live, [])
        self.assertTrue(self._Wait(lambda: w._hud_cam_stack.currentIndex() == 0))


# ── Main.py (imported, never started) ───────────────────────────────────────
def _Live():
    import Main
    live = Main.OrionLive.__new__(Main.OrionLive)
    live.ui = mock.MagicMock()
    live._engine = None
    live._reply_live = False
    live._interrupted = False
    live._interrupts = 0
    live._talk = None
    live.audio_in_queue = None
    live._play_cursor = 0.0
    live._turn_done_event = None
    live._loop = None
    live.out_queue = None
    live.SetSpeaking = lambda on: None
    return live


class TestInterruptFlag(unittest.TestCase):
    """H11: a stop after the reply finished must not eat the next answer."""

    def TestStopWithNoLiveReplyLeavesNoFlag(self):
        live = _Live()
        live.interrupt()
        self.assertFalse(live._interrupted)
        self.assertEqual(live._interrupts, 1)

    def TestStopDuringAReplyDropsOnlyThatReply(self):
        live = _Live()
        live._reply_live = True
        live.audio_in_queue = asyncio.Queue()
        for _ in range(3):
            live.audio_in_queue.put_nowait(b"x")
        live.interrupt()
        self.assertTrue(live._interrupted)
        self.assertTrue(live.audio_in_queue.empty())

    def TestReceiveLoopClearsTheFlagAtTurnComplete(self):
        # The turn_complete branch sets _reply_live False and clears _interrupted
        src = (ROOT / "Main.py").read_text(encoding="utf-8")
        i = src.index("if sc.turn_complete:")
        block = src[i:i + 900]
        self.assertIn("self._reply_live = False", block)
        self.assertIn("self._interrupted = False", block)

    def TestLocalEngineNeverSetsTheFlag(self):
        live = _Live()
        live._reply_live = True
        live._engine = mock.MagicMock()
        live.interrupt()
        self.assertFalse(live._interrupted)
        live._engine.interrupt.assert_called_once()


class TestBadKeyCheck(unittest.TestCase):
    """M1: the inline condition in Main.py's run loop, pulled out and evaluated."""

    @classmethod
    def setUpClass(cls):
        cls.test = None
        for n in ast.walk(Tree(ROOT / "Main.py")):
            if isinstance(n, ast.If) and "api key not valid" in ast.unparse(n.test):
                cls.test = compile(ast.Expression(n.test), "Main.py", "eval")
                break

    def _Bad(self, err):
        if self.test is None:
            self.skipTest("the bad-key condition was not found in Main.py")
        return eval(self.test, {}, {"err_str": err, "_low": err.lower()})

    def TestASchema1007IsNotABadKey(self):
        self.assertFalse(self._Bad("received 1007 (invalid frame payload data) Invalid JSON payload "
                                   "received. Unknown name \"foo\" at 'setup.tools[0]'"))

    def TestRealBadKeysAreCaught(self):
        self.assertTrue(self._Bad("received 1007 API key not valid. Please pass a valid API key."))
        self.assertTrue(self._Bad("400 API_KEY_INVALID"))
        self.assertTrue(self._Bad("1007 invalid api key"))


class TestSmallMainFixes(unittest.TestCase):
    def TestBackoffIsSetOnSelf(self):
        # L1: no plain local `_conn_backoff = <number>` left
        bad = [n.lineno for n in ast.walk(Tree(ROOT / "Main.py"))
               if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
               and any(isinstance(t, ast.Name) and t.id == "_conn_backoff" for t in n.targets)]
        self.assertEqual(bad, [])

    def TestFailedFutureIsLogged(self):
        # L3
        import Main
        fut = concurrent.futures.Future()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            Main._Watch(fut, "typed command")
            fut.set_exception(RuntimeError("boom"))
        self.assertIn("typed command failed: boom", out.getvalue())

    def TestTypedTextWithNoLoopIsNotLostSilently(self):
        # L3
        live = _Live()
        live.OnTextCommand("hello")
        msg = live.ui.WriteLog.call_args[0][0]
        self.assertIn("Not connected", msg)

    def TestFullOutQueueDropsTheOldest(self):
        # L4
        live = _Live()
        q = asyncio.Queue(maxsize=2)
        live.out_queue = q
        for i in range(5):
            live.PutOut({"n": i})           # no QueueFull raised
        self.assertEqual([q.get_nowait()["n"] for _ in range(2)], [3, 4])

    def TestPlaceholderNameHasNoStraySpaces(self):
        # L5
        src = (ROOT / "Main.py").read_text(encoding="utf-8")
        self.assertNotRegex(src, r'"JARVI\s+S"')
        import Main
        self.assertEqual(Main.OrionLive.__init__.__code__.co_consts.count("JARVI    S"), 0)


if __name__ == "__main__":
    unittest.main()
