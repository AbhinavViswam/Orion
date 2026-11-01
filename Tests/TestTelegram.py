"""Orion's Telegram bot: the rules (who is obeyed, what is late, yes / no, Telegram's answers), the token kept
encrypted, pairing, requests through a stand-in desk runner, private answers, the built-in commands, confirmations
asked and answered in the chat, and alerts. No real Telegram, model or voice is used."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import Tests.Helpers  # noqa: F401  (paths)
from System import TelegramLink as T
from System import TelegramRules as R

OWNER = 4242
TOKEN = "123456789:" + "A" * 35


def Msg(text, uid=1, frm=OWNER, private=True, bot=False, age=0, photo=None, document=None, now=1_000_000):
    return R.Incoming(update_id=uid, from_id=frm, from_name="Abhi", private=private, bot=bot, text=text,
                      date_s=now - age, photo=photo, document=document)


class TestRules(unittest.TestCase):
    NOW = 1_000_000

    def V(self, m, paired=OWNER, code=None):
        return R.GetVerdict(m, paired, code, self.NOW)

    def TestStrangersAndGroupsGetSilence(self):
        self.assertEqual(self.V(Msg("hi", frm=7)).kind, "ignore")
        self.assertEqual(self.V(Msg("hi", private=False)).kind, "ignore")
        self.assertEqual(self.V(Msg("hi", bot=True)).kind, "ignore")
        self.assertEqual(self.V(Msg("hi"), paired=None).kind, "ignore")

    def TestPairingCode(self):
        self.assertEqual(self.V(Msg("123456", frm=7), paired=None, code="123456").kind, "pair")
        self.assertEqual(self.V(Msg("111111", frm=7), paired=None, code="123456").kind, "wrong_code")
        # Opening the bot sends /start: not a guess.
        self.assertEqual(self.V(Msg("/start", frm=7), paired=None, code="123456").kind, "ignore")

    def TestLateMessagesAreNotRun(self):
        v = self.V(Msg("shut down", age=R.STALE_S + 120))
        self.assertEqual((v.kind, v.minutes), ("stale", 7))
        self.assertEqual(self.V(Msg("shut down", age=R.STALE_S - 1)).kind, "command")

    def TestCommandsPhotosFiles(self):
        self.assertEqual((self.V(Msg("/status@orion_bot")).kind, self.V(Msg("/status@orion_bot")).text),
                         ("builtin", "/status"))
        self.assertEqual(self.V(Msg("/weird")).kind, "command")
        v = self.V(Msg("what is this", photo="f1"))
        self.assertEqual((v.kind, v.text, v.file["id"]), ("photo", "what is this", "f1"))
        self.assertEqual(self.V(Msg(None, document={"id": "d", "name": "a.pdf"})).kind, "document")
        self.assertEqual(self.V(Msg(None)).kind, "not_text")

    def TestLateSummary(self):
        self.assertIn("5 minutes late", R.LateSummary([("x", 5)]))
        many = R.LateSummary([(f"msg {i}", 10 + i) for i in range(12)] + [(None, 3)])
        self.assertIn("13 messages", many)
        self.assertIn("…and 3 more", many)
        self.assertIn("the oldest 21 minutes", many)

    def TestYesNoAndButtons(self):
        self.assertTrue(R.YesNo("Yes!"))
        self.assertFalse(R.YesNo("cancel"))
        self.assertIsNone(R.YesNo("maybe"))
        self.assertIn("yes:7", R.ConfirmButtons(7))
        self.assertEqual(R.TapAnswer("yes:7"), (7, True))
        self.assertEqual(R.TapAnswer("no:12"), (12, False))
        self.assertIsNone(R.TapAnswer("maybe:1"))
        self.assertIsNone(R.TapAnswer("yes:x"))

    def TestChunksAndToken(self):
        parts = R.Chunks(("word " * 2000).strip(), limit=1000)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertEqual(" ".join(parts), ("word " * 2000).strip())
        self.assertTrue(R.LooksLikeToken(TOKEN))
        self.assertFalse(R.LooksLikeToken("hello"))
        self.assertRegex(R.NewCode(), r"^\d{6}$")

    def TestParseUpdates(self):
        body = ('{"ok":true,"result":[{"update_id":5,"message":{"message_id":1,"date":9,"text":"hi",'
                '"from":{"id":4242,"first_name":"Abhi","is_bot":false},"chat":{"id":4242,"type":"private"}}},'
                '{"update_id":6,"callback_query":{"id":"q","data":"yes:1","from":{"id":4242},'
                '"message":{"message_id":3,"chat":{"type":"private"}}}},'
                '{"update_id":7,"message":{"message_id":2,"date":9,"caption":"look",'
                '"photo":[{"file_id":"s","width":90,"height":90},{"file_id":"m","width":1280,"height":720},'
                '{"file_id":"l","width":2560,"height":1440}],"from":{"id":4242},"chat":{"type":"private"}}}]}')
        u = R.ParseUpdates(200, body)
        self.assertEqual((u.kind, u.last_id, len(u.messages), len(u.taps)), ("ok", 7, 2, 1))
        self.assertEqual((u.messages[0].text, u.messages[0].from_name), ("hi", "Abhi"))
        self.assertEqual((u.messages[1].photo, u.messages[1].text), ("m", "look"))
        self.assertEqual(u.taps[0].data, "yes:1")
        self.assertEqual(R.ParseUpdates(401, '{"ok":false,"error_code":401}').kind, "bad_token")
        self.assertEqual(R.ParseUpdates(409, '{"ok":false,"error_code":409}').kind, "elsewhere")
        slow = R.ParseUpdates(429, '{"ok":false,"error_code":429,"parameters":{"retry_after":9}}')
        self.assertEqual((slow.kind, slow.seconds), ("slow", 9))
        self.assertEqual(R.ParseUpdates(500, "not json").kind, "failed")


class FakeApi:
    def __init__(self):
        self.sent, self.edits, self.taps, self.photos = [], [], [], []
        self.n = 100

    def BotName(self, token):
        return "orion_test_bot" if token == TOKEN else None

    def SetCommands(self, token):
        return True

    def Send(self, token, chat, text, buttons=None):
        self.n += 1
        self.sent.append((chat, text, buttons))
        return self.n

    def Edit(self, token, chat, mid, text):
        self.edits.append((mid, text))
        return True

    def AnswerTap(self, token, qid, text=None):
        self.taps.append(text)

    def Typing(self, token, chat):
        pass

    def SendPhoto(self, token, chat, image, caption="", name="", mime=""):
        self.photos.append(caption)
        return True

    def Download(self, token, fid):
        return b"\xff\xd8 fake jpeg"


class FakeTasks:
    def __init__(self):
        self.asked, self.reply = [], ("completed", "Three flights, cheapest ₹4,200.")
        self.controls = []

    def Ask(self, task, system=None, history=None, mark_private=False):
        self.asked.append((task, system, list(history or []), mark_private))
        return self.reply

    def Control(self, cmd):
        self.controls.append(cmd)
        return {"mute": "Orion is muted.", "undo": "Undid it."}.get(cmd, "ok")

    def Lookup(self, query):
        return "Messages from calls: Mum, call back."

    @staticmethod
    def _Local():
        return False


class LinkCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        store = Path(self.dir.name) / "Telegram.json"
        self.settings = {}
        self.private = False
        for p in (mock.patch.object(T, "_Store", lambda: store),
                  mock.patch.object(T, "_InboxDir", lambda: Path(self.dir.name) / "inbox"),
                  mock.patch.object(T, "Enabled", lambda: True),
                  mock.patch.object(T, "Setting", lambda k, d=False: self.settings.get(k, d)),
                  mock.patch("Brain.Privacy.Active", lambda: self.private)):
            p.start()
            self.addCleanup(p.stop)
        self.link = T.Link()
        self.api = self.link.api = FakeApi()
        self.tasks = self.link.tasks = FakeTasks()
        self.logs = []
        self.link.live = SimpleNamespace(ui=SimpleNamespace(muted=False, WriteLog=self.logs.append),
                                         session=object(), _is_speaking=False,
                                         RequestReconnect=lambda **k: None)

    def Flush(self):
        """Sends everything waiting, the way the sender thread does."""
        while not self.link._outbox.empty():
            item = self.link._outbox.get()
            self.link._SendOne(TOKEN, OWNER, item)

    def Texts(self):
        return [t for _, t, _ in self.api.sent]

    def Pair(self):
        ok, msg = self.link.SaveToken(TOKEN)
        self.assertTrue(ok, msg)
        self.link.HandleAll([Msg(self.link.code, frm=OWNER)], now=1_000_000)
        self.Flush()

    def RunTurns(self):
        while not self.link._turns.empty():
            self.link.Turn(*self.link._turns.get())
        self.Flush()


class TestPairingAndStore(LinkCase):
    def TestTokenIsKeptEncrypted(self):
        self.assertFalse(self.link.SaveToken("nope")[0])
        self.assertFalse(self.link.SaveToken("987654321:" + "B" * 35)[0])     # Telegram refuses it
        ok, msg = self.link.SaveToken(TOKEN)
        self.assertTrue(ok)
        self.assertIn(self.link.code, msg)
        raw = T._Store().read_text("utf-8")
        self.assertNotIn(TOKEN, raw)
        self.assertEqual(T.Token(), TOKEN)

    def TestPairThenStrangersIgnored(self):
        self.Pair()
        d = T.Load()
        self.assertEqual((d["owner"], d["owner_name"]), (OWNER, "Abhi"))
        self.assertIsNone(self.link.Code())
        self.assertIn("Paired", self.Texts()[-1])
        n = len(self.api.sent)
        self.link.HandleAll([Msg("do things", frm=99, now=1_000_000)], now=1_000_000)
        self.Flush()
        self.assertEqual(len(self.api.sent), n)
        self.assertTrue(self.link._turns.empty())

    def TestWrongCodesWithdrawTheCode(self):
        self.link.SaveToken(TOKEN)
        for i in range(R.MAX_WRONG_CODES):
            self.link.HandleAll([Msg(f"00000{i}", frm=99)], now=1_000_000)
        self.assertIsNone(self.link.Code())

    def TestButtonFollowsState(self):
        self.assertEqual(self.link.ButtonLabel(), "Save token")
        self.link.SaveToken(TOKEN)
        self.assertEqual(self.link.ButtonLabel(), "Cancel code")
        self.link.CancelPairing()
        self.assertEqual(self.link.ButtonLabel(), "Pair")
        self.assertTrue(self.link.Act({})[0])          # Pair → a code
        self.assertTrue(self.link.Code())
        self.link.HandleAll([Msg(self.link.code)], now=1_000_000)
        self.assertEqual(self.link.ButtonLabel(), "Unpair")
        self.link.Act({})
        self.assertIsNone(T.Load().get("owner"))


class TestRequests(LinkCase):
    def TestRequestGoesToTheDeskRunnerWithHistory(self):
        self.Pair()
        self.link.HandleAll([Msg("find flights to Delhi", uid=2)], now=1_000_000)
        self.RunTurns()
        task, system, history, mark = self.tasks.asked[0]
        self.assertEqual((task["text"], task["where"], system, history, mark),
                         ("find flights to Delhi", "Telegram", T.SYSTEM, [], True))
        self.assertIn("cheapest", self.Texts()[-1])
        self.link.HandleAll([Msg("the cheapest one", uid=3)], now=1_000_000)
        self.RunTurns()
        self.assertEqual(self.tasks.asked[1][2], [("find flights to Delhi", "Three flights, cheapest ₹4,200.")])
        self.link.Builtin("/new")
        self.assertEqual(self.link.history, [])

    def TestLateMessagesGetOneReply(self):
        self.Pair()
        self.link.HandleAll([Msg("a", uid=2, age=900), Msg("b", uid=3, age=800), Msg("now", uid=4)],
                            now=1_000_000)
        self.Flush()
        self.assertIn("2 messages reached the laptop late", self.Texts()[-1])
        self.assertEqual(self.link._turns.qsize(), 1)

    def TestPrivateAnswersStayOnTheLaptop(self):
        self.Pair()
        self.tasks.reply = ("private", "Mum: are you coming?")
        self.link.HandleAll([Msg("read my messages", uid=2)], now=1_000_000)
        self.RunTurns()
        self.assertEqual(self.Texts()[-1], T.PRIVATE_KEPT)
        self.assertNotIn("Mum", str(self.link.history))
        self.settings["details"] = True
        self.link.HandleAll([Msg("read my messages", uid=3)], now=1_000_000)
        self.RunTurns()
        self.assertEqual(self.Texts()[-1], "Mum: are you coming?")

    def TestFileWithoutCaptionIsJustSaved(self):
        self.Pair()
        self.link.HandleAll([Msg(None, uid=2, document={"id": "d1", "name": "notes.txt", "size": 9})],
                            now=1_000_000)
        self.RunTurns()
        self.assertEqual(self.tasks.asked, [])
        self.assertIn("Saved it on the laptop", self.Texts()[-1])
        self.assertTrue((Path(self.dir.name) / "inbox" / "notes.txt").is_file())
        self.link.HandleAll([Msg("what's in this?", uid=3, photo="p1")], now=1_000_000)
        self.RunTurns()
        self.assertEqual(len(self.tasks.asked[0][0]["files"]), 1)

    def TestBuiltins(self):
        self.Pair()
        self.link.Builtin("/status")
        self.link.Builtin("/mute")
        self.link.Builtin("/undo")
        self.link.Builtin("/calls")
        self.Flush()
        texts = self.Texts()
        self.assertIn("Gemini", texts[-4])
        self.assertEqual(texts[-3:], ["Orion is muted.", "Undid it.", "Messages from calls: Mum, call back."])
        self.assertEqual(self.tasks.controls, ["mute", "undo"])
        self.assertEqual(self.tasks.asked, [])             # none of them used the model
        self.private = True
        self.link.Builtin("/calls")
        self.link.Builtin("/screenshot")
        self.Flush()
        self.assertEqual(self.Texts()[-2], T.PRIVATE_KEPT)
        self.assertIn("Private mode is on", self.Texts()[-1])
        self.assertEqual(self.api.photos, [])


class TestConfirmations(LinkCase):
    def setUp(self):
        super().setUp()
        from Brain import Confirm
        self.gate = Confirm
        self.banner = []
        Confirm.bind(lambda t, d: self.banner.append(t), lambda: self.banner.append("hidden"), log=lambda m: None)
        self.addCleanup(Confirm.bind, None, None)
        self.addCleanup(lambda: Confirm.resolve(False))
        self.link.WrapConfirm(Confirm)
        self.link.WrapConfirm(Confirm)                       # twice: still one question in the chat
        self.Pair()

    def Ask(self):
        done = threading.Event()
        self.link.turn_running = True
        self.gate.request("shutdown", "Shut down the laptop", "In 30 seconds", lambda: (done.set(), "Shutting down.")[1])
        self.link.turn_running = False
        self.Flush()
        return done

    def TestTapYes(self):
        done = self.Ask()
        mid, question = self.api.n, self.Texts()[-1]
        self.assertIn("Confirm: Shut down the laptop", question)
        self.assertEqual(sum("Confirm:" in t for t in self.Texts()), 1)
        pid = self.link.asking["pid"]
        self.link.HandleTap(R.Tap("q", 99, True, mid, f"yes:{pid}"))      # a stranger's tap: silence
        self.assertFalse(done.is_set())
        self.link.HandleTap(R.Tap("q", OWNER, True, mid - 1, f"yes:{pid}"))  # an old message's button
        self.assertEqual(self.api.taps[-1], "That's no longer waiting.")
        self.link.HandleTap(R.Tap("q", OWNER, True, mid, f"yes:{pid}"))
        self.assertTrue(done.wait(2))
        time.sleep(0.1)
        self.Flush()
        self.assertEqual(self.api.taps[-1], "Doing it")
        self.assertTrue(self.api.edits and self.api.edits[-1][1].endswith("Shutting down."))
        self.assertIsNone(self.link.asking)

    def TestReplyNoAndWaiting(self):
        done = self.Ask()
        self.link.HandleAll([Msg("open spotify", uid=5)], now=1_000_000)
        self.Flush()
        self.assertIn("I'm waiting on", self.Texts()[-1])
        self.assertTrue(self.link._turns.empty())
        self.link.HandleAll([Msg("no", uid=6)], now=1_000_000)
        self.Flush()
        self.assertFalse(done.is_set())
        self.assertIn("✗ No", self.api.edits[-1][1])

    def TestLaptopQuestionsStayOnTheLaptop(self):
        n = len(self.api.sent)
        self.gate.request("shutdown", "Shut down", "", lambda: "ok")      # no Telegram request running
        self.Flush()
        self.assertEqual(len(self.api.sent), n)


class TestAlerts(LinkCase):
    def TestOffUntilTurnedOn(self):
        self.Pair()
        n = len(self.api.sent)
        with mock.patch.object(T, "Api", self.api):
            self.assertFalse(T.AlertNow("reminder", "Reminder", "Call the dentist"))
            self.settings["alert_reminders"] = True
            self.assertTrue(T.AlertNow("reminder", "Reminder", "Call the dentist"))
        self.assertEqual(self.Texts()[n:], ["⏰ Reminder: Call the dentist"])
        self.assertEqual(T.AlertWords("work", "", "the dev agent"), "✓ Orion finished the dev agent.")


class TestTokenProtection(unittest.TestCase):
    def TestRoundTrip(self):
        stored = T._Protect(TOKEN)
        self.assertNotIn(TOKEN, stored)
        self.assertEqual(T._Unprotect(stored), TOKEN)
        self.assertIsNone(T._Unprotect("dpapi:bm90IGEgYmxvYg=="))


if __name__ == "__main__":
    unittest.main()
