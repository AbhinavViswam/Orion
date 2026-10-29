# config/__init__.py
import json, os, platform
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent / "ApiKeys.json"

def PlatformOs() -> str:
    """Auto-detect OS when config file is absent."""
    return {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}.get(
        platform.system(), "linux"
    )

def GetConfig() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def GetOs() -> str:
    """Returns: 'windows' | 'mac' | 'linux'"""
    return GetConfig().get("os_system", PlatformOs()).lower()

def IsWindows() -> bool: return GetOs() == "windows"
def IsMac()     -> bool: return GetOs() == "mac"
def IsLinux()   -> bool: return GetOs() == "linux"
