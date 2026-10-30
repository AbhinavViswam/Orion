"""Requests from the phone: the desk runner (with a stand-in model and tools), lookups, controls, handoff,
cancelling, and Orion's confirmations reaching the phone. Then the whole road once: a pretend phone asks through
the real JARVIS Link code, Orion's bridge picks it up and answers. No real model, voice or Link is used."""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import Tests.Helpers  # noqa: F401  (paths)
from google.genai import types
from System import PhoneBridge
from System.PhoneTasks import PhoneTasks

LINK_DIR = Path(__file__).resolve().parent.parent.parent / "jarvis-link"


class FakeLive:
    def __init__(self):
        self._loop = None
        self._asst_name = "ORION"
        self.session = object()
        self.said, self.ran, self.interrupted = [], [], 0
        self.ui = SimpleNamespace(SetMicMuted=lambda m, note="": self.said.append(("muted", m)))

    def ToolDecls(self):
        return [{"name": "web_search", "description": "search", "parameters": {"type": "OBJECT", "properties": {}}},
                {"name": "screen_process", "description": "eyes", "parameters": {"type": "OBJECT", "properties": {}}}]

    async def ExecuteTool(self, fc):
        self.ran.append(fc.name)
        return types.FunctionResponse(id=fc.id, name=fc.name, response={"result": "3 flights, cheapest ₹4,200"})

    async def RunFastCommand(self, text):
        return "It's 5 PM." if text == "what time is it" else None

    def PluginSay(self, text):
        self.said.append(text)

    def interrupt(self):
        self.interrupted += 1


def Reply(text="", calls=()):
    content = types.Content(role="model", parts=[types.Part(function_call=c) for c in calls] or [types.Part(text=text)])
    return SimpleNamespace(text=text, function_calls=list(calls), candidates=[SimpleNamespace(content=content)])


def Run(tasks: PhoneTasks, task: dict) -> list:
    got = []
    tasks.answer = lambda tid, state, text, speak=True: got.append((tid, state, text))
    tasks._Run(task)
    return got


class TestDeskRunner(unittest.TestCase):
    def setUp(self):
        self.live = FakeLive()
        self.config = mock.patch.object(PhoneTasks, "_Config", lambda self: None)
        self.config.start()

    def tearDown(self):
        self.config.stop()

    def TestToolsThenAnswer(self):
        script = [Reply(calls=[types.FunctionCall(id="1", name="web_search", args={"q": "flights"}),
                               types.FunctionCall(id="2", name="screen_process", args={})]),
                  Reply("The cheapest flight is ₹4,200.")]
        seen = []
        gen = lambda contents, config: (seen.append(len(contents)), script.pop(0))[1]
        got = Run(PhoneTasks(self.live, None, gen), {"id": "t-1", "kind": "ask", "text": "cheapest flight to Delhi"})
        self.assertEqual(got, [("t-1", "completed", "The cheapest flight is ₹4,200.")])
        self.assertEqual(self.live.ran, ["web_search"])          # the live-session-only tool never ran
        self.assertEqual(seen, [1, 3])                            # ask; ask + model's calls + their results

    def TestFastCommandNeedsNoModel(self):
        gen = mock.Mock()
        got = Run(PhoneTasks(self.live, None, gen), {"id": "t-2", "kind": "ask", "text": "what time is it"})
        self.assertEqual(got, [("t-2", "completed", "It's 5 PM.")])
        gen.assert_not_called()

    def TestNoModelAnswer(self):
        got = Run(PhoneTasks(self.live, None, lambda c, k: None), {"id": "t-3", "kind": "ask", "text": "x"})
        self.assertEqual(got[0][1], "failed")

    def TestStopsAfterSixRounds(self):
        loop = lambda c, k: Reply(calls=[types.FunctionCall(id="1", name="web_search", args={})])
        got = Run(PhoneTasks(self.live, None, loop), {"id": "t-4", "kind": "ask", "text": "x"})
        self.assertEqual(len(self.live.ran), 6)
        self.assertIn("didn't finish", got[0][2])

    def TestCancelledBetweenSteps(self):
        tasks = PhoneTasks(self.live, None, None)

        def gen(c, k):
            tasks.cancelled.add("t-5")
            return Reply(calls=[types.FunctionCall(id="1", name="web_search", args={})])
        tasks.generate = gen
        self.assertEqual(Run(tasks, {"id": "t-5", "kind": "ask", "text": "x"}), [])   # nothing sent: Link did it

    def TestFilesRideAlong(self):
        d = Path(tempfile.mkdtemp())
        (d / "note.txt").write_text("buy milk", "utf-8")
        (d / "photo.jpg").write_bytes(b"\xff\xd8\xff")
        seen = []
        gen = lambda c, k: (seen.append(c[0].parts), Reply("A note and a photo."))[1]
        Run(PhoneTasks(self.live, None, gen), {"id": "t-6", "kind": "ask", "text": "what are these",
                                               "files": [str(d / "note.txt"), str(d / "photo.jpg"), str(d / "gone")]})
        parts = seen[0]
        self.assertEqual(len(parts), 3)
        self.assertIn("buy milk", parts[1].text)
        self.assertEqual(parts[2].inline_data.mime_type, "image/jpeg")


