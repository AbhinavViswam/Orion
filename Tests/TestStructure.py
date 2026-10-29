"""The whole project: every module loads, every cross-module name exists, and
nothing calls a library by a name the library does not have.

These are the checks that catch a rename that went too far (or not far
enough): `session.Receive()` on Gemini's session, `Outcome.Faild(...)`, a
method called through `self.` that no longer exists.
"""
import ast
import importlib.util
import importlib
import inspect
import unittest

from Tests.Helpers import App, ModuleName, SourceFiles, Tree, ROOT


class TestEveryModule(unittest.TestCase):
    def TestEveryModuleImports(self):
        App()
        failed = []
        for f in SourceFiles():
            if f.name == "Setup.py":
                continue
            try:
                importlib.import_module(ModuleName(f))
            except SystemExit:
                pass
            except Exception as e:
                failed.append(f"{ModuleName(f)}: {type(e).__name__}: {e}")
        self.assertEqual(failed, [])

    def TestEveryFileCompiles(self):
        for f in SourceFiles():
            compile(f.read_text(encoding="utf-8"), str(f), "exec")

    def TestFilesAndFoldersArePascalCase(self):
        bad = []
        for f in SourceFiles():
            for part in f.relative_to(ROOT).with_suffix("").parts:
                name = part.lstrip("_")
                if part != "__init__" and name and not name[0].isupper():
                    bad.append(str(f.relative_to(ROOT)))
        self.assertEqual(bad, [])


def _ProjectModuleBindings(tree):
    """Names bound to an Ultron module by `from Pkg import Mod` / `import X as Y`."""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            for a in n.names:
                full = f"{n.module}.{a.name}"
                try:
                    spec = importlib.util.find_spec(full)
                except Exception:
                    spec = None
                if spec and spec.origin and str(ROOT) in str(spec.origin):
                    out[a.asname or a.name] = full
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.asname:
                    try:
                        spec = importlib.util.find_spec(a.name)
                    except Exception:
                        spec = None
                    if spec and spec.origin and str(ROOT) in str(spec.origin):
                        out[a.asname] = a.name
    return out


class TestCrossModuleNames(unittest.TestCase):
    def TestFromImportsExist(self):
        """`from Brain.Settings import GetVoice` — GetVoice must exist there."""
        App()
        missing = []
        for f in SourceFiles():
            for n in ast.walk(Tree(f)):
                if isinstance(n, ast.ImportFrom) and n.module:
                    mod = n.module
                    if n.level:
                        base = ModuleName(f).split(".")
                        base = base if f.name == "__init__.py" else base[:-1]
                        base = base[: len(base) - (n.level - 1)]
                        mod = ".".join(base + ([n.module] if n.module else []))
                    try:
                        spec = importlib.util.find_spec(mod)
                    except Exception:
                        continue
                    if not spec or not spec.origin or str(ROOT) not in str(spec.origin):
                        continue
                    m = importlib.import_module(mod)
                    for a in n.names:
                        # _whatsapp_core is an optional community plugin, imported in a try
                        if a.name != "*" and not hasattr(m, a.name) and not a.name.startswith("_whatsapp"):
                            try:
                                importlib.import_module(f"{mod}.{a.name}")
                            except Exception:
                                missing.append(f"{f.relative_to(ROOT)}:{n.lineno} {mod}.{a.name}")
        self.assertEqual(missing, [])

    def TestModuleAttributesExist(self):
        """`Outcome.done(...)` — done must exist in Brain.Outcome."""
        App()
        missing = []
        for f in SourceFiles():
            tree = Tree(f)
            binds = _ProjectModuleBindings(tree)
            if not binds:
                continue
            mods = {k: importlib.import_module(v) for k, v in binds.items()}
            for n in ast.walk(tree):
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in mods:
                    if isinstance(n.ctx, ast.Load) and not hasattr(mods[n.value.id], n.attr):
                        missing.append(f"{f.relative_to(ROOT)}:{n.lineno} {n.value.id}.{n.attr}")
        self.assertEqual(missing, [])


