"""Each settings page: it saves what it should, asks for a reconnect when it
should, and never crashes when a page closes while work is still running."""
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


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        App()
        from Interface import Window
        cls.W = Window


class TestSafeTimersAndRelay(_Base):
    def TestLaterFiresWhileTheOwnerLives(self):
        from PyQt6.QtCore import QObject
        owner, hits = QObject(), []
        self.W._Later(5, owner, lambda: hits.append(1))
        Pump(80)
        self.assertEqual(hits, [1])

    def TestLaterIsDroppedWithItsOwner(self):
        from PyQt6.QtCore import QObject
        owner, hits = QObject(), []
        self.W._Later(30, owner, lambda: hits.append(1))
        from PyQt6 import sip
        sip.delete(owner)                    # what closing a page does to it
        Pump(120)
        self.assertEqual(hits, [])

    def TestALateResultAfterThePageClosedIsHarmless(self):
        page = self.W.PluginsPage([], [])
        from PyQt6 import sip
        sip.delete(page)
        Pump(20)
        relay = self.W._Relay()
        relay.plugin_test.emit("gone", True, "ok")
        relay.test_result.emit("late", True)
        relay.level_seen.emit(50)
        relay.status.emit("gone", "late", "#ffffff")
        relay.live_ready.emit()
        Pump(20)                             # reaching here is the test


class TestMemoryPage(_Base):
    def setUp(self):
        import Brain.Memory as M
        self.M = M
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = M.MEMORY_PATH
        M.MEMORY_PATH = self.tmp / "LongTerm.json"
        M.remember("favourite_coffee", "flat white", "preferences")
        M.remember("sister", "Dilsha", "relationships")
        self.page = self.W.MemoryPage("ORION")
        # Shown (off screen) so rows added by a refresh really appear.
        from PyQt6.QtCore import Qt
        self.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.page.show()
        Pump(20)

    def tearDown(self):
        self.page.deleteLater()
        Pump(10)
        self.M.MEMORY_PATH = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def Keys(self):
        return {r["key"] for r in self.M.AllEntriesForUi()}

    def Buttons(self, text):
        return [b for b in self.page.findChildren(self.W.QPushButton)
                if b.text() == text and not b.isHidden()]

    def TestItCountsWhatItKnows(self):
        self.assertEqual(self.page._count.text(), "2 things remembered")

    def TestForgetAsksFirst(self):
        btn = self.Buttons("Forget")[0]
        btn.click()
        self.assertEqual(btn.text(), "Sure?")
        self.assertEqual(len(self.Keys()), 2)        # nothing gone yet
        btn.click()
        Pump(30)
        self.assertEqual(len(self.Keys()), 1)

    def TestTeachingItSomething(self):
        self.page.ShowAdd()
        self.page._add_key.setText("Favourite Film")
        self.page._add_val.setText("Interstellar")
        self.page.AddEntry()
        self.assertIn("favourite_film", self.Keys())
        self.assertTrue(self.page._add_card.isHidden())

    def TestAnEmptyEntryIsNotSaved(self):
        self.page.ShowAdd()
        self.page._add_key.setText("thing")
        self.page.AddEntry()
        self.assertNotIn("thing", self.Keys())

    def TestSearchAndFilter(self):
        def Titles():
            return [l.text() for l in self.page.findChildren(self.W.QLabel)
                    if l.text() in ("Favourite coffee", "Sister") and l.isVisible()]
        self.page._search.setText("dilsha")          # typed into the box
        Pump(30)
        self.assertEqual(Titles(), ["Sister"])
        self.page._search.clear()
        self.page.SetFilter("preferences")
        Pump(30)
        self.assertEqual(Titles(), ["Favourite coffee"])