class TestWithoutModel(unittest.TestCase):
    def setUp(self):
        self.live = FakeLive()
        self.tasks = PhoneTasks(self.live, None, mock.Mock())

    def TestControls(self):
        self.assertEqual(Run(self.tasks, {"id": "t-1", "kind": "ctl", "cmd": "mute"})[0][2], "Orion is muted.")
        self.assertEqual(self.live.said[-1], ("muted", True))
        Run(self.tasks, {"id": "t-2", "kind": "ctl", "cmd": "stop_talking"})
        self.assertEqual(self.live.interrupted, 1)
        with mock.patch("Brain.Undo.UndoLast", lambda: "Undid: renamed photo.jpg"):
            self.assertEqual(Run(self.tasks, {"id": "t-3", "kind": "ctl", "cmd": "undo"})[0][2], "Undid: renamed photo.jpg")

    def TestCallsLookup(self):
        notes = [{"caller": "Mom", "summary": "Call back", "at": time.time()}]
        with mock.patch("Plugins.CallSecretary.LoadNotes", lambda: notes):
            text = Run(self.tasks, {"id": "t-4", "kind": "ask", "query": "calls"})[0][2]
        self.assertIn("Mom", text)
        self.tasks.generate.assert_not_called()

    def TestHandoffSpeaksAtTheDesk(self):
        got = Run(self.tasks, {"id": "t-5", "kind": "ask", "handoff": {"summary": "booking a cab", "task": "pick a time"}})
        self.assertEqual(got[0][1], "completed")
        self.assertIn("[HANDOFF]", self.live.said[-1])
        self.assertIn("booking a cab", self.live.said[-1])
        self.live.session = None
        self.assertIn("isn't listening", Run(self.tasks, {"id": "t-6", "kind": "ask", "handoff": {"summary": "x"}})[0][2])


class FakeGate:
    """Brain.Confirm's surface, enough for the mirror."""

    def __init__(self):
        self._show_cb = lambda t, d: self.shown.append(t)
        self._hide_cb = lambda: self.hidden.append(1)
        self._log_cb = None
        self.shown, self.hidden, self.resolved = [], [], []
        self.pending = ""

    def bind(self, show, hide, log=None):
        self._show_cb, self._hide_cb, self._log_cb = show, hide, log

    def PendingTitle(self):
        return self.pending

    def resolve(self, yes):
        self.resolved.append(yes)
        self.pending = ""
        self._hide_cb()


