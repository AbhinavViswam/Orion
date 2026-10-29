import os
import platform
import re
import sys
import json
import time
import subprocess
import threading
from pathlib import Path
from datetime import datetime

from Config import GetOs, IsWindows, IsMac, IsLinux

_CNW: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW}
    if platform.system() == "Windows" else {}
)

_KNOWN_APPIDS: dict[str, tuple[str, str]] = {
    "pubg":                ("578080",  "PUBG: Battlegrounds"),
    "pubg battlegrounds":  ("578080",  "PUBG: Battlegrounds"),
    "pubg: battlegrounds": ("578080",  "PUBG: Battlegrounds"),
    "battlegrounds":       ("578080",  "PUBG: Battlegrounds"),
    "gta5":                ("271590",  "Grand Theft Auto V"),
    "gta v":               ("271590",  "Grand Theft Auto V"),
    "grand theft auto v":  ("271590",  "Grand Theft Auto V"),
    "cs2":                 ("730",     "Counter-Strike 2"),
    "csgo":                ("730",     "Counter-Strike 2"),
    "counter-strike 2":    ("730",     "Counter-Strike 2"),
    "counter strike 2":    ("730",     "Counter-Strike 2"),
    "dota2":               ("570",     "Dota 2"),
    "dota 2":              ("570",     "Dota 2"),
    "rust":                ("252490",  "Rust"),
    "valheim":             ("892970",  "Valheim"),
    "cyberpunk":           ("1091500", "Cyberpunk 2077"),
    "cyberpunk 2077":      ("1091500", "Cyberpunk 2077"),
    "elden ring":          ("1245620", "ELDEN RING"),
    "minecraft":           ("1672970", "Minecraft Launcher"),
    "apex legends":        ("1172470", "Apex Legends"),
    "apex":                ("1172470", "Apex Legends"),
    "fortnite":            ("1517990", "Fortnite"),
    "goose goose duck":    ("1568590", "Goose Goose Duck"),
    "among us":            ("945360",  "Among Us"),
    "fall guys":           ("1097150", "Fall Guys"),
    "rocket league":       ("252950",  "Rocket League"),
    "warframe":            ("230410",  "Warframe"),
    "destiny 2":           ("1085660", "Destiny 2"),
    "team fortress 2":     ("440",     "Team Fortress 2"),
    "tf2":                 ("440",     "Team Fortress 2"),
    "left 4 dead 2":       ("550",     "Left 4 Dead 2"),
    "l4d2":                ("550",     "Left 4 Dead 2"),
    "paladins":            ("444090",  "Paladins"),
    "smite":               ("386360",  "SMITE"),
    "war thunder":         ("236390",  "War Thunder"),
    "world of warships":   ("552990",  "World of Warships"),
    "path of exile":       ("238960",  "Path of Exile"),
    "poe":                 ("238960",  "Path of Exile"),
    "lost ark":            ("1599340", "Lost Ark"),
    "new world":           ("1063730", "New World: Aeternum"),
}

def FindSteamPath() -> Path | None:
    if IsWindows(): return FindSteamWindows()
    if IsMac():     return FindSteamMac()
    return FindSteamLinux()


