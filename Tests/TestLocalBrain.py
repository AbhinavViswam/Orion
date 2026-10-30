"""The laptop's own brain (System/LocalBrain.py, ORION_LOCAL_BRAIN_PLAN.md P1): brain modes, the
"limited tool access" check, the short tool set and prompt, Auto's switch both ways, the one-shot
ladder in each mode, and the Brain page. No Ollama, model or Gemini is used."""
import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from Brain import Gemini, Settings
from Engines.LlmOpenai import OpenAICompatLLM
from System import LocalBrain


def Providers(**block):
    return mock.patch.object(Settings, "GetProviders", lambda: dict(block))


class TestModes(unittest.TestCase):
    def TestDefaultIsAuto(self):
        with Providers():
            self.assertEqual(Settings.GetBrainMode(), "auto")
            self.assertEqual(LocalBrain.Mode(), "auto")
        with Providers(brain="nonsense"):
            self.assertEqual(Settings.GetBrainMode(), "auto")

    def TestMyOwnSetupWins(self):
        with Providers(mode="pipeline", brain="local"):
            self.assertEqual(LocalBrain.Mode(), "pipeline")
            self.assertFalse(LocalBrain.UseLocal(True))

    def TestWhenLocalAnswers(self):
        with Providers(brain="local"):
            self.assertTrue(LocalBrain.UseLocal(False))
        with Providers(brain="auto"):
            self.assertFalse(LocalBrain.UseLocal(False))
            self.assertTrue(LocalBrain.UseLocal(True))
        with Providers(brain="gemini"):
            self.assertFalse(LocalBrain.UseLocal(True))

    def TestSaveProvidersKeepsBrain(self):
        Settings.SaveProviders(mode="gemini", brain="local")
        self.assertEqual(Settings.GetBrainMode(), "local")
        Settings.SaveProviders(mode="pipeline")          # brain left alone
        self.assertEqual(Settings.GetBrainMode(), "local")
        self.assertEqual(Settings.GetProviderMode(), "pipeline")
        Settings.SaveProviders(mode="gemini", brain="auto")


class TestLaptopVoice(unittest.TestCase):
    def Voice(self, picked="", own="", gemini="Charon"):
        with mock.patch.object(Settings, "GetLocalVoice", lambda: picked), \
             mock.patch.object(Settings, "GetProviderConfig", lambda pid: {"voice": own}), \
             mock.patch.object(Settings, "GetVoice", lambda: gemini):
            return LocalBrain.LocalVoice()

    def TestNearestToTheGeminiVoice(self):
        self.assertEqual(self.Voice(), "en_US-ryan-high")
        self.assertEqual(self.Voice(gemini="Kore"), "en_US-lessac-high")

    def TestAPickedVoiceWins(self):
        self.assertEqual(self.Voice(picked="en_GB-alan-medium", own="en_US-joe-medium"), "en_GB-alan-medium")
        self.assertEqual(self.Voice(own="en_US-joe-medium"), "en_US-joe-medium")

    def TestEveryOfferedVoiceIsAPiperName(self):
        names = [v for v, _n, _d in LocalBrain.LOCAL_VOICES]
        for v in list(LocalBrain.LIKE_GEMINI.values()) + names:
            self.assertRegex(v, r"^en_(US|GB)-[a-z_]+-(low|medium|high)$")
        for v in LocalBrain.LIKE_GEMINI.values():
            self.assertIn(v, names)                   # the automatic one is in the list too


class TestLimited(unittest.TestCase):
    def TestCantDo(self):
        for said in ("what's on my screen", "look at this", "find me flights to Dubai",
                     "write a program that sorts files", "send a whatsapp to mom",
                     "summarize this pdf", "open the camera"):
            self.assertEqual(LocalBrain.LimitedReply(said), LocalBrain.LIMITED, said)

    def TestCanDo(self):
        for said in ("make the screen brighter", "dim the screen", "open spotify", "what is the capital of France",
                     "remind me to call mom at 6", "how much battery does my phone have", ""):
            self.assertIsNone(LocalBrain.LimitedReply(said), said)