class TestConfirmOnThePhone(unittest.TestCase):
    def setUp(self):
        self.bridge = PhoneBridge.Bridge()
        self.bridge.connected = True
        self.gate = FakeGate()
        self.bridge.WrapConfirm(self.gate)
        self.bridge.WrapConfirm(self.gate)             # twice: still one mirror
        self.sent = []
        self.sealed = mock.patch.object(PhoneBridge, "Sealed", lambda op, **f: self.sent.append((op, f)) or {"ok": True})
        self.sealed.start()

    def tearDown(self):
        self.sealed.stop()

    def Wait(self, n):
        for _ in range(50):
            if len(self.sent) >= n:
                return
            time.sleep(0.02)

    def TestMirroredWhenAwayAndAnsweredOnPhone(self):
        with mock.patch.object(PhoneBridge, "IdleSeconds", lambda: 300):
            self.gate.pending = "Delete 3 files?"
            self.gate._show_cb("Delete 3 files?", "In Downloads")
        self.Wait(1)
        self.assertEqual(self.gate.shown, ["Delete 3 files?"])          # the laptop banner too
        op, f = self.sent[0]
        self.assertEqual((op, f["title"]), ("confirm", "Delete 3 files?"))
        self.bridge.OnTask({"id": "c-old", "kind": "confirm_answer", "yes": True})
        self.assertEqual(self.gate.resolved, [])                          # an old question: ignored
        self.bridge.OnTask({"id": f["id"], "kind": "confirm_answer", "yes": True})
        self.assertEqual(self.gate.resolved, [True])
        self.Wait(2)
        self.assertEqual(self.sent[1][0], "confirm_done")

    def TestNotMirroredAtTheDesk(self):
        with mock.patch.object(PhoneBridge, "IdleSeconds", lambda: 5):
            self.gate._show_cb("Shut down?", "")
        time.sleep(0.1)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.bridge.confirm_id, "")

    def TestTooManyHopsRefused(self):
        self.bridge.tasks = mock.Mock()
        self.bridge.OnTask({"id": "t-1", "kind": "ask", "hops": 3})
        self.bridge.tasks.Handle.assert_not_called()


@unittest.skipUnless((LINK_DIR / "jarvis_link.py").exists(), "jarvis-link not next to Orion")
class TestWholeRoad(unittest.TestCase):
    """Phone → Link → Orion's bridge → desk runner → Link → phone, with the real Link code."""

    def TestAskFromThePhone(self):
        appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = tempfile.mkdtemp()
        sys.path.insert(0, str(LINK_DIR))
        import jarvis_link as L
        L.PORT = 47896
        server = L.Server(("127.0.0.1", L.PORT), L.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        patches = [mock.patch.object(PhoneBridge, "STORE", Path(tempfile.mkdtemp()) / "PhoneLink.json"),
                   mock.patch.object(PhoneBridge, "LINK_PORT", L.PORT),
                   mock.patch.object(PhoneBridge, "WAIT_S", 1),
                   mock.patch.object(PhoneTasks, "_Config", lambda self: None)]
        for p in patches:
            p.start()
        try:
            ok, text = PhoneBridge.Pair(L.ORION_PAIRING.start())
            self.assertTrue(ok, text)
            bridge = PhoneBridge.Bridge()
            bridge.tasks = PhoneTasks(FakeLive(), PhoneBridge.Answer, lambda c, k: Reply("Two meetings today."))
            bridge.Start()
            for _ in range(50):
                if bridge.connected:
                    break
                time.sleep(0.1)
            ok, said, data = L.act_orion_ask({"text": "what's on today", "wait_s": 10})
            self.assertTrue(ok)
            self.assertEqual(said, "Two meetings today.")
            self.assertEqual(data["state"], "completed")
            ok, said, _ = L.act_orion_ctl({"cmd": "mute"})
            self.assertEqual(said, "Orion is muted.")
        finally:
            bridge.Stop()
            for p in patches:
                p.stop()
            server.shutdown()
            server.server_close()
            if appdata is not None:
                os.environ["APPDATA"] = appdata


if __name__ == "__main__":
    unittest.main()