class TestSelfAttributes(unittest.TestCase):
    """Every `self.X` a method reads must exist on the class, a base class, or
    be assigned somewhere — so a method renamed in one place only is caught."""

    def TestSelfReadsResolve(self):
        App()
        problems = []
        # instance attributes each project class assigns (parents count too)
        by_class = {}
        for f in SourceFiles():
            for cls in [n for n in ast.walk(Tree(f)) if isinstance(n, ast.ClassDef)]:
                s = by_class.setdefault(cls.name, set())
                for n in ast.walk(cls):
                    if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                            and n.value.id == "self" and isinstance(n.ctx, (ast.Store, ast.Del)):
                        s.add(n.attr)
                # dataclass / NamedTuple fields: `pid: int`
                for st in cls.body:
                    if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                        s.add(st.target.id)
        for f in SourceFiles():
            if f.name == "Setup.py":
                continue
            tree = Tree(f)
            mod = importlib.import_module(ModuleName(f))
            for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
                obj = getattr(mod, cls.name, None)
                if not inspect.isclass(obj):
                    continue          # nested or conditional class
                assigned = set()
                for n in ast.walk(cls):
                    if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                            and n.value.id == "self" and isinstance(n.ctx, (ast.Store, ast.Del)):
                        assigned.add(n.attr)
                    if isinstance(n, ast.Call) and getattr(n.func, "id", "") in ("setattr",) and n.args \
                            and isinstance(n.args[0], ast.Name) and n.args[0].id == "self" \
                            and len(n.args) > 1 and isinstance(n.args[1], ast.Constant):
                        assigned.add(n.args[1].value)
                known = set(dir(obj)) | assigned
                for k in obj.__mro__:
                    known |= by_class.get(k.__name__, set())
                for meth in [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                    for n in ast.walk(meth):
                        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                                and n.value.id == "self" and isinstance(n.ctx, ast.Load) \
                                and n.attr not in known:
                            problems.append(f"{f.relative_to(ROOT)}:{n.lineno} {cls.name}.{n.attr}")
        # Set at runtime by http.server's BaseHTTPRequestHandler for each request.
        runtime = {"_MockServer.wfile", "_MockServer.rfile", "_MockServer.headers"}
        left = sorted(p for p in set(problems) if p.split(" ", 1)[1] not in runtime)
        self.assertEqual(left, [])


class TestLibraryContracts(unittest.TestCase):
    """Ultron must call libraries by the names the libraries define."""

    def TestGeminiSessionCallsExist(self):
        """Every `self.session.X` in Main.py exists on Gemini's live session
        AND on Ultron's own engine (which stands in for it)."""
        from google.genai import live
        from Engines import Base, EnginePipeline
        session_cls = live.AsyncSession
        engines = [c for c in vars(EnginePipeline).values()
                   if inspect.isclass(c) and c.__module__ == EnginePipeline.__name__ and hasattr(c, "receive")]
        self.assertTrue(engines, "no engine class stands in for the Gemini session")
        called = set()
        for f in (ROOT / "Main.py", ROOT / "Brain" / "Gemini.py"):
            for n in ast.walk(Tree(f)):
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Attribute) \
                        and n.value.attr == "session":
                    called.add(n.attr)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "session":
                    called.add(n.attr)
        self.assertTrue(called)
        for name in sorted(called):
            self.assertTrue(hasattr(session_cls, name), f"Gemini's session has no '{name}'")
            for e in engines:
                self.assertTrue(hasattr(e, name), f"{e.__name__} has no '{name}' (Gemini's session does)")

    def TestOverridesKeepLibrarySpelling(self):
        """A class that extends a library class must override with the library's
        exact name: `paintEvent`, not `PaintEvent`; `run`, not `Run`."""
        App()
        problems = []
        for f in SourceFiles():
            if f.name == "Setup.py":
                continue
            mod = importlib.import_module(ModuleName(f))
            for name, cls in vars(mod).items():
                if not inspect.isclass(cls) or cls.__module__ != mod.__name__:
                    continue
                lib_names = set()
                for base in cls.__mro__[1:]:
                    if str(ROOT) not in str(getattr(inspect.getmodule(base), "__file__", "") or ""):
                        lib_names |= set(dir(base))
                folded = {n.replace("_", "").lower(): n for n in lib_names if not n.startswith("__")}
                for meth in vars(cls):
                    key = meth.replace("_", "").lower()
                    if meth not in lib_names and key in folded and not meth.startswith("__"):
                        problems.append(f"{cls.__name__}.{meth} should be '{folded[key]}'")
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