class TestToolsAndPrompt(unittest.TestCase):
    DECLS = [
        {"name": "browser_control", "description": "x" * 900, "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "reminder", "description": "Long. " * 80, "brief": "b", "tier": "core",
         "parameters": {"type": "OBJECT", "properties": {
             "time": {"type": "STRING", "description": "HH:MM. Then a very long explanation " + "y" * 300}}}},
        {"name": "open_app", "description": "z" * 1400, "parameters": {"type": "OBJECT", "properties": {
            "app_name": {"type": "STRING", "description": "The app."}}}},
    ]

    def TestShortTools(self):
        short = LocalBrain.ShortTools(self.DECLS)
        self.assertEqual([d["name"] for d in short], ["open_app", "reminder"])     # TOOLS order, browser gone
        self.assertEqual(short[1]["description"], LocalBrain.TOOLS["reminder"])
        self.assertNotIn("brief", short[1])
        self.assertNotIn("tier", short[1])
        self.assertEqual(short[1]["parameters"]["properties"]["time"]["description"], "HH:MM.")
        self.assertEqual(self.DECLS[1]["parameters"]["properties"]["time"]["description"][:6], "HH:MM.")  # untouched

    def TestEveryShortToolExists(self):
        from pathlib import Path
        import ast
        import re
        names = set()
        for folder in ("Actions", "Plugins"):
            for f in (Path(__file__).resolve().parent.parent / folder).glob("*.py"):
                names |= set(re.findall(r'"name":\s*"([a-z_]+)"', f.read_text(encoding="utf-8")))
        main = (Path(__file__).resolve().parent.parent / "Main.py").read_text(encoding="utf-8")
        names |= set(re.findall(r'"name":\s*"([a-z_]+)"', main))
        missing = [n for n in LocalBrain.TOOLS if n not in names]
        self.assertEqual(missing, [])

    def TestPrompt(self):
        memory = {"identity": {"name": {"value": "Abhi", "updated": "2026-10-01"}},
                  "preferences": {f"p{i}": {"value": f"v{i}", "updated": f"2026-10-{10 + i}"} for i in range(9)},
                  "rules": {"r1": {"value": "Keep answers short", "at": 5}}}
        text = LocalBrain.Prompt("Orion", "Maker", "", memory, now=datetime(2026, 10, 30, 13, 5))
        self.assertIn(LocalBrain.LIMITED, text)
        self.assertIn("from your own knowledge", text)
        self.assertIn("name: Abhi", text)                    # who they are first
        self.assertIn("p8: v8", text)                        # then the newest
        self.assertNotIn("p0: v0", text)                     # only 6 facts
        self.assertIn("Keep answers short", text)
        self.assertTrue(text.endswith("It is Friday, 30 October 2026, 1:05 PM."))   # changing part last
        self.assertLess(len(text), 2500)


class TestLlmBody(unittest.TestCase):
    def TestExtraFields(self):
        llm = OpenAICompatLLM(LocalBrain.LlmConfig())
        body = llm._body([{"role": "user", "content": "hi"}], None, True, None)
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertEqual(body["max_tokens"], 200)
        self.assertEqual(body["model"], LocalBrain.MODEL)
        self.assertFalse(llm.caps["vision"])
        self.assertTrue(llm.ensure_vision_known() is False)     # stored, not measured

    def TestPlainProviderUnchanged(self):
        llm = OpenAICompatLLM({"base_url": "http://127.0.0.1:11434/v1", "model": "m"})
        body = llm._body([], None, False, None)
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("reasoning_effort", body)


