"""Run every Orion test.

    .venv\\Scripts\\python.exe -m Tests.RunAll            # unit tests
    .venv\\Scripts\\python.exe -m Tests.RunAll --all      # + the engine self-test (Engines._Tests)

Test files are Tests/Test*.py and test methods start with `Test`, in the
project's PascalCase style. Nothing here touches your real settings or memory:
Config/ApiKeys.json and Brain/LongTerm.json are saved first and put back after,
whatever happens.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROTECTED = [ROOT / "Config" / "ApiKeys.json", ROOT / "Brain" / "LongTerm.json"]


def Main() -> int:
    # Orion prints emoji; Main.py switches the console to UTF-8, so do the same.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    saved = {p: p.read_bytes() for p in PROTECTED if p.exists()}
    try:
        loader = unittest.TestLoader()
        loader.testMethodPrefix = "Test"
        suite = loader.discover(str(ROOT / "Tests"), pattern="Test*.py", top_level_dir=str(ROOT))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        ok = result.wasSuccessful()
        if "--all" in sys.argv:
            print("\n── Engines._Tests ──")
            r = subprocess.run([sys.executable, "-m", "Engines._Tests"], cwd=str(ROOT),
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            print(r.stdout[-3000:])
            ok = ok and r.returncode == 0
        return 0 if ok else 1
    finally:
        for p, data in saved.items():
            p.write_bytes(data)
        for p in PROTECTED:
            if p not in saved and p.exists():
                p.unlink()


if __name__ == "__main__":
    sys.exit(Main())