def FindSteamWindows() -> Path | None:
    try:
        import winreg
        for hive, key_path in [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
            (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Valve\Steam"),
        ]:
            try:
                key = winreg.OpenKey(hive, key_path)
                val, _ = winreg.QueryValueEx(key, "InstallPath")
                winreg.CloseKey(key)
                p = Path(val)
                if p.exists() and (p / "steam.exe").exists():
                    return p
            except Exception:
                continue
    except ImportError:
        pass
    for p in [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Steam",
        Path(os.environ.get("ProgramFiles", ""))       / "Steam",
        Path("C:/Steam"), Path("D:/Steam"), Path("E:/Steam"), Path("F:/Steam"),
    ]:
        if p.exists() and (p / "steam.exe").exists():
            return p
    return None


def FindSteamMac() -> Path | None:
    for p in [
        Path.home() / "Library" / "Application Support" / "Steam",
        Path("/Applications/Steam.app/Contents/MacOS"),
    ]:
        if p.exists():
            return p
    return None


def FindSteamLinux() -> Path | None:
    for p in [
        Path.home() / ".steam" / "steam",
        Path.home() / ".steam" / "root",
        Path.home() / ".local"  / "share" / "Steam",
        Path("/usr/share/steam"),
        Path("/opt/steam"),
    ]:
        if p.exists():
            return p
    return None


def SteamExe(steam_path: Path) -> Path:
    if IsWindows(): return steam_path / "steam.exe"
    if IsMac():     return Path("/Applications/Steam.app/Contents/MacOS/steam_osx")
    return steam_path / "steam.sh"


def LaunchSteamUrl(exe: Path, url: str) -> None:
    if IsMac():
        subprocess.Popen(["open", url])
    elif IsLinux():
        subprocess.Popen(["xdg-open", url])
    else:
        subprocess.Popen([str(exe), url])

def GetSteamLibraries(steam_path: Path) -> list[Path]:
    libraries = [steam_path / "steamapps"]
    vdf_path  = steam_path / "steamapps" / "libraryfolders.vdf"
    if not vdf_path.exists():
        return libraries
    try:
        content = vdf_path.read_text(encoding="utf-8", errors="ignore")
        for raw_path in re.findall(r'"path"\s+"([^"]+)"', content):
            lib = Path(raw_path.replace("\\\\", "/")) / "steamapps"
            if lib.exists() and lib not in libraries:
                libraries.append(lib)
    except Exception:
        pass
    return libraries


def GetSteamGames(steam_path: Path) -> list[dict]:
    games = []
    for lib in GetSteamLibraries(steam_path):
        for acf in lib.glob("appmanifest_*.acf"):
            try:
                content  = acf.read_text(encoding="utf-8", errors="ignore")
                app_id   = re.search(r'"appid"\s+"(\d+)"',     content)
                name     = re.search(r'"name"\s+"([^"]+)"',     content)
                state    = re.search(r'"StateFlags"\s+"(\d+)"', content)
                size     = re.search(r'"SizeOnDisk"\s+"(\d+)"', content)
                if app_id and name:
                    games.append({
                        "id":    app_id.group(1),
                        "name":  name.group(1),
                        "state": int(state.group(1)) if state else 0,
                        "size":  int(size.group(1))  if size  else 0,
                        "lib":   str(lib),
                        "acf":   str(acf),
                    })
            except Exception:
                continue
    return games

def IsSteamRunning() -> bool:
    try:
        if IsWindows():
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe"],
                                 capture_output=True, text=True, **_CNW).stdout
            return "steam.exe" in out.lower()
        proc = "steam_osx" if IsMac() else "steam"
        return bool(subprocess.run(["pgrep", "-x", proc],
                                   capture_output=True, text=True).stdout.strip())
    except Exception:
        return False

def GetSteamWindowRect() -> tuple[int, int, int, int] | None:
    try:
        import pygetwindow as gw
        for w in gw.getAllWindows():
            if "steam" in w.title.lower() and w.width > 200 and w.visible:
                return w.left, w.top, w.width, w.height
    except Exception:
        pass
    return None