class TestLadder(unittest.TestCase):
    def Run(self, mode, installed=True):
        tried = []

        def Live(*a, **k):
            tried.append("live")
            raise RuntimeError("503 UNAVAILABLE")

        def Prov(model, *a, **k):
            tried.append(model)
            return "local answer"

        class FakeModels:
            def generate_content(self, model, **k):
                tried.append(model)
                raise RuntimeError("503 UNAVAILABLE")

        with mock.patch.object(Gemini, "BrainMode", lambda: mode), \
                mock.patch.object(LocalBrain, "Installed", lambda refresh=False: installed), \
                mock.patch.object(Gemini, "LiveCall", Live), \
                mock.patch.object(Gemini, "ProviderCall", Prov), \
                mock.patch.object(Gemini, "Cooling", lambda m: False), \
                mock.patch.object(Gemini, "Cool", lambda *a: None), \
                mock.patch.object(Gemini, "api_key", lambda: "k"), \
                mock.patch.object(Gemini, "client", lambda **k: SimpleNamespace(models=FakeModels())), \
                mock.patch.object(Gemini, "_provider_rungs", lambda tier: ()):
            out = Gemini.call("hello", tier=Gemini.FAST)
        return out, tried

    def TestOnDeviceNeverAsksGemini(self):
        out, tried = self.Run("local")
        self.assertEqual(out, "local answer")
        self.assertEqual(tried, [Gemini.LOCAL_RUNG])

    def TestAutoUsesLocalLast(self):
        out, tried = self.Run("auto")
        self.assertEqual(out, "local answer")
        self.assertEqual(tried[0], "live")
        self.assertEqual(tried[-1], Gemini.LOCAL_RUNG)

    def TestAutoWithoutDownload(self):
        out, tried = self.Run("auto", installed=False)
        self.assertIsNone(out)
        self.assertNotIn(Gemini.LOCAL_RUNG, tried)

    def TestGeminiOnly(self):
        out, tried = self.Run("gemini")
        self.assertNotIn(Gemini.LOCAL_RUNG, tried)

    def TestOnDeviceSearchIsNotDone(self):
        with mock.patch.object(Gemini, "BrainMode", lambda: "local"):
            self.assertIsNone(Gemini.call("news", tier=Gemini.SEARCH))


class TestAutoSwitch(unittest.TestCase):
    def Live(self):
        import Main
        logs = []
        live = SimpleNamespace(_local_fallback=False, _local_hello=False, _back_wait=60.0, _back_at=0.0,
                               _conn_backoff=3, ui=SimpleNamespace(WriteLog=logs.append),
                               EngineMode=lambda: "gemini")
        live.FallBackToLocal = lambda why: Main.OrionLive.FallBackToLocal(live, why)
        return live, logs

    def TestFallsBackOnceInAuto(self):
        live, logs = self.Live()
        with mock.patch.object(LocalBrain, "CanFallBack", lambda: True):
            self.assertTrue(live.FallBackToLocal("Gemini can't be reached"))
            self.assertFalse(live.FallBackToLocal("again"))          # already on it
        self.assertTrue(live._local_fallback and live._local_hello)
        self.assertEqual(live._conn_backoff, 0)
        self.assertIn("laptop's own brain", logs[0])

    def TestNoFallbackWhenNotPossible(self):
        live, logs = self.Live()
        with mock.patch.object(LocalBrain, "CanFallBack", lambda: False):
            self.assertFalse(live.FallBackToLocal("x"))
        self.assertFalse(live._local_fallback)

    def TestBacksOffAfterQuickFailure(self):
        import time
        live, _ = self.Live()
        live._back_at = time.monotonic() - 30           # came back to Gemini 30 s ago and it failed again
        with mock.patch.object(LocalBrain, "CanFallBack", lambda: True):
            live.FallBackToLocal("x")
        self.assertEqual(live._back_wait, 120.0)

    def TestLocalFastSaysLimited(self):
        import Main

        async def NoFast(text, live=False):
            return None

        live = SimpleNamespace(RunFastCommand=NoFast)
        said = asyncio.run(Main.OrionLive.LocalFast(live, "what's on my screen"))
        self.assertEqual(said, LocalBrain.LIMITED)
        self.assertIsNone(asyncio.run(Main.OrionLive.LocalFast(live, "tell me a joke")))

    def TestPhoneAsksSkipTheModel(self):
        import Main

        async def NoFast(text, live=False):
            return None

        ran = []

        async def Execute(fc):
            ran.append((fc.name, dict(fc.args)))
            return Main.types.FunctionResponse(id=fc.id, name=fc.name, response={"result": "77%, charging"})

        live = SimpleNamespace(RunFastCommand=NoFast, ExecuteTool=Execute, _fast_n=0)
        said = asyncio.run(Main.OrionLive.LocalFast(live, "how much battery is on my phone's battery"))
        self.assertEqual(ran, [("ask_phone", {"action": "battery"})])
        self.assertEqual(said, "Your phone is at 77%, charging.")

    def TestUsingLocalClearsStaleFallback(self):
        import Main
        live = SimpleNamespace(_local_fallback=True, EngineMode=lambda: "gemini")
        with mock.patch.object(LocalBrain, "Mode", lambda: "gemini"):
            self.assertFalse(Main.OrionLive.UsingLocal(live))
        self.assertFalse(live._local_fallback)


