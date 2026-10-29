"""System: tool and plugin loading, tool schemas, platform."""
import platform
import shutil
import tempfile
import unittest
from pathlib import Path

from Tests.Helpers import ROOT
from Config import GetOs, IsLinux, IsMac, IsWindows
from Engines import Schema
from System import ActionLoader, LazyImport, PlatformSupport, PluginLoader


def _Quiet(_msg):
    pass


class TestActions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = ActionLoader.DiscoverActions(ROOT / "Actions", logger=_Quiet)
        cls.decls = cls.reg.GetToolDeclarations()

    def TestEveryToolFileLoads(self):
        with_tool = [p for p in (ROOT / "Actions").glob("*.py")
                     if "TOOL = {" in p.read_text(encoding="utf-8")]
        self.assertEqual(len(self.reg.names()), len(with_tool))

    def TestToolNamesAreUniqueAndSnakeCase(self):
        names = [d["name"] for d in self.decls]
        self.assertEqual(len(names), len(set(names)))
        for n in names:
            self.assertRegex(n, r"^[a-z][a-z0-9_]*$", "Gemini tool names stay snake_case")

    def TestEveryToolHasADescription(self):
        for d in self.decls:
            self.assertTrue(d.get("description"), d["name"])

    def TestUnknownToolIsNotThere(self):
        self.assertFalse(self.reg.has("no_such_tool"))

    def TestSchemasConvertForEveryEngine(self):
        clean = ActionLoader.StripMeta(self.decls)
        for fmt in ("gemini", "openai", "anthropic"):
            self.assertTrue(Schema.convert(clean, fmt), fmt)
        self.assertIn(self.decls[0]["name"], Schema.ToPrompt(clean))


class TestPlugins(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _Template(self, name):
        src = (ROOT / "Plugins" / "_Template.py").read_text(encoding="utf-8")
        return src.replace('"my_plugin"', f'"{name}"')

    def TestTemplateIsSkipped(self):
        reg = PluginLoader.DiscoverPlugins(ROOT / "Plugins", set(), logger=_Quiet)
        self.assertFalse(reg.has("my_plugin"))
        self.assertNotIn("_Template.py", {r["file"] for r in reg.ListForUi()})

    def TestACopiedTemplateLoads(self):
        (self.tmp / "MyPlugin.py").write_text(self._Template("my_test_plugin"), encoding="utf-8")
        reg = PluginLoader.DiscoverPlugins(self.tmp, set(), logger=_Quiet)
        self.assertTrue(reg.has("my_test_plugin"))

    def TestABrokenPluginIsRejectedNotFatal(self):
        (self.tmp / "Broken.py").write_text("this is not python(", encoding="utf-8")
        reg = PluginLoader.DiscoverPlugins(self.tmp, set(), logger=_Quiet)
        self.assertEqual(reg.GetToolDeclarations(), [])

    def TestAPluginCannotTakeACoreToolName(self):
        (self.tmp / "Clash.py").write_text(self._Template("open_app"), encoding="utf-8")
        reg = PluginLoader.DiscoverPlugins(self.tmp, {"open_app"}, logger=_Quiet)
        self.assertFalse(reg.has("open_app"))


class TestPlatform(unittest.TestCase):
    def TestOsIsDetected(self):
        self.assertIn(GetOs(), ("windows", "mac", "linux"))
        self.assertEqual(sum([IsWindows(), IsMac(), IsLinux()]), 1)
        self.assertEqual(IsWindows(), platform.system() == "Windows")

    def TestSupportDescriptionsAreText(self):
        s = PlatformSupport.detect()
        self.assertIsInstance(PlatformSupport.DescribeForModel(s), (list, str))

    def TestLazyImportKnowsWhatIsInstalled(self):
        self.assertTrue(LazyImport.installed("json"))
        self.assertFalse(LazyImport.installed("surely_not_a_real_module_name"))


if __name__ == "__main__":
    unittest.main()
