"""Orion and JARVIS together (Phase 4): "Hey JARVIS" turns let pass, quiet during calls, one briefing, the phone's
battery, the shared activity log, leaving home, and memory offered both ways but always reviewed. No phone,
Link or model; files go to a temp folder and memory is a dict here."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from Tests.Helpers import App
from System import Together as T


class FakeUi:
    def __init__(self, muted=False):
        self.muted = muted
        self.calls = []

    def SetMicMuted(self, m, note=""):
        self.calls.append((m, note))
        self.muted = m


class FakeLive:
    def __init__(self, muted=False):
        self.ui = FakeUi(muted)
        self._is_speaking = False
        self.interrupted = 0
        self.briefed = 0

    def interrupt(self):
        self.interrupted += 1

    def GiveBriefingNow(self):
        self.briefed += 1


class _Base(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.memory = {c: {} for c in ("identity", "preferences", "relationships", "rules", "notes", "projects")}

        def Update(u):
            for cat, kv in u.items():
                for k, v in kv.items():
                    self.memory.setdefault(cat, {})[k] = {"value": v["value"], "updated": "2026-10-30"}
        self.patches = [mock.patch.object(T, "ACTIVITY_FILE", d / "Activity.json"),
                        mock.patch.object(T, "SYNC_FILE", d / "PhoneTogether.json"),
                        mock.patch.object(T, "On", lambda key: True),
                        mock.patch("Brain.Memory.LoadMemory", lambda: self.memory),
                        mock.patch("Brain.Memory.UpdateMemory", Update),
                        mock.patch("Brain.Memory.SaveMemory", lambda m: None)]
        for p in self.patches:
            p.start()
        self.sent = []
        self.live = FakeLive()
        self.t = T.Together(self.live, send=lambda kind, **f: self.sent.append((kind, f)) or True)
        self.t.SendLater = lambda kind, **f: self.sent.append((kind, f))
        self.t.RunLater = lambda fn: fn()

    def tearDown(self):
        for p in self.patches:
            p.stop()


class TestOneVoice(_Base):
    def TestHeyJarvisTurnIsLetPass(self):
        self.live._is_speaking = True
        self.t.Handle({"type": "phone_listening"})
        self.assertTrue(self.t.PhoneTurn())
        self.assertEqual(self.live.interrupted, 1)
        self.t.OnTurn("what's the weather")            # JARVIS's turn: not logged as a laptop request
        self.assertEqual(T.Activity(), [])
        self.t.Handle({"type": "phone_idle"})
        self.assertFalse(self.t.PhoneTurn())

    def TestEndsByItself(self):
        self.t.Handle({"type": "phone_listening"})
        self.t.phone_turn_until = time.monotonic() - 1
        self.assertFalse(self.t.PhoneTurn())

    def TestSwitchedOff(self):
        with mock.patch.object(T, "On", lambda key: key != "one_voice"):
            self.t.Handle({"type": "phone_listening"})
        self.assertFalse(self.t.PhoneTurn())


class TestCallsAndHome(_Base):
    def TestQuietDuringACall(self):
        self.t.Handle({"type": "phone_call", "state": "ringing", "caller": "Mom"})
        self.assertEqual(self.live.ui.calls[-1], (True, "Phone call — Mom"))
        self.t.Handle({"type": "phone_call", "state": "idle"})
        self.assertEqual(self.live.ui.calls[-1], (False, ""))

    def TestAlreadyMutedStaysMuted(self):
        self.live.ui.muted = True
        self.t.Handle({"type": "phone_call", "state": "in_call"})
        self.t.Handle({"type": "phone_call", "state": "idle"})
        self.assertEqual(self.live.ui.calls, [])

    def TestLeavingHome(self):
        with mock.patch("System.PhoneBridge.IdleSeconds", lambda: 900):
            self.t.Handle({"type": "home", "at_home": False})
        self.assertTrue(self.live.ui.muted)
        self.t.Handle({"type": "home", "at_home": True})
        self.assertFalse(self.live.ui.muted)

    def TestLeavingHomeWhileSomeoneIsAtTheLaptop(self):
        with mock.patch("System.PhoneBridge.IdleSeconds", lambda: 10):
            self.t.Handle({"type": "home", "at_home": False})
        self.assertFalse(self.live.ui.muted)


class TestBriefingAndPhoneState(_Base):
    def TestOneBriefing(self):
        self.assertTrue(self.t.ShouldBrief())
        self.t.Handle({"type": "briefing_done", "date": T.Today()})
        self.assertFalse(self.t.ShouldBrief())
        self.t.Briefed()
        self.assertEqual(self.sent[-1], ("briefing_done", {"date": T.Today()}))
        self.t.Handle({"type": "briefing_handoff"})      # Orion already gave it today
        self.assertEqual(self.live.briefed, 0)

    def TestHandoff(self):
        self.t.Handle({"type": "briefing_handoff"})
        self.assertEqual(self.live.briefed, 1)

    def TestPhoneRing(self):
        self.assertIsNone(self.t.PhoneRing())
        self.t.Handle({"type": "phone_state", "battery": 41, "charging": True, "unread": 3})
        self.assertEqual(self.t.PhoneRing(), (41.0, "41", "Phone: 41%, charging, 3 unread"))
        self.t.phone_state["at"] -= 1000
        self.assertIsNone(self.t.PhoneRing())


class TestActivity(_Base):
    def TestBothSidesInOneLog(self):
        self.t.OnTurn("find  flights to Delhi")
        self.t.Handle({"type": "activity", "line": "Asked for the weather in Pune", "at": 1761800000})
        self.assertEqual([(r["device"], r["line"]) for r in T.Activity()],
                         [("laptop", "find flights to Delhi"), ("phone", "Asked for the weather in Pune")])
        self.assertEqual(self.sent[-1][0], "activity")
        self.assertIn("phone · Asked for the weather", T.ActivityWords())

    def TestKeepsFifty(self):
        for i in range(60):
            T.AddActivity("laptop", f"line {i}")
        self.assertEqual(len(T.Activity()), 50)
        self.assertEqual(T.Activity()[0]["line"], "line 10")


class TestOneMemory(_Base):
    """One shared memory (2026-10-30): automatic, no duplicates, newer timestamp wins, deletions carry over, and
    each entry says which assistant learned it."""

    def Item(self, key, value, updated="2026-10-30T10:00:00", cat="preferences", **kw):
        return {"category": cat, "key": key, "value": value, "updated": updated, **kw}

    def TestNewItemsAreSavedWithTheirSource(self):
        n = T.Merge([self.Item("address", "boss", source="jarvis"), self.Item("todo", "milk", cat="notes")])
        self.assertEqual(n, 2)
        self.assertEqual(self.memory["preferences"]["address"], {"value": "boss", "updated": "2026-10-30", "source": "jarvis"})
        self.assertIn("todo", self.memory["notes"])                       # every kind syncs now

    def TestNoDuplicates(self):
        self.memory["preferences"]["Favourite coffee"] = {"value": "Black", "updated": "2026-10-01"}
        n = T.Merge([self.Item("favourite_coffee", " black ", source="jarvis")])
        self.assertEqual(n, 0)
        self.assertEqual(list(self.memory["preferences"]), ["Favourite coffee"])
        self.assertNotIn("source", self.memory["preferences"]["Favourite coffee"])   # still Orion's own

    def TestNewerTimestampWins(self):
        self.memory["preferences"]["address"] = {"value": "sir", "updated": "2026-10-30"}
        T.SetState(stamps={"preferences/address": "2026-10-30T09:00:00"})
        self.assertEqual(T.Merge([self.Item("address", "boss", "2026-10-30T08:00:00")]), 0)   # older: Orion keeps sir
        self.assertEqual(T.Merge([self.Item("address", "boss", "2026-10-30T11:00:00")]), 1)   # newer: boss
        self.assertEqual(self.memory["preferences"]["address"]["value"], "boss")
        self.assertEqual(T.State()["stamps"]["preferences/address"], "2026-10-30T11:00:00")

    def TestDeletionsCarryOverButNotOverNewer(self):
        self.memory["identity"]["city"] = {"value": "Pune", "updated": "2026-10-30"}
        T.SetState(stamps={"identity/city": "2026-10-30T12:00:00"})
        T.Merge([self.Item("city", "", "2026-10-30T11:00:00", cat="identity", deleted=True)])
        self.assertIn("city", self.memory["identity"])                  # Orion changed it after that deletion
        T.Merge([self.Item("city", "", "2026-10-30T13:00:00", cat="identity", deleted=True)])
        self.assertNotIn("city", self.memory["identity"])

    def TestOrionsDeletionIsntUndoneByAnOlderCopy(self):
        T.SetState(tombstones={"identity/city": "2026-10-30T12:00:00"})
        self.assertEqual(T.Merge([self.Item("city", "Pune", "2026-10-30T10:00:00", cat="identity")]), 0)
        self.assertNotIn("city", self.memory["identity"])

    def TestFullOfferOnConnectAndBack(self):
        self.memory["identity"]["name"] = {"value": "Abhi", "updated": "2026-10-01"}
        T.SetState(tombstones={"notes/old": "2026-10-29T10:00:00"})
        self.t.OnConnected()
        kind, f = self.sent[-1]
        self.assertEqual([k for k, _ in self.sent], ["privacy", "memory_request", "memory_offer"])   # in this order
        self.assertTrue(self.sent[0][1]["follows"])
        self.assertEqual((kind, f["first"]), ("memory_offer", True))
        self.assertEqual([(i["key"], i.get("deleted", False), i["source"]) for i in f["items"]],
                         [("name", False, "orion"), ("old", True, "orion")])
        self.sent.clear()
        self.t.Handle({"type": "memory_offer", "first": True, "items": [self.Item("language", "English", source="jarvis")]})
        self.assertEqual(self.memory["preferences"]["language"]["source"], "jarvis")
        self.assertEqual(self.sent[-1][1]["first"], True)               # a full list from the phone gets Orion's back

    def TestChangesGoOutAndPhoneItemsDontEchoBack(self):
        self.memory["preferences"]["coffee"] = {"value": "black", "updated": "2026-10-29"}
        self.assertEqual(self.t.OfferChanges(), 0)                      # first look: just remembers
        T.Merge([self.Item("address", "boss", source="jarvis")])         # came from the phone
        self.memory["identity"]["city"] = {"value": "Pune", "updated": "2026-10-30"}
        del self.memory["preferences"]["coffee"]
        self.assertEqual(self.t.OfferChanges(), 2)
        items = {i["key"]: i for i in self.sent[-1][1]["items"]}
        self.assertEqual(set(items), {"city", "coffee"})
        self.assertTrue(items["coffee"]["deleted"])
        self.assertEqual(items["city"]["source"], "orion")
        self.assertIn("preferences/coffee", T.State()["tombstones"])
        self.assertEqual(self.t.OfferChanges(), 0)

    def TestSyncNow(self):
        self.assertIn("Syncing memory", self.t.SyncNow())
        self.assertEqual([k for k, _ in self.sent], ["memory_request", "memory_offer"])

    def TestMemoryPageSaysWhere(self):
        from Brain import Memory
        with mock.patch.object(Memory, "LoadMemory", lambda: {"preferences": {
                "a": {"value": "x", "updated": "2026-10-30", "source": "jarvis"}, "b": {"value": "y"}}}):
            rows = {r["key"]: r["source"] for r in Memory.AllEntriesForUi()}
        self.assertEqual(rows, {"a": "jarvis", "b": "orion"})


class TestNoSignalNameTwice(unittest.TestCase):
    def TestEachClassNamesItsSignalsOnce(self):
        """A second `_review_sig = pyqtSignal(list)` in the main window silently replaced the document review's
        signal (2026-10-30): the memory review never opened and document reviews broke. Python keeps the last one."""
        import ast
        src = (Path(__file__).resolve().parent.parent / "Interface" / "Window.py").read_text("utf-8")
        for cls in [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ClassDef)]:
            names = [t.id for st in cls.body if isinstance(st, ast.Assign)
                     and isinstance(st.value, ast.Call) and getattr(st.value.func, "id", "") == "pyqtSignal"
                     for t in st.targets if isinstance(t, ast.Name)]
            self.assertEqual(len(names), len(set(names)), f"{cls.name}: {sorted(n for n in names if names.count(n) > 1)}")


if __name__ == "__main__":
    unittest.main()