class TestPhoneRules(unittest.TestCase):
    def TestMatches(self):
        cases = {"where is my phone": "where", "where did I leave my phone": "where", "find my phone": "where",
                 "ring my phone": "ring", "my phone battery": "battery", "battery on my phone": "battery",
                 "how much battery does my phone have": "battery"}
        for said, action in cases.items():
            self.assertEqual(LocalBrain.PhoneRule(said), {"action": action}, said)
        for said in ("what is my laptop battery at", "open spotify", "", "call mom"):
            self.assertIsNone(LocalBrain.PhoneRule(said), said)

    def TestReplies(self):
        self.assertEqual(LocalBrain.PhoneReply("ring", "Ringing your phone."), "Ringing your phone.")
        self.assertEqual(LocalBrain.PhoneReply("battery", "77%, charging"), "Your phone is at 77%, charging.")
        self.assertEqual(LocalBrain.PhoneReply("where", 'status=failed action=ask_phone reason="phone is off"'),
                         "That didn't work: phone is off.")
        self.assertEqual(LocalBrain.PhoneReply("where", ""), "Done.")


class TestTextOnlyRecipe(unittest.TestCase):
    def TestDropsThePictureAndSoundPart(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            big, small = Path(d, "sha256-aaa"), Path(d, "sha256-bbb")
            big.write_bytes(b"x" * 3000)
            small.write_bytes(b"x" * 1000)
            show = {"modelfile": f"# made by ollama\nFROM {big}\nFROM {small}\nTEMPLATE \"\"\"{{{{ .Prompt }}}}\"\"\"\n"
                                 "PARAMETER stop <bos>\nPARAMETER num_ctx 131072\n"}
            recipe = LocalBrain.TextOnlyRecipe(show)
        self.assertIn(f"FROM {big}", recipe)
        self.assertNotIn("sha256-bbb", recipe)
        self.assertIn("TEMPLATE", recipe)
        self.assertIn("PARAMETER stop <bos>", recipe)
        self.assertNotIn("131072", recipe)
        self.assertIn("PARAMETER num_gpu 99", recipe)
        self.assertIn("PARAMETER num_ctx 4096", recipe)

    def TestNoFilesIsNotReady(self):
        with self.assertRaises(LocalBrain.NotReady):
            LocalBrain.TextOnlyRecipe({"modelfile": "TEMPLATE x"})


class TestInstalledWithoutServer(unittest.TestCase):
    def TestReadsManifestFolder(self):
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            repo, _, tag = LocalBrain.BASE_MODEL.partition(":")
            p = Path(d, "manifests", *repo.split("/"))
            p.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"OLLAMA_MODELS": d}), \
                    mock.patch.object(LocalBrain, "Running", lambda: False):
                self.assertFalse(LocalBrain.Installed(refresh=True))
                (p / tag).write_text("{}")
                self.assertTrue(LocalBrain.Installed(refresh=True))
        LocalBrain._installed = None


if __name__ == "__main__":
    unittest.main()