def ClickFirstProfileByScreenshot() -> bool:

    try:
        import pyautogui
        import numpy as np

        time.sleep(1.5)
        win = GetSteamWindowRect()
        if not win:
            print("[GameUpdater] ⚠️ Steam window not found")
            return False

        wx, wy, ww, wh = win
        screenshot = pyautogui.screenshot(region=(wx, wy, ww, wh))
        img        = np.array(screenshot)
        h, w       = img.shape[:2]

        search_y1, search_y2 = h // 3,  h * 3 // 4
        search_x1, search_x2 = w // 5,  w * 4 // 5
        region = img[search_y1:search_y2, search_x1:search_x2]

        r, g, b = region[:,:,0].astype(int), region[:,:,1].astype(int), region[:,:,2].astype(int)
        max_c   = np.maximum(np.maximum(r, g), b)
        min_c   = np.minimum(np.minimum(r, g), b)
        colorful = (max_c > 60) & ((max_c - min_c) > 40)

        if not colorful.any():
            print("[GameUpdater] ⚠️ Avatar colour not found — clicking by guess")
            pyautogui.click(wx + ww // 2 - ww // 6, wy + wh // 2)
            return True

        cols = np.where(colorful.any(axis=0))[0]
        rows = np.where(colorful.any(axis=1))[0]
        if not len(cols) or not len(rows):
            return False

        avatar_w   = min(90, region.shape[1] // 4)
        first_col  = int(cols[0])
        block_cols = cols[cols < first_col + avatar_w]

        abs_x = wx + search_x1 + int(block_cols.mean())
        abs_y = wy + search_y1 + int(rows.mean())
        print(f"[GameUpdater] 🎯 Profile avatar ({abs_x}, {abs_y}) — clicking")
        pyautogui.click(abs_x, abs_y)
        return True

    except ImportError as e:
        print(f"[GameUpdater] ⚠️ Missing library: {e}")
        return False
    except Exception as e:
        print(f"[GameUpdater] ⚠️ Profile detection failed: {e}")
        return False


def HandleSteamProfileSelection() -> bool:
    print("[GameUpdater] 🔍 Checking profile-selection dialog...")
    win = GetSteamWindowRect()
    if not win:
        return False

    wx, wy, ww, wh = win
    try:
        import pyautogui, numpy as np
        screenshot   = pyautogui.screenshot(region=(wx, wy, ww, wh))
        img          = np.array(screenshot)
        is_small     = ww < 900 and wh < 700
        top_region   = img[:wh // 3, :, :]
        white_pixels = int(np.sum(
            (top_region[:,:,0] > 200) &
            (top_region[:,:,1] > 200) &
            (top_region[:,:,2] > 200)
        ))
        if not is_small and white_pixels <= 100:
            print("[GameUpdater] ℹ️ No profile dialog — Steam is already logged in")
            return False
    except ImportError:
        pass
    except Exception:
        pass

    print("[GameUpdater] 👤 Profile selection detected — clicking the first profile")
    return ClickFirstProfileByScreenshot()

def FindBestDrive() -> dict | None:
    import shutil, string
    drives = []
    for letter in string.ascii_uppercase:
        drive_path = f"{letter}:\\"
        if os.path.exists(drive_path):
            try:
                free_gb = shutil.disk_usage(drive_path).free / (1024 ** 3)
                if free_gb > 0:
                    drives.append({"letter": letter, "path": drive_path, "free_gb": free_gb})
            except Exception:
                continue
    return max(drives, key=lambda d: d["free_gb"]) if drives else None


def SelectDriveInDialog(dialog, drive_letter: str) -> bool:
    target = drive_letter.upper()
    for control_type in ("ListItem", "RadioButton"):
        try:
            for ctrl in dialog.descendants(control_type=control_type):
                if target in ctrl.window_text().upper():
                    ctrl.click_input()
                    print(f"[GameUpdater] ✅ Drive selected ({control_type}): {ctrl.window_text()}")
                    return True
        except Exception:
            continue
    try:
        for combo in dialog.descendants(control_type="ComboBox"):
            try:
                combo.expand()
                time.sleep(0.15)
                for idx, txt in enumerate(combo.texts()):
                    if target in txt.upper():
                        combo.select(idx)
                        return True
                combo.collapse()
            except Exception:
                continue
    except Exception:
        pass
    try:
        for ctrl in dialog.descendants():
            txt = ctrl.window_text().upper()
            if f"{target}:" in txt and len(txt) < 80:
                ctrl.click_input()
                return True
    except Exception:
        pass
    return False


def ClickButton(window, keywords: list[str]) -> bool:
    try:
        for btn in window.descendants(control_type="Button"):
            try:
                txt = btn.window_text().lower().strip()
                if txt in keywords or any(kw in txt for kw in keywords):
                    btn.click_input()
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def HandleInstallDialogPyautogui(game_name: str, best_drive: dict) -> str:
    try:
        import pyautogui
        import pygetwindow as gw
    except ImportError:
        return (f"Install dialog opened for '{game_name}'. "
                f"Please select '{best_drive['letter']}:' and click Install manually.")

    pyautogui.FAILSAFE = False
    drive_label = f"{best_drive['letter']}:"
    install_win = None

    for _ in range(30):
        time.sleep(0.5)
        for w in gw.getAllWindows():
            if ("install" in w.title.lower() or "steam" in w.title.lower()) \
                    and w.width > 300 and w.visible:
                install_win = w
                break
        if install_win:
            break

    if not install_win:
        return f"Please select '{drive_label}' and click Install in Steam for '{game_name}'."

    try:
        install_win.activate()
        time.sleep(0.4)
    except Exception:
        pass

    wx, wy = install_win.left, install_win.top
    ww, wh = install_win.width, install_win.height
    pyautogui.click(wx + int(ww * 0.35), wy + int(wh * 0.45))
    time.sleep(0.2)
    pyautogui.typewrite(best_drive["letter"], interval=0.05)
    time.sleep(0.2)
    pyautogui.click(wx + int(ww * 0.72), wy + int(wh * 0.88))
    return f"Attempted drive {drive_label} selection and Install click for '{game_name}'."


def HandleInstallDialog(game_name: str) -> str:
    best_drive = FindBestDrive()
    if not best_drive:
        return f"Install dialog opened for '{game_name}'. Could not detect drives."

    drive_letter = best_drive["letter"]
    drive_label  = f"{drive_letter}:"
    print(f"[GameUpdater] 🏆 Target drive: {drive_label} ({best_drive['free_gb']:.1f} GB free)")

    try:
        from pywinauto import Application, findwindows
        dialog = None

        # NOTE: this drives Steam's native installer dialog by matching its
        # on-screen button/title text, which is localized. We match English and
        # Turkish labels (install/yükle, next/ileri, ok/tamam). On a Steam client
        # set to another language the match simply fails and the automation
        # reports it — it never crashes, and manual install still works.
        for _ in range(40):
            time.sleep(0.5)
            try:
                for hwnd in findwindows.find_windows(
                    title_re=r"(?i)(install|yükle|steam)", visible_only=True
                ):
                    try:
                        app  = Application(backend="uia").connect(handle=hwnd)
                        win  = app.window(handle=hwnd)
                        rect = win.rectangle()
                        if win.is_visible() and rect.width() > 300 and rect.height() > 200:
                            all_text = " ".join(
                                c.window_text() for c in win.descendants()
                                if c.window_text()
                            ).upper()
                            if any(x in all_text for x in
                                   ("C:", "D:", "E:", "F:", "INSTALL", "YÜKLE")):
                                dialog = win
                                break
                    except Exception:
                        continue
            except Exception:
                pass
            if dialog:
                break

        if not dialog:
            raise RuntimeError("Dialog not found")

        dialog.set_focus()
        time.sleep(0.4)
        drive_selected  = SelectDriveInDialog(dialog, drive_letter)
        install_clicked = ClickButton(
            dialog, ["install", "yükle", "next", "ileri", "ok", "tamam"]
        )

        if install_clicked:
            suffix = f"Selected {drive_label} and" if drive_selected else "Default drive used, but"
            return f"{suffix} clicked Install for '{game_name}'."
        return f"Please click Install manually in Steam for '{game_name}'."

    except ImportError:
        return HandleInstallDialogPyautogui(game_name, best_drive)
    except Exception as e:
        print(f"[GameUpdater] ⚠️ pywinauto failed: {e}")
        return HandleInstallDialogPyautogui(game_name, best_drive)

def EnsureSteamRunning(steam_path: Path) -> bool:
    if IsSteamRunning():
        return True

    exe = SteamExe(steam_path)
    if not exe.exists():
        print(f"[GameUpdater] ❌ Steam not found: {exe}")
        return False

    print("[GameUpdater] 🚀 Starting Steam...")
    if IsMac():
        subprocess.Popen(["open", "-a", "Steam"])
    else:
        subprocess.Popen([str(exe)])

    for _ in range(20):
        time.sleep(1)
        if IsSteamRunning():
            print("[GameUpdater] ✅ Steam is running")
            time.sleep(4)
            if IsWindows():
                HandleSteamProfileSelection()
                time.sleep(2)
            return True

    print("[GameUpdater] ⚠️ Could not start Steam")
    return False

def SearchSteamAppid(game_name: str) -> tuple[str | None, str | None]:
    name_lower = game_name.lower().strip()


    steam_path = FindSteamPath()
    if steam_path:
        for g in GetSteamGames(steam_path):
            if name_lower in g["name"].lower():
                return g["id"], g["name"]

    if name_lower in _KNOWN_APPIDS:
        app_id, canonical = _KNOWN_APPIDS[name_lower]
        print(f"[GameUpdater] 📖 Bilinen: {canonical} ({app_id})")
        return app_id, canonical

    for key, (app_id, canonical) in _KNOWN_APPIDS.items():
        if name_lower in key or key in name_lower:
            print(f"[GameUpdater] 📖 Partial match: {canonical} ({app_id})")
            return app_id, canonical

    try:
        import urllib.request, urllib.parse
        query = urllib.parse.quote(game_name)
        url   = f"https://store.steampowered.com/api/storesearch/?term={query}&l=english&cc=US"
        req   = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            items = json.loads(resp.read().decode()).get("items", [])
        if items:
            best = items[0]
            print(f"[GameUpdater] 🌐 Store API: {best['name']} ({best['id']})")
            return str(best["id"]), best["name"]
    except Exception as e:
        print(f"[GameUpdater] ⚠️ AppID lookup failed: {e}")

    return None, None

def UpdateSteamGames(steam_path: Path, game_name: str = None) -> str:
    if not EnsureSteamRunning(steam_path):
        return "Could not start Steam."

    exe   = SteamExe(steam_path)
    games = GetSteamGames(steam_path)
    if not games:
        return "No Steam games found."

    if game_name:
        name_lower = game_name.lower()
        targets    = [g for g in games if name_lower in g["name"].lower()]
        if not targets:
            available = ", ".join(g["name"] for g in games[:5])
            return f"Game '{game_name}' not found. Installed: {available}..."
    else:
        targets = games

    already_updated, already_running, update_started, errors = [], [], [], []

    for game in targets:
        state = game["state"]
        name  = game["name"]
        if state == 4:
            already_updated.append(name)
        elif state == 1026:
            already_running.append(name)
        else:
            try:
                LaunchSteamUrl(exe, f"steam://update/{game['id']}")
                update_started.append(name)
                time.sleep(0.3)
            except Exception as e:
                errors.append(f"{name}: {e}")

    parts = []
    if update_started:
        names  = ", ".join(update_started[:3])
        suffix = f" and {len(update_started) - 3} more" if len(update_started) > 3 else ""
        parts.append(f"Update started for: {names}{suffix}.")
    if already_running:
        parts.append(f"Already updating: {', '.join(already_running)}.")
    if already_updated:
        parts.append(
            f"{already_updated[0]} is already up to date."
            if game_name else
            f"{len(already_updated)} game(s) already up to date."
        )
    if errors:
        parts.append(f"Errors: {'; '.join(errors)}.")
    return " ".join(parts) if parts else "No games to update."

def InstallSteamGame(steam_path: Path, game_name: str = None,
                        app_id: str = None) -> str:
    if not EnsureSteamRunning(steam_path):
        return "Could not start Steam."

    exe             = SteamExe(steam_path)
    installed_games = GetSteamGames(steam_path)

    already = None
    if app_id:
        already = next((g for g in installed_games if g["id"] == str(app_id)), None)
    elif game_name:
        name_lower = game_name.lower()
        already    = next((g for g in installed_games
                           if name_lower in g["name"].lower()), None)
    else:
        return "Please specify a game name or AppID."

    if already:
        state = already["state"]
        name  = already["name"]
        if state == 4:
            return f"'{name}' is already installed and up to date."
        if state == 1026:
            return f"'{name}' is currently downloading or updating."
        if state in (6, 516):
            LaunchSteamUrl(exe, f"steam://update/{already['id']}")
            return f"'{name}' has a pending update. Update started."
        return f"'{name}' is already installed."

    if not app_id and game_name:
        found_id, found_name = SearchSteamAppid(game_name)
        if not found_id:
            return (f"Could not find '{game_name}' on Steam. "
                    f"Try providing the AppID directly.")
        app_id    = found_id
        game_name = found_name or game_name
        print(f"[GameUpdater] 🔍 Kuruluyor: {game_name} (AppID: {app_id})")

    try:
        LaunchSteamUrl(exe, f"steam://install/{app_id}")

        if IsWindows():
            threading.Thread(
                target=HandleInstallDialog,
                args=(game_name or str(app_id),),
                daemon=True
            ).start()
        return f"Install started for '{game_name}'. Steam will open the download dialog."
    except Exception as e:
        return f"Install failed: {e}"

def GetDownloadStatus(steam_path: Path) -> str:
    games   = GetSteamGames(steam_path)
    active  = [g for g in games if g["state"] == 1026]
    pending = [g for g in games if g["state"] in (6, 516)]
    lines   = []
    if active:
        lines.append(f"Downloading: {', '.join(g['name'] for g in active)}.")
    if pending:
        names  = ", ".join(g["name"] for g in pending[:5])
        suffix = f" and {len(pending) - 5} more" if len(pending) > 5 else ""
        lines.append(f"Pending updates: {names}{suffix}.")
    return " ".join(lines) if lines else "No active downloads or pending updates."


def _SystemShutdown() -> None:
    if IsWindows():
        subprocess.run(["shutdown", "/s", "/t", "10"], **_CNW)
    elif IsMac():
        subprocess.run(["osascript", "-e", 'tell app "System Events" to shut down'])
    else:
        subprocess.run(["systemctl", "poweroff"])


def WatchAndShutdown(steam_path: Path, speak=None,
                        check_interval: int = 30, timeout_hours: int = 12):
    print("[GameUpdater]...")
    deadline = time.time() + timeout_hours * 3600

    for _ in range(24):
        time.sleep(5)
        active = [g for g in GetSteamGames(steam_path) if g["state"] == 1026]
        if active:
            names = ", ".join(g["name"] for g in active)
            if speak:
                speak(f"Download started for {names}. I'll shut down when done.")
            break
    else:
        return  

    while time.time() < deadline:
        time.sleep(check_interval)
        if not any(g["state"] == 1026 for g in GetSteamGames(steam_path)):
            if speak:
                speak("Download complete. Shutting down now.")
            time.sleep(5)
            _SystemShutdown()
            return

    if speak:
        speak("Download taking too long. Cancelling auto-shutdown.")


def FindEpicExe() -> Path | None:
    if IsWindows(): return FindEpicExeWindows()
    if IsMac():     return FindEpicExeMac()
    return FindEpicExeLinux()


def FindEpicExeWindows() -> Path | None:
    try:
        import winreg
        for hive, key_path in [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\EpicGames\EpicGamesLauncher"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\EpicGames\EpicGamesLauncher"),
            (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\EpicGames\EpicGamesLauncher"),
        ]:
            try:
                key = winreg.OpenKey(hive, key_path)
                val, _ = winreg.QueryValueEx(key, "AppDataPath")
                winreg.CloseKey(key)
                exe = Path(val) / "Binaries" / "Win64" / "EpicGamesLauncher.exe"
                if exe.exists():
                    return exe
            except Exception:
                continue
    except ImportError:
        pass
    for candidate in [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Epic Games" / "Launcher" / "Portal" / "Binaries" / "Win64" / "EpicGamesLauncher.exe",
        Path(os.environ.get("ProgramFiles", ""))       / "Epic Games" / "Launcher" / "Portal" / "Binaries" / "Win64" / "EpicGamesLauncher.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))        / "EpicGamesLauncher" / "Portal" / "Binaries" / "Win64" / "EpicGamesLauncher.exe",
    ]:
        if candidate.exists():
            return candidate
    return None


def FindEpicExeMac() -> Path | None:
    p = Path("/Applications/Epic Games Launcher.app/Contents/MacOS/EpicGamesLauncher")
    return p if p.exists() else None


def FindEpicExeLinux() -> Path | None:
    for c in [Path.home() / ".local" / "bin" / "heroic", Path("/usr/bin/heroic")]:
        if c.exists():
            return c
    return None


def EpicManifestsPath() -> Path | None:
    if IsWindows():
        p = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) \
            / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
        return p if p.exists() else None
    if IsMac():
        p = Path.home() / "Library" / "Application Support" \
            / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
        return p if p.exists() else None
    return None  


def GetEpicGames() -> list[dict]:
    manifests = EpicManifestsPath()
    if not manifests:
        return []
    games = []
    for item_file in manifests.glob("*.item"):
        try:
            data = json.loads(item_file.read_text(encoding="utf-8"))
            name = data.get("DisplayName") or data.get("AppName", "")
            if name:
                games.append({"id": data.get("AppName", ""), "name": name})
        except Exception:
            continue
    return games


def IsEpicRunning() -> bool:
    try:
        if IsWindows():
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq EpicGamesLauncher.exe"],
                capture_output=True, text=True, **_CNW
            ).stdout
            return "epicgameslauncher.exe" in out.lower()
        proc = "EpicGamesLauncher" if IsMac() else "heroic"
        return bool(subprocess.run(["pgrep", "-x", proc],
                                   capture_output=True, text=True).stdout.strip())
    except Exception:
        return False


def UpdateEpicGames(epic_exe: Path, game_name: str = None) -> str:
    games = GetEpicGames()

    if game_name:
        name_lower = game_name.lower()
        matched    = [g for g in games if name_lower in g["name"].lower()]
        if not matched:
            return f"'{game_name}' not found in Epic."
        try:
            url = f"com.epicgames.launcher://apps/{matched[0]['id']}?action=launch&silent=true"
            if IsMac():
                subprocess.Popen(["open", url])
            elif IsLinux():
                subprocess.Popen([str(epic_exe), url] if epic_exe else ["xdg-open", url])
            else:
                subprocess.Popen([str(epic_exe), url])
            return f"Opened Epic for '{matched[0]['name']}'."
        except Exception as e:
            return f"Epic update failed: {e}"
    else:
        try:
            if IsMac():
                subprocess.Popen(["open", "-a", "Epic Games Launcher"])
            elif IsLinux():
                if epic_exe:
                    subprocess.Popen([str(epic_exe)])
                else:
                    return ("Epic Games is not natively supported on Linux. "
                            "Consider using Heroic Launcher.")
            else:

                if IsEpicRunning():
                    for g in games[:10]:
                        subprocess.Popen([str(epic_exe),
                            f"com.epicgames.launcher://apps/{g['id']}?action=launch&silent=true"])
                        time.sleep(0.5)
                    return f"Triggered update check for {len(games)} Epic game(s)."
                else:
                    subprocess.Popen([str(epic_exe)])
            count = len(games)
            return (f"Epic Games Launcher opened. {count} game(s) will be checked."
                    if count else "Epic Games Launcher opened.")
        except Exception as e:
            return f"Epic launch failed: {e}"

def ScheduleDailyUpdate(hour: int = 3, minute: int = 0) -> str:
    if IsWindows(): return ScheduleWindows(hour, minute)
    if IsMac():     return ScheduleMac(hour, minute)
    return ScheduleLinux(hour, minute)


def ScheduleWindows(hour: int, minute: int) -> str:
    task_name   = "ORION_GameUpdater"
    script_path = Path(__file__).resolve()
    subprocess.run(["schtasks", "/Delete", "/TN", task_name, "/F"], capture_output=True, **_CNW)
    for extra in (["/RL", "HIGHEST", "/RU", "SYSTEM"], []):
        cmd    = ["schtasks", "/Create", "/TN", task_name,
                  "/TR", f'"{sys.executable}" "{script_path}" --scheduled',
                  "/SC", "DAILY", "/ST", f"{hour:02d}:{minute:02d}", "/F", *extra]
        result = subprocess.run(cmd, capture_output=True, text=True, **_CNW)
        if result.returncode == 0:
            return f"Daily game update scheduled at {hour:02d}:{minute:02d}."
    return f"Scheduling failed: {result.stderr.strip()}"


def ScheduleMac(hour: int, minute: int) -> str:
    plist_dir   = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path  = plist_dir / "com.orion.gameupdater.plist"
    script_path = Path(__file__).resolve()
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>com.orion.gameupdater</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{script_path}</string>
        <string>--scheduled</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>{hour}</integer>
        <key>Minute</key><integer>{minute}</integer>
    </dict>
    <key>RunAtLoad</key><false/>
</dict></plist>"""
    try:
        plist_path.write_text(plist_content, encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        result = subprocess.run(["launchctl", "load", str(plist_path)],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return f"Daily game update scheduled at {hour:02d}:{minute:02d} via launchd."
        return f"Scheduling failed: {result.stderr.strip()}"
    except Exception as e:
        return f"Scheduling failed: {e}"


def ScheduleLinux(hour: int, minute: int) -> str:
    script_path = Path(__file__).resolve()
    marker      = "# ORION_GameUpdater"
    cron_entry  = f"{minute} {hour} * * * {sys.executable} {script_path} --scheduled  {marker}"
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        lines    = [l for l in existing.stdout.splitlines()
                    if marker not in l and str(script_path) not in l]
        lines.append(cron_entry)
        proc = subprocess.run(["crontab", "-"],
                              input="\n".join(lines) + "\n",
                              text=True, capture_output=True)
        if proc.returncode == 0:
            return f"Daily game update scheduled at {hour:02d}:{minute:02d} via cron."
        return f"Scheduling failed: {proc.stderr.strip()}"
    except Exception as e:
        return f"Scheduling failed: {e}"


def CancelScheduledUpdate() -> str:
    if IsWindows():
        result = subprocess.run(
            ["schtasks", "/Delete", "/TN", "ORION_GameUpdater", "/F"],
            capture_output=True, text=True, **_CNW
        )
        return ("Scheduled update cancelled."
                if result.returncode == 0 else "No scheduled update found.")
    if IsMac():
        plist_path = Path.home() / "Library" / "LaunchAgents" / "com.orion.gameupdater.plist"
        if plist_path.exists():
            subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
            plist_path.unlink()
            return "Scheduled update cancelled."
        return "No scheduled update found."

    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        lines    = [l for l in existing.stdout.splitlines()
                    if "ORION_GameUpdater" not in l]
        subprocess.run(["crontab", "-"],
                       input="\n".join(lines) + "\n", text=True)
        return "Scheduled update cancelled."
    except Exception as e:
        return f"Cancel failed: {e}"


def GetScheduleStatus() -> str:
    if IsWindows():
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", "ORION_GameUpdater", "/FO", "LIST"],
            capture_output=True, text=True, **_CNW
        )
        if result.returncode != 0:
            return "No scheduled game update found."
        for line in result.stdout.strip().splitlines():
            if any(k in line for k in
                   ("Next Run", "Sonraki", "Prochaine", "Próxima", "Nächste")):
                return f"Game update scheduled. {line.strip()}"
        return "Game update is scheduled."
    if IsMac():
        plist_path = (Path.home() / "Library" / "LaunchAgents"
                      / "com.orion.gameupdater.plist")
        return ("Game update is scheduled via launchd."
                if plist_path.exists() else "No scheduled game update found.")

    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if "ORION_GameUpdater" in result.stdout:
            for line in result.stdout.splitlines():
                if "ORION_GameUpdater" in line:
                    return f"Game update is scheduled: {line.split('#')[0].strip()}"
        return "No scheduled game update found."
    except Exception:
        return "No scheduled game update found."


def game_updater(parameters: dict, player=None, speak=None) -> str:
    p         = parameters or {}
    action    = p.get("action",    "update").lower().strip()
    platform  = p.get("platform",  "both").lower().strip()
    game_name = (p.get("game_name") or "").strip() or None
    app_id    = (p.get("app_id")    or "").strip() or None
    hour      = int(p.get("hour",   3))
    minute    = int(p.get("minute", 0))
    shutdown  = str(p.get("shutdown_when_done", "false")).lower() == "true"

    results = []

    if action == "schedule":        return ScheduleDailyUpdate(hour=hour, minute=minute)
    if action == "cancel_schedule": return CancelScheduledUpdate()
    if action == "schedule_status": return GetScheduleStatus()

    if action == "list":
        if platform in ("steam", "both"):
            steam_path = FindSteamPath()
            if steam_path:
                games = GetSteamGames(steam_path)
                if games:
                    names  = ", ".join(g["name"] for g in games[:8])
                    suffix = f" and {len(games) - 8} more" if len(games) > 8 else ""
                    results.append(f"Steam ({len(games)} games): {names}{suffix}.")
                else:
                    results.append("Steam: No games found.")
            else:
                results.append("Steam: Not installed.")
        if platform in ("epic", "both"):
            if IsLinux():
                results.append("Epic: Not natively supported on Linux.")
            else:
                games = GetEpicGames()
                if games:
                    names  = ", ".join(g["name"] for g in games[:8])
                    suffix = f" and {len(games) - 8} more" if len(games) > 8 else ""
                    results.append(f"Epic ({len(games)} games): {names}{suffix}.")
                else:
                    results.append("Epic: No games found.")
        return " | ".join(results) or "No platforms found."

    if action == "download_status":
        if platform in ("steam", "both"):
            steam_path = FindSteamPath()
            results.append(
                GetDownloadStatus(steam_path) if steam_path else "Steam: Not installed."
            )
        if platform in ("epic", "both"):
            results.append("Epic download status not available directly.")
        return " ".join(results)

    if action in ("install", "update"):
        if platform in ("steam", "both"):
            steam_path = FindSteamPath()
            if not steam_path:
                results.append("Steam: Not installed.")
            else:
                if game_name:
                    installed  = GetSteamGames(steam_path)
                    name_lower = game_name.lower()
                    is_installed = any(
                        name_lower in g["name"].lower() for g in installed
                    )
                    if not is_installed:
                        msg = InstallSteamGame(
                            steam_path, game_name=game_name, app_id=app_id
                        )
                        if shutdown:
                            threading.Thread(
                                target=WatchAndShutdown,
                                kwargs={"steam_path": steam_path, "speak": speak},
                                daemon=True
                            ).start()
                            msg += " Auto-shutdown enabled."
                        if player: player.WriteLog(f"[GameUpdater] {msg[:100]}")
                        if speak:  speak(msg)
                        return msg
                    else:
                        results.append(
                            f"Steam: {UpdateSteamGames(steam_path, game_name=game_name)}"
                        )
                else:
                    if action == "install":
                        results.append("Steam: Please specify a game name to install.")
                    else:
                        results.append(f"Steam: {UpdateSteamGames(steam_path)}")

                if shutdown:
                    threading.Thread(
                        target=WatchAndShutdown,
                        kwargs={"steam_path": steam_path, "speak": speak},
                        daemon=True
                    ).start()
                    results.append("Auto-shutdown enabled.")

        if platform in ("epic", "both"):
            if IsLinux():
                results.append(
                    "Epic: Not natively supported on Linux. Use Heroic Launcher."
                )
            else:
                epic_exe = FindEpicExe()
                if epic_exe:
                    results.append(
                        f"Epic: {UpdateEpicGames(epic_exe, game_name=game_name)}"
                    )
                else:
                    results.append("Epic: Not installed.")

        output = " | ".join(results) or "Nothing to do."
        if player: player.WriteLog(f"[GameUpdater] {output[:100]}")
        if speak:  speak(output)
        return output

    return f"Unknown action: '{action}'."


if __name__ == "__main__":
    if "--scheduled" in sys.argv:
        print(f"[GameUpdater] 🕐 Scheduled run at {datetime.now().strftime('%H:%M')}")
        result = game_updater({"action": "update", "platform": "both"})
        print(f"[GameUpdater] ✅ {result}")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "game_updater",
    "description": "THE ONLY tool for ANY Steam or Epic Games request. Use for: installing, downloading, updating games, listing installed games, checking download status, scheduling updates. ALWAYS call directly for any Steam/Epic/game request. NEVER use browser_control or web_search for Steam/Epic.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status (default: update)"
            },
            "platform": {
                "type": "STRING",
                "description": "steam | epic | both (default: both)"
            },
            "game_name": {
                "type": "STRING",
                "description": "Game name (partial match supported)"
            },
            "app_id": {
                "type": "STRING",
                "description": "Steam AppID for install (optional)"
            },
            "hour": {
                "type": "INTEGER",
                "description": "Hour for scheduled update 0-23 (default: 3)"
            },
            "minute": {
                "type": "INTEGER",
                "description": "Minute for scheduled update 0-59 (default: 0)"
            },
            "shutdown_when_done": {
                "type": "BOOLEAN",
                "description": "Shut down PC when download finishes"
            }
        },
        "required": []
    },
    "handler": game_updater,
}