class TestPluginsPage(_Base):
    SECTION = {"namespace": "printer", "title": "3D printer", "values": {"host": "octopi"},
               "fields": [{"key": "host", "label": "Address"},
                          {"key": "key", "label": "Key", "type": "password"},
                          {"key": "speed", "label": "Speed", "type": "choice", "options": ["slow", "fast"]},
                          {"key": "notify", "label": "Notify", "type": "toggle", "default": True}]}

    def TestSwitchSavesAndAsksForReconnect(self):
        plugins = [{"name": "spotify", "description": "Music", "file": "Spotify.py",
                    "valid": True, "error": "", "enabled": False}]
        store, asked = {}, []
        with mock.patch("Brain.Settings.GetPluginEnabled", lambda n: store.get(n, False)), \
             mock.patch("Brain.Settings.SavePluginEnabled", lambda n, v: store.__setitem__(n, v)):
            page = self.W.PluginsPage(plugins, [])
            page.changed.connect(lambda: asked.append(1))
            page.findChildren(self.W.ToggleSwitch)[0].click()
        self.assertEqual(store, {"spotify": True})
        self.assertEqual(asked, [1])

    def TestABrokenPluginHasNoSwitch(self):
        plugins = [{"name": "old", "description": "", "file": "Old.py", "valid": False,
                    "error": "no TOOL", "enabled": False}]
        page = self.W.PluginsPage(plugins, [])
        self.assertEqual(page.findChildren(self.W.ToggleSwitch), [])

    def TestSettingsGatherEveryFieldType(self):
        page = self.W.PluginsPage([], [self.SECTION])
        self.assertEqual(page.Gather("printer"),
                         {"host": "octopi", "key": "", "speed": "slow", "notify": True})
        saved = {}
        with mock.patch("Brain.Settings.SavePluginConfig", lambda ns, v: saved.update({ns: v})):
            page.SaveNs("printer", say=True)
        self.assertEqual(saved["printer"]["host"], "octopi")


class _FakeWindow:
    def __init__(self):
        self.accents, self.names = [], []

    def SetAccent(self, key):
        self.accents.append(key)

    def ApplyNameUpdate(self, name, user, ui_color="", voice=""):
        self.names.append((name, user))


class TestLookPage(_Base):
    def setUp(self):
        self.win = _FakeWindow()
        self.page = self.W.LookPage(self.win)

    def TestEveryPhoneColourIsOffered(self):
        from Interface import Accents
        self.assertEqual(set(self.page._swatches), {a.key for a in Accents.ALL})
        self.assertEqual([len(Accents.InGroup(g)) for g in Accents.GROUPS], [10, 5, 5])

    def TestTappingASwatchPicksIt(self):
        sw = self.page._swatches["GOLD"]
        sw.clicked.emit()
        self.assertEqual(self.win.accents, ["GOLD"])
        self.assertTrue(sw.isChecked())
        self.assertEqual(sum(s.isChecked() for s in self.page._swatches.values()), 1)

    def TestSwatchesPaint(self):
        for sw in self.page._swatches.values():
            sw.grab()

    def TestNamesSaveOnlyWhenChanged(self):
        self.page.SaveNames()
        self.assertEqual(self.win.names, [])
        self.page._user.setText("Abhinav")
        self.page.SaveNames()
        self.assertEqual(self.win.names[-1][1], "Abhinav")


class TestAccents(unittest.TestCase):
    def TestOldNamesAndHexesStillLoad(self):
        from Interface import Accents
        self.assertEqual(Accents.Of("gold").key, "GOLD")
        self.assertEqual(Accents.Of("RED_GOLD").key, "MARK")        # retired on the phone
        self.assertEqual(Accents.Of(None, "#ff3b4e").key, "RED")    # an old hex that matches
        self.assertEqual(Accents.Of(None, "#00d4ff").key, "BLUE")   # the old wheel's cyan: nearest swatch
        self.assertEqual(Accents.Of(None, "#dcefff").key, "GOLD")    # no colour of its own: the default
        self.assertEqual(Accents.Of("nonsense").key, Accents.DEFAULT)
        self.assertEqual(Accents.DEFAULT, "GOLD")
        self.assertEqual(Accents.Of(None).key, "GOLD")             # nothing chosen yet

    def TestLivingColoursMove(self):
        from Interface import Accents
        for key in ("PRISM", "ARC_REACTOR", "MARK", "DAYLIGHT"):
            a = Accents.Of(key)
            seen = {Accents.At(a, t)[0] for t in range(0, 6 * 3600, 137)}
            self.assertGreater(len(seen), 3, key)
            for hx in seen:
                self.assertRegex(hx, r"^#[0-9a-f]{6}$")

    def TestGradientsHaveTwoColours(self):
        from Interface import Accents
        for a in Accents.InGroup(Accents.GRADIENT):
            main, second = Accents.At(a)
            self.assertNotEqual(main, second, a.key)

    def TestAColourShowsAtFullStrength(self):
        from Interface import Window as W
        saved = W.CurrentPalette()
        try:
            W.ApplyUiAccent("#ff3b4e")
            self.assertEqual(W.C.PRI, "#ff3b4e")                     # not a pale pink
            W.ApplyUiAccent(W.DEFAULT_UI_COLOR)
            self.assertEqual(W.CurrentPalette(), W._PALETTE_DEFAULTS)  # white is untouched
        finally:
            for k, v in saved.items():
                setattr(W.C, k, v)


