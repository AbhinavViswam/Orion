"""Shared bits for the tests."""
import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

CODE_DIRS = ["Interface", "Brain", "Voice", "Engines", "System", "Actions", "Plugins", "Config"]

_APP = None


def App():
    """One QApplication for every test that needs Qt."""
    global _APP
    from PyQt6.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication([])
    return _APP


def SourceFiles():
    """Every Ultron .py file (not the tests, not the venv)."""
    files = [ROOT / "Main.py", ROOT / "Setup.py"]
    for d in CODE_DIRS:
        files += sorted((ROOT / d).rglob("*.py"))
    return [f for f in files if "__pycache__" not in f.parts]


def ModuleName(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def Tree(path: Path):
    return ast.parse(path.read_text(encoding="utf-8"))