class TestVoicePage(_Base):
    def MakePage(self, store):
        patches = [
            mock.patch("Voice.AudioDevices.ListDevices", lambda kind, refresh=False: ["Headset"]),
            mock.patch("Voice.AudioDevices.GetOutputVolume", lambda name="": 40),
            mock.patch("Brain.Settings.GetInputDevice", lambda: store.get("in", "")),
            mock.patch("Brain.Settings.GetOutputDevice", lambda: store.get("out", "")),
            mock.patch("Brain.Settings.SaveInputDevice", lambda v: store.__setitem__("in", v)),
            mock.patch("Brain.Settings.SaveOutputDevice", lambda v: store.__setitem__("out", v)),
            mock.patch("Brain.Settings.GetVoice", lambda: store.get("voice", "Charon")),
            mock.patch("Brain.Settings.SaveVoice", lambda v: store.__setitem__("voice", v)),
            mock.patch("Brain.Settings.GetProviderMode", lambda: "gemini"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return self.W.VoicePage("ORION")

    def TestPickingADeviceSavesIt(self):
        store, asked = {}, []
        page = self.MakePage(store)
        page.devices_changed.connect(lambda: asked.append(1))
        page._in_box.setCurrentIndex(page._in_box.findData("Headset"))
        self.assertEqual(store.get("in"), "Headset")
        self.assertEqual(asked, [1])

    def TestPickingAVoiceSavesIt(self):
        store, asked = {}, []
        page = self.MakePage(store)
        page.voice_changed.connect(lambda: asked.append(1))
        page._voice_cards["Puck"].clicked.emit()
        page._voice_cards["Puck"].clicked.emit()       # same voice again: no second reconnect
        self.assertEqual(store["voice"], "Puck")
        self.assertEqual(asked, [1])
        self.assertTrue(page._voice_cards["Puck"].isChecked())
        self.assertFalse(page._voice_cards["Charon"].isChecked())


class TestBrainPage(_Base):
    def TestHostedPanelHasNoScrollOrClose(self):
        ov = self.W.ProvidersOverlay(hosted=True)
        self.assertFalse(hasattr(ov, "_scroll"))
        texts = [b.text() for b in ov.findChildren(self.W.QPushButton)]
        self.assertIn("Save && apply", texts)          # && shows as one "&"
        self.assertNotIn("Close", texts)

    def TestModeCardsWrapTheirText(self):
        ov = self.W.ProvidersOverlay(hosted=True)
        self.assertIsInstance(ov._card_gemini, self.W._ChoiceCard)
        self.assertTrue(ov._card_gemini._body.wordWrap())

    def TestLiveOptionsSaveAndAskForReconnect(self):
        calls, asked = [], []
        with mock.patch("Brain.Settings.GetProviderMode", lambda: "gemini"), \
             mock.patch("Brain.Settings.SaveThinkingEnabled", lambda v: calls.append(("think", v))), \
             mock.patch("Brain.Settings.SaveMediaResolution", lambda v: calls.append(("media", v))):
            ov = self.W.ProvidersOverlay(hosted=True)
            ov.live_tuned.connect(lambda: asked.append(1))
            ov.SetLive("thinking", True)
            ov.SetLive("media", "high")
        self.assertEqual(calls, [("think", True), ("media", "high")])
        self.assertEqual(asked, [1, 1])


class TestReconnectIsBatched(_Base):
    def MakeOwner(self):
        from PyQt6.QtCore import QObject
        W = self.W

        class Owner(QObject):
            QueueReconnect = W.MainWindow.QueueReconnect
            _DoReconnect = W.MainWindow._DoReconnect

        o = Owner()
        o.reasons, o.voices = [], []
        o.on_audio_device_change = lambda reason="": o.reasons.append(reason)
        o.on_voice_change = lambda: o.voices.append(1)
        return o

    def TestQuickChangesBecomeOneReconnect(self):
        o = self.MakeOwner()
        o.QueueReconnect("audio device")
        o.QueueReconnect("Gemini Live options")
        Pump(1200)
        self.assertEqual(o.reasons, ["Gemini Live options"])

    def TestANewVoiceStartsFresh(self):
        o = self.MakeOwner()
        o.QueueReconnect("audio device")
        o.QueueReconnect("new voice", fresh=True)
        Pump(1200)
        self.assertEqual((o.voices, o.reasons), ([1], []))


if __name__ == "__main__":
    unittest.main()
