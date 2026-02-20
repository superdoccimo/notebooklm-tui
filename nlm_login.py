"""
nlm_login.py: NotebookLM authentication tool

Launch a browser for Google login and automatically retrieve cookies.
Supports Chrome, Edge, Brave, and Firefox. Zero external dependencies.

Language: Set NLM_LANG=en or NLM_LANG=ja (auto-detected from OS locale).

Usage:
    python nlm_login.py                  # Login with default browser
    python nlm_login.py --browser chrome # Login with Chrome
    python nlm_login.py --browser firefox # Login with Firefox
    python nlm_login.py --browser firefox --firefox-profile ~/.mozilla/firefox/xxxx.default-release
    python nlm_login.py --extract        # Direct DB read (browser must be closed)
    python nlm_login.py --check          # Check authentication status
    python nlm_login.py --list-browsers  # List detected browsers
"""

import argparse
import base64
import configparser
import ctypes
import ctypes.wintypes
import json
import locale
import os
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_COOKIES_PATH = Path.home() / ".notebooklm-mcp-cli" / "profiles" / "default" / "cookies.json"
REQUIRED_COOKIES = {"SID", "HSID", "SSID", "APISID", "SAPISID"}
CDP_PORT = 9222


# ---------------------------------------------------------------------------
# i18n
# ---------------------------------------------------------------------------

def _detect_lang() -> str:
    env = os.environ.get("NLM_LANG", "").lower()
    if env in ("ja", "en"):
        return env
    try:
        loc = locale.getdefaultlocale()[0] or ""
    except Exception:
        loc = ""
    return "ja" if loc.startswith("ja") else "en"


LANG = _detect_lang()

MESSAGES = {
    # --- CLI help ---
    "cli_description": {
        "ja": "NotebookLM 認証ツール - ブラウザからクッキーを取得",
        "en": "NotebookLM auth tool - retrieve cookies from browser",
    },
    "cli_epilog": {
        "ja": (
            "例:\n"
            "  python nlm_login.py              # 既定ブラウザ候補でログイン\n"
            "  python nlm_login.py -b chrome     # Chrome でログイン\n"
            "  python nlm_login.py -b firefox    # Firefox でログイン\n"
            "  python nlm_login.py -b firefox --firefox-profile ~/.mozilla/firefox/xxxx.default-release\n"
            "  python nlm_login.py --extract     # DB直接読み取り(ブラウザ終了必要)\n"
            "  python nlm_login.py --check       # 認証確認\n"
        ),
        "en": (
            "Examples:\n"
            "  python nlm_login.py              # Login with default browser\n"
            "  python nlm_login.py -b chrome     # Login with Chrome\n"
            "  python nlm_login.py -b firefox    # Login with Firefox\n"
            "  python nlm_login.py -b firefox --firefox-profile ~/.mozilla/firefox/xxxx.default-release\n"
            "  python nlm_login.py --extract     # Direct DB read (browser must be closed)\n"
            "  python nlm_login.py --check       # Check auth status\n"
        ),
    },
    "help_browser": {
        "ja": "ブラウザを指定 (edge/chrome/brave/firefox)",
        "en": "Specify browser (edge/chrome/brave/firefox)",
    },
    "help_extract": {
        "ja": "DB直接読み取り (ブラウザ終了が必要)",
        "en": "Direct DB read (browser must be closed)",
    },
    "help_check": {
        "ja": "認証状態を確認",
        "en": "Check authentication status",
    },
    "help_list_browsers": {
        "ja": "検出ブラウザ一覧",
        "en": "List detected browsers",
    },
    "help_firefox_profile": {
        "ja": "Firefox プロファイルディレクトリ (cookies.sqlite があるパス)",
        "en": "Firefox profile directory (path containing cookies.sqlite)",
    },
    "help_output": {
        "ja": "クッキー出力先",
        "en": "Cookie output path",
    },
    # --- Browser detection / selection ---
    "err_browser_not_found": {
        "ja": "[ERROR] {browser} が見つかりません。",
        "en": "[ERROR] {browser} not found.",
    },
    "available_browsers": {
        "ja": "  利用可能: {browsers}",
        "en": "  Available: {browsers}",
    },
    "err_no_browser": {
        "ja": "[ERROR] 対応ブラウザが見つかりません (Edge, Chrome, Brave, Firefox)",
        "en": "[ERROR] No supported browser found (Edge, Chrome, Brave, Firefox)",
    },
    "detected_browsers": {
        "ja": "\n検出されたブラウザ:",
        "en": "\nDetected browsers:",
    },
    "profile_not_detected": {
        "ja": "(未検出: 実行時に一時プロファイルでフォールバック)",
        "en": "(not detected: will fall back to temporary profile)",
    },
    # --- CDP login ---
    "launching_browser": {
        "ja": "\n{name} を起動します...",
        "en": "\nLaunching {name}...",
    },
    "login_instruction": {
        "ja": "\n  ブラウザで Google アカウントにログインしてください。\n  NotebookLM のホーム画面が表示されたら、",
        "en": "\n  Please log in to your Google account in the browser.\n  Once the NotebookLM home screen appears,",
    },
    "press_enter": {
        "ja": "  ここに戻って Enter を押してください > ",
        "en": "  come back here and press Enter > ",
    },
    "cancelled": {
        "ja": "\nキャンセルしました。",
        "en": "\nCancelled.",
    },
    "fetching_cookies": {
        "ja": "\n  クッキーを取得中...",
        "en": "\n  Fetching cookies...",
    },
    "err_cdp_failed": {
        "ja": "  [ERROR] CDP 通信に失敗: {error}",
        "en": "  [ERROR] CDP communication failed: {error}",
    },
    "err_cdp_timeout": {
        "ja": "CDP レスポンスがタイムアウト: {method}",
        "en": "CDP response timed out: {method}",
    },
    "err_no_page_tab": {
        "ja": "ページタブが見つかりません",
        "en": "No page tab found",
    },
    # --- Firefox login ---
    "firefox_temp_profile": {
        "ja": "  既存 Firefox プロファイルが未検出のため、一時プロファイルで起動します。",
        "en": "  No existing Firefox profile detected; launching with temporary profile.",
    },
    "warn_browser_launch_failed": {
        "ja": "\n  [WARN] ブラウザ起動に失敗: {error}",
        "en": "\n  [WARN] Failed to launch browser: {error}",
    },
    "firefox_manual_open": {
        "ja": "  手動で Firefox を開いて NotebookLM にログインしてください。",
        "en": "  Please open Firefox manually and log in to NotebookLM.",
    },
    "firefox_login_instruction": {
        "ja": "\n  Firefox で NotebookLM にログインしてください。",
        "en": "\n  Please log in to NotebookLM in Firefox.",
    },
    "firefox_close_then_enter": {
        "ja": "  ログイン後、この Firefox ウィンドウを閉じてから Enter を押してください。",
        "en": "  After login, close this Firefox window and then press Enter.",
    },
    "firefox_press_enter": {
        "ja": "  ログイン後、ここに戻って Enter を押してください > ",
        "en": "  After login, come back here and press Enter > ",
    },
    "err_firefox_cookies_not_found": {
        "ja": "Firefox の cookies.sqlite が見つかりません: {path}",
        "en": "Firefox cookies.sqlite not found: {path}",
    },
    "err_firefox_copy_failed": {
        "ja": "Firefox クッキーDBのコピーに失敗: {error}",
        "en": "Failed to copy Firefox cookie DB: {error}",
    },
    "err_firefox_read_failed": {
        "ja": "Firefox クッキーDBの読み取りに失敗: {error}",
        "en": "Failed to read Firefox cookie DB: {error}",
    },
    # --- DB extract ---
    "err_browser_key_not_found": {
        "ja": "{browser} が見つかりません",
        "en": "{browser} not found",
    },
    "err_cookies_db_not_found": {
        "ja": "クッキーDBが見つかりません: {path}",
        "en": "Cookie DB not found: {path}",
    },
    "err_cookies_db_locked": {
        "ja": (
            "クッキーDBがロックされています。\n"
            "  {name} を終了してから再実行してください。\n"
            "  または python nlm_login.py (CDP方式) を使ってください。\n"
            "  元のエラー: {error}"
        ),
        "en": (
            "Cookie DB is locked.\n"
            "  Please close {name} and try again.\n"
            "  Or use python nlm_login.py (CDP mode) instead.\n"
            "  Original error: {error}"
        ),
    },
    "extract_reading": {
        "ja": "\n{name} のクッキーDB から直接読み取ります...",
        "en": "\nReading cookies directly from {name} DB...",
    },
    "err_extract_windows_only": {
        "ja": "[ERROR] --extract は現在 Windows でのみサポートしています。\n  Linux/macOS では通常モード（CDP方式）を使用してください。",
        "en": "[ERROR] --extract is currently supported on Windows only.\n  On Linux/macOS, use the default mode (CDP) instead.",
    },
    # --- Common / results ---
    "err_no_cookies": {
        "ja": "\n[ERROR] Google クッキーが取得できませんでした。\n  NotebookLM にログインしてから再実行してください。",
        "en": "\n[ERROR] Failed to retrieve Google cookies.\n  Please log in to NotebookLM and try again.",
    },
    "err_missing_cookies": {
        "ja": "\n[ERROR] 必要なクッキーが不足: {missing}\n  NotebookLM にログインしてから再実行してください。",
        "en": "\n[ERROR] Required cookies missing: {missing}\n  Please log in to NotebookLM and try again.",
    },
    "cookies_saved": {
        "ja": "\n  クッキー保存完了 → {path}\n  ({count} 件の Google クッキー)",
        "en": "\n  Cookies saved -> {path}\n  ({count} Google cookies)",
    },
    "verifying_auth": {
        "ja": "\n認証を確認中...",
        "en": "\nVerifying authentication...",
    },
    "setup_complete": {
        "ja": "\nセットアップ完了! nlm-backup / nlm-upload が使えます。",
        "en": "\nSetup complete! You can now use nlm-backup / nlm-upload.",
    },
    "warn_auth_failed": {
        "ja": "\n[WARN] 認証に失敗しました。再度ログインしてください。",
        "en": "\n[WARN] Authentication failed. Please log in again.",
    },
    "cookies_file_not_found": {
        "ja": "クッキーファイルが見つかりません: {path}",
        "en": "Cookie file not found: {path}",
    },
    "auth_ok": {
        "ja": "認証OK - {count} 件のノートブックにアクセスできます。",
        "en": "Auth OK - {count} notebook(s) accessible.",
    },
    "auth_error": {
        "ja": "認証エラー ({error_type}): {error}",
        "en": "Auth error ({error_type}): {error}",
    },
}


def msg(key: str, **kwargs) -> str:
    entry = MESSAGES[key]
    return entry.get(LANG, entry["en"]).format(**kwargs)


# ---------------------------------------------------------------------------
# Browser detection
# ---------------------------------------------------------------------------

BROWSER_REGISTRY = {
    "edge": {
        "name": "Microsoft Edge",
        "exe_names": ["msedge.exe", "msedge", "microsoft-edge", "microsoft-edge-stable"],
        "known_paths": [
            "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
            "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
        ],
        "registry_key": r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
        "user_data": Path.home() / "AppData/Local/Microsoft/Edge/User Data",
    },
    "chrome": {
        "name": "Google Chrome",
        "exe_names": [
            "chrome.exe",
            "chrome",
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        ],
        "known_paths": [
            "C:/Program Files/Google/Chrome/Application/chrome.exe",
            "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
        ],
        "registry_key": r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
        "user_data": Path.home() / "AppData/Local/Google/Chrome/User Data",
    },
    "brave": {
        "name": "Brave Browser",
        "exe_names": ["brave.exe", "brave", "brave-browser", "brave-browser-stable"],
        "known_paths": [
            "C:/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe",
        ],
        "registry_key": r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\brave.exe",
        "user_data": Path.home() / "AppData/Local/BraveSoftware/Brave-Browser/User Data",
    },
    "firefox": {
        "name": "Mozilla Firefox",
        "exe_names": ["firefox", "firefox.exe"],
        "known_paths": [
            "/usr/bin/firefox",
            "/snap/bin/firefox",
            "C:/Program Files/Mozilla Firefox/firefox.exe",
        ],
        "registry_key": r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\firefox.exe",
        "user_data": Path.home() / ".mozilla/firefox",
    },
}


def find_browser_exe(browser_key: str) -> str | None:
    """Find browser executable."""
    info = BROWSER_REGISTRY.get(browser_key)
    if not info:
        return None

    # Search PATH
    for name in info["exe_names"]:
        found = shutil.which(name)
        if found:
            return found

    # Search Windows registry
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, info["registry_key"])
        value, _ = winreg.QueryValueEx(key, "")
        winreg.CloseKey(key)
        if Path(value).exists():
            return value
    except (OSError, ImportError):
        pass

    # Search known paths (protect against invalid path strings)
    for path in info["known_paths"]:
        try:
            if Path(path).exists():
                return path
        except OSError:
            continue

    return None


def detect_browsers() -> dict[str, dict]:
    """Detect available browsers."""
    browsers = {}
    for key, info in BROWSER_REGISTRY.items():
        exe = find_browser_exe(key)
        if exe:
            entry = {
                "name": info["name"],
                "exe": exe,
                "user_data": info["user_data"],
            }
            if key == "firefox":
                profile_dir = find_firefox_profile_dir()
                if profile_dir:
                    entry["profile_dir"] = profile_dir
            browsers[key] = entry
    return browsers


def _resolve_firefox_profile_path(root: Path, path_value: str, is_relative: bool) -> Path:
    if is_relative:
        return root / path_value
    return Path(path_value)


def _pick_firefox_profile_from_ini(profiles_ini: Path, root: Path) -> Path | None:
    if not profiles_ini.exists():
        return None

    parser = configparser.ConfigParser()
    parser.read(profiles_ini, encoding="utf-8")

    profile_sections = [s for s in parser.sections() if s.startswith("Profile")]
    if not profile_sections:
        return None

    install_defaults = []
    for section in parser.sections():
        if section.startswith("Install"):
            default_path = parser.get(section, "Default", fallback="").strip()
            if default_path:
                install_defaults.append(default_path)

    candidates: list[tuple[int, Path]] = []
    for section in profile_sections:
        path_value = parser.get(section, "Path", fallback="").strip()
        if not path_value:
            continue

        is_relative = parser.getboolean(section, "IsRelative", fallback=True)
        profile_path = _resolve_firefox_profile_path(root, path_value, is_relative)
        if not profile_path.exists():
            continue

        score = 0
        if parser.get(section, "Default", fallback="0") in ("1", "true", "True"):
            score += 20
        if path_value in install_defaults or profile_path.name in install_defaults:
            score += 40
        if profile_path.name.endswith(".default-release"):
            score += 10
        if (profile_path / "cookies.sqlite").exists():
            score += 10
        candidates.append((score, profile_path))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def find_firefox_profile_dir() -> Path | None:
    profile_roots = [
        Path.home() / ".mozilla" / "firefox",
        Path.home() / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
        Path.home() / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
    ]

    for root in profile_roots:
        selected = _pick_firefox_profile_from_ini(root / "profiles.ini", root)
        if selected:
            return selected

        # Fallback when profiles.ini is missing (snap/portable etc.)
        if not root.exists():
            continue
        candidates: list[tuple[int, Path]] = []
        for p in root.iterdir():
            if not p.is_dir():
                continue
            if not (p / "cookies.sqlite").exists():
                continue
            score = 0
            if p.name.endswith(".default-release"):
                score += 20
            elif p.name.endswith(".default"):
                score += 10
            candidates.append((score, p))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
    return None


# ---------------------------------------------------------------------------
# Minimal WebSocket client for CDP (stdlib only)
# ---------------------------------------------------------------------------

class SimpleWebSocket:
    """Minimal WebSocket client for CDP communication."""

    def __init__(self, url: str):
        parsed = urllib.parse.urlparse(url)
        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.path = parsed.path or "/"

        self.sock = socket.create_connection((self.host, self.port), timeout=10)
        self._handshake()

    def _handshake(self):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self.sock.sendall(req.encode())

        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket handshake: connection closed")
            resp += chunk

        if b"101" not in resp.split(b"\r\n")[0]:
            raise RuntimeError(f"WebSocket handshake failed: {resp[:100]}")

    def send(self, data: str):
        payload = data.encode()
        mask_key = os.urandom(4)

        header = bytearray([0x81])  # FIN + text
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        header.extend(mask_key)

        masked = bytearray(payload)
        for i in range(len(masked)):
            masked[i] ^= mask_key[i % 4]

        self.sock.sendall(header + masked)

    def recv(self) -> str:
        header = self._recv_exact(2)
        opcode = header[0] & 0x0F
        has_mask = bool(header[1] & 0x80)
        length = header[1] & 0x7F

        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]

        mask_key = self._recv_exact(4) if has_mask else None
        payload = bytearray(self._recv_exact(length))

        if mask_key:
            for i in range(len(payload)):
                payload[i] ^= mask_key[i % 4]

        if opcode == 8:  # close
            raise RuntimeError("WebSocket closed by server")
        if opcode == 9:  # ping -> pong
            self.sock.sendall(b"\x8a\x80" + os.urandom(4))
            return self.recv()

        return bytes(payload).decode("utf-8", errors="replace")

    def _recv_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(min(n - len(data), 65536))
            if not chunk:
                raise RuntimeError("WebSocket connection closed")
            data += chunk
        return data

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CDP login (primary method)
# ---------------------------------------------------------------------------

def cdp_login(browser_key: str, browsers: dict, output_path: Path) -> list[dict]:
    """Launch browser and retrieve cookies via CDP."""
    if browser_key not in browsers:
        print(msg("err_browser_not_found", browser=browser_key))
        print(msg("available_browsers", browsers=", ".join(browsers.keys())))
        sys.exit(1)

    info = browsers[browser_key]
    exe = info["exe"]
    tmpdir = tempfile.mkdtemp(prefix="nlm_login_")

    print(msg("launching_browser", name=info["name"]))

    # Launch browser with temporary profile + debug port
    proc = subprocess.Popen(
        [
            exe,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={tmpdir}",
            "--no-first-run",
            "--no-default-browser-check",
            "https://notebooklm.google.com/",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    print(msg("login_instruction"))

    try:
        input(msg("press_enter"))
    except (EOFError, KeyboardInterrupt):
        proc.terminate()
        shutil.rmtree(tmpdir, ignore_errors=True)
        print(msg("cancelled"))
        sys.exit(0)

    # Retrieve cookies via CDP
    print(msg("fetching_cookies"))
    try:
        cookies = get_cookies_via_cdp()
    except Exception as e:
        print(msg("err_cdp_failed", error=e))
        cookies = []
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(1)
        shutil.rmtree(tmpdir, ignore_errors=True)

    return cookies


def extract_firefox_cookies(profile_dir: Path) -> list[dict]:
    """Extract Google cookies from Firefox profile's cookies.sqlite."""
    cookies_db = profile_dir / "cookies.sqlite"
    if not cookies_db.exists():
        raise RuntimeError(msg("err_firefox_cookies_not_found", path=cookies_db))

    query = (
        "SELECT host, name, value, path, expiry, isSecure "
        "FROM moz_cookies "
        "WHERE host LIKE '%.google.com' OR host='google.com' OR host='notebooklm.google.com'"
    )

    tmp_dir = Path(tempfile.mkdtemp(prefix="nlm_firefox_cookies_"))
    tmp_db = tmp_dir / "cookies.sqlite"

    try:
        shutil.copy2(cookies_db, tmp_db)
        # Copy WAL-mode companion files if present
        for suffix in ("-wal", "-shm"):
            src = profile_dir / f"cookies.sqlite{suffix}"
            if src.exists():
                shutil.copy2(src, tmp_dir / src.name)
    except OSError as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(msg("err_firefox_copy_failed", error=e))

    conn = None
    try:
        conn = sqlite3.connect(str(tmp_db))
        rows = conn.execute(query).fetchall()
    except sqlite3.Error as e:
        raise RuntimeError(msg("err_firefox_read_failed", error=e))
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)

    cookies = []
    for host, name, value, path, expiry, is_secure in rows:
        if not name or value is None:
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": host,
                "path": path or "/",
                "expires": int(expiry or 0),
                "secure": bool(is_secure),
            }
        )
    return cookies


def firefox_login(info: dict) -> list[dict]:
    """Open Firefox for login, then extract cookies from profile DB."""
    profile_dir = info.get("profile_dir")
    temp_profile_dir: Path | None = None
    firefox_proc: subprocess.Popen | None = None
    use_temp_profile = profile_dir is None

    if use_temp_profile:
        temp_profile_dir = Path(tempfile.mkdtemp(prefix="nlm_firefox_profile_"))
        profile_dir = temp_profile_dir

    print(msg("launching_browser", name=info["name"]))
    try:
        if use_temp_profile:
            print(msg("firefox_temp_profile"))
            firefox_proc = subprocess.Popen(
                [
                    info["exe"],
                    "-no-remote",
                    "-profile",
                    str(profile_dir),
                    "https://notebooklm.google.com/",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            firefox_proc = subprocess.Popen(
                [info["exe"], "https://notebooklm.google.com/"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except OSError as e:
        print(msg("warn_browser_launch_failed", error=e))
        print(msg("firefox_manual_open"))

    print(msg("firefox_login_instruction"))
    if use_temp_profile:
        print(msg("firefox_close_then_enter"))
    try:
        input(msg("firefox_press_enter"))
    except (EOFError, KeyboardInterrupt):
        if firefox_proc and firefox_proc.poll() is None:
            firefox_proc.terminate()
            try:
                firefox_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                firefox_proc.kill()
        if temp_profile_dir:
            shutil.rmtree(temp_profile_dir, ignore_errors=True)
        print(msg("cancelled"))
        sys.exit(0)

    try:
        cookies = extract_firefox_cookies(profile_dir)
    finally:
        if firefox_proc and firefox_proc.poll() is None and use_temp_profile:
            firefox_proc.terminate()
            try:
                firefox_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                firefox_proc.kill()
        if temp_profile_dir:
            shutil.rmtree(temp_profile_dir, ignore_errors=True)

    return cookies


def _cdp_send_recv(ws: SimpleWebSocket, msg_id: int, method: str, params: dict | None = None) -> dict:
    """Send a CDP command and wait for a response."""
    cmd = {"id": msg_id, "method": method}
    if params:
        cmd["params"] = params
    ws.send(json.dumps(cmd))

    for _ in range(50):
        raw = ws.recv()
        result = json.loads(raw)
        if result.get("id") == msg_id:
            return result
    raise RuntimeError(msg("err_cdp_timeout", method=method))


def get_cookies_via_cdp() -> list[dict]:
    """Retrieve all cookies via CDP WebSocket."""
    resp = urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json", timeout=5)
    tabs = json.loads(resp.read())

    # Find a NotebookLM or Google page tab
    ws_url = None
    for tab in tabs:
        if tab.get("type") != "page":
            continue
        url = tab.get("url", "")
        if "notebooklm" in url or "google.com" in url:
            ws_url = tab.get("webSocketDebuggerUrl")
            break
    if not ws_url:
        # Fall back to the first page tab
        for tab in tabs:
            if tab.get("type") == "page" and tab.get("webSocketDebuggerUrl"):
                ws_url = tab["webSocketDebuggerUrl"]
                break
    if not ws_url:
        raise RuntimeError(msg("err_no_page_tab"))

    ws = SimpleWebSocket(ws_url)
    try:
        _cdp_send_recv(ws, 1, "Network.enable")
        result = _cdp_send_recv(ws, 2, "Network.getAllCookies")
    finally:
        ws.close()

    # Filter Google cookies
    raw_cookies = result.get("result", {}).get("cookies", [])
    cookies = []
    for c in raw_cookies:
        domain = c.get("domain", "")
        if ".google.com" in domain or "google.com" == domain:
            cookies.append({
                "name": c["name"],
                "value": c["value"],
                "domain": domain,
                "path": c.get("path", "/"),
                "expires": int(c.get("expires", 0)),
                "secure": c.get("secure", True),
            })
    return cookies


# ---------------------------------------------------------------------------
# Direct DB read (only works when browser is closed)
# ---------------------------------------------------------------------------

class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def dpapi_decrypt(encrypted: bytes) -> bytes:
    """Decrypt using Windows DPAPI."""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    input_blob = DATA_BLOB()
    input_blob.cbData = len(encrypted)
    input_blob.pbData = (ctypes.c_byte * len(encrypted))(*encrypted)
    output_blob = DATA_BLOB()

    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob), None, None, None, None, 0,
        ctypes.byref(output_blob),
    ):
        raise RuntimeError("DPAPI decryption failed")

    result = bytes((ctypes.c_byte * output_blob.cbData).from_address(
        ctypes.cast(output_blob.pbData, ctypes.c_void_p).value
    ))
    kernel32.LocalFree(output_blob.pbData)
    return result


class BCRYPT_AUTH_MODE_INFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong), ("dwInfoVersion", ctypes.c_ulong),
        ("pbNonce", ctypes.c_void_p), ("cbNonce", ctypes.c_ulong),
        ("pbAuthData", ctypes.c_void_p), ("cbAuthData", ctypes.c_ulong),
        ("pbTag", ctypes.c_void_p), ("cbTag", ctypes.c_ulong),
        ("pbMacContext", ctypes.c_void_p), ("cbMacContext", ctypes.c_ulong),
        ("cbAAD", ctypes.c_ulong), ("cbData", ctypes.c_ulonglong),
        ("dwFlags", ctypes.c_ulong),
    ]


def aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes) -> bytes:
    """AES-256-GCM decryption via Windows CNG bcrypt.dll."""
    bcrypt = ctypes.windll.bcrypt

    alg_handle = ctypes.c_void_p()
    status = bcrypt.BCryptOpenAlgorithmProvider(
        ctypes.byref(alg_handle), ctypes.c_wchar_p("AES"), None, 0)
    if status != 0:
        raise RuntimeError(f"BCryptOpenAlgorithmProvider: {status:#010x}")

    try:
        mode = "ChainingModeGCM\0".encode("utf-16-le")
        mode_buf = (ctypes.c_byte * len(mode))(*mode)
        bcrypt.BCryptSetProperty(alg_handle, ctypes.c_wchar_p("ChainingMode"),
                                 mode_buf, len(mode), 0)

        key_handle = ctypes.c_void_p()
        key_buf = (ctypes.c_byte * len(key))(*key)
        bcrypt.BCryptGenerateSymmetricKey(alg_handle, ctypes.byref(key_handle),
                                          None, 0, key_buf, len(key), 0)
        try:
            nonce_buf = (ctypes.c_byte * len(nonce))(*nonce)
            tag_buf = (ctypes.c_byte * len(tag))(*tag)

            auth = BCRYPT_AUTH_MODE_INFO()
            auth.cbSize = ctypes.sizeof(BCRYPT_AUTH_MODE_INFO)
            auth.dwInfoVersion = 1
            auth.pbNonce = ctypes.cast(nonce_buf, ctypes.c_void_p)
            auth.cbNonce = len(nonce)
            auth.pbTag = ctypes.cast(tag_buf, ctypes.c_void_p)
            auth.cbTag = len(tag)

            ct_buf = (ctypes.c_byte * len(ciphertext))(*ciphertext)
            pt_buf = (ctypes.c_byte * len(ciphertext))()
            pt_len = ctypes.c_ulong()

            status = bcrypt.BCryptDecrypt(key_handle, ct_buf, len(ciphertext),
                                          ctypes.byref(auth), None, 0,
                                          pt_buf, len(ciphertext),
                                          ctypes.byref(pt_len), 0)
            if status != 0:
                raise RuntimeError(f"BCryptDecrypt: {status:#010x}")
            return bytes(pt_buf[:pt_len.value])
        finally:
            bcrypt.BCryptDestroyKey(key_handle)
    finally:
        bcrypt.BCryptCloseAlgorithmProvider(alg_handle, 0)


def get_chromium_key(user_data: Path) -> bytes:
    """Retrieve encryption key from Local State."""
    with open(user_data / "Local State", encoding="utf-8") as f:
        state = json.load(f)
    enc_key = base64.b64decode(state["os_crypt"]["encrypted_key"])
    if enc_key[:5] == b"DPAPI":
        enc_key = enc_key[5:]
    return dpapi_decrypt(enc_key)


def decrypt_cookie_value(encrypted: bytes, key: bytes) -> str:
    if not encrypted:
        return ""
    if encrypted[:3] in (b"v10", b"v20"):
        nonce = encrypted[3:15]
        ct_tag = encrypted[15:]
        if len(ct_tag) < 16:
            return ""
        try:
            return aes_gcm_decrypt(key, nonce, ct_tag[:-16], ct_tag[-16:]).decode("utf-8", errors="replace")
        except RuntimeError:
            return ""
    try:
        return dpapi_decrypt(encrypted).decode("utf-8", errors="replace")
    except RuntimeError:
        return ""


def extract_from_db(browser_key: str, browsers: dict) -> list[dict]:
    """Read cookies directly from browser's cookie DB (browser must be closed)."""
    if browser_key not in browsers:
        raise RuntimeError(msg("err_browser_key_not_found", browser=browser_key))

    info = browsers[browser_key]
    user_data = info["user_data"]

    # Find cookie DB
    cookies_db = None
    for profile in ["Default", "Profile 1", "Profile 2", "Profile 3"]:
        for subpath in ["Network/Cookies", "Cookies"]:
            p = user_data / profile / subpath
            if p.exists():
                cookies_db = p
                break
        if cookies_db:
            break

    if not cookies_db:
        raise RuntimeError(msg("err_cookies_db_not_found", path=user_data))

    # Get encryption key
    key = get_chromium_key(user_data)

    # Copy DB and read
    tmp = Path(tempfile.gettempdir()) / "nlm_cookies_extract.db"
    try:
        shutil.copy2(cookies_db, tmp)
    except OSError as e:
        raise RuntimeError(msg("err_cookies_db_locked", name=info["name"], error=e))

    conn = sqlite3.connect(str(tmp))
    try:
        rows = conn.execute(
            "SELECT host_key, name, encrypted_value, path, expires_utc, is_secure "
            "FROM cookies WHERE host_key LIKE '%.google.com'"
        ).fetchall()
    finally:
        conn.close()
        tmp.unlink(missing_ok=True)

    cookies = []
    for host_key, name, enc_value, path, expires_utc, is_secure in rows:
        value = decrypt_cookie_value(enc_value, key)
        if not value:
            continue
        expires = 0
        if expires_utc and expires_utc > 0:
            expires = int((expires_utc / 1_000_000) - 11644473600)
        cookies.append({
            "name": name, "value": value, "domain": host_key,
            "path": path, "expires": expires, "secure": bool(is_secure),
        })
    return cookies


# ---------------------------------------------------------------------------
# Common
# ---------------------------------------------------------------------------

def validate_cookies(cookies: list[dict]) -> bool:
    return REQUIRED_COOKIES.issubset({c["name"] for c in cookies})


def save_cookies(cookies: list[dict], output_path: Path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)

    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass

    tmp_path.replace(output_path)
    try:
        os.chmod(output_path, 0o600)
    except OSError:
        pass


def check_auth(cookies_path: Path) -> bool:
    if not cookies_path.exists():
        print(msg("cookies_file_not_found", path=cookies_path))
        return False
    try:
        from notebooklm_client import NotebookLMClient
        client = NotebookLMClient(cookies_path=cookies_path)
        notebooks = client.list_notebooks()
        print(msg("auth_ok", count=len(notebooks)))
        return True
    except Exception as e:
        print(msg("auth_error", error_type=type(e).__name__, error=e))
        return False


def select_browser(browsers: dict, preferred: str | None = None) -> str:
    if preferred:
        if preferred in browsers:
            return preferred
        print(msg("err_browser_not_found", browser=preferred))
        print(msg("available_browsers", browsers=", ".join(browsers.keys())))
        sys.exit(1)

    priority = ["edge", "chrome", "brave", "firefox"]
    if sys.platform != "win32":
        priority = ["firefox", "chrome", "brave", "edge"]

    for key in priority:
        if key in browsers:
            return key
    return next(iter(browsers))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="nlm-login",
        description=msg("cli_description"),
        epilog=msg("cli_epilog"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--browser", "-b", help=msg("help_browser"))
    parser.add_argument("--extract", action="store_true",
                        help=msg("help_extract"))
    parser.add_argument("--check", action="store_true", help=msg("help_check"))
    parser.add_argument("--list-browsers", action="store_true", help=msg("help_list_browsers"))
    parser.add_argument(
        "--firefox-profile",
        type=str,
        default=None,
        help=msg("help_firefox_profile"),
    )
    parser.add_argument("--output", "-o", type=str, default=None, help=msg("help_output"))
    args = parser.parse_args()

    output_path = Path(args.output) if args.output else DEFAULT_COOKIES_PATH

    if args.check:
        ok = check_auth(output_path)
        sys.exit(0 if ok else 1)

    if args.extract and sys.platform != "win32":
        print(msg("err_extract_windows_only"))
        sys.exit(1)

    browsers = detect_browsers()
    if not browsers:
        print(msg("err_no_browser"))
        sys.exit(1)

    if args.list_browsers:
        print(msg("detected_browsers"))
        for key, info in browsers.items():
            print(f"  {key:>10}  {info['name']}")
            print(f"             {info['exe']}")
            if key == "firefox":
                profile_dir = info.get("profile_dir")
                if profile_dir:
                    print(f"             profile: {profile_dir}")
                else:
                    print(f"             profile: {msg('profile_not_detected')}")
        return

    browser_key = select_browser(browsers, args.browser)
    info = browsers[browser_key]

    # Retrieve cookies
    if browser_key == "firefox":
        if args.firefox_profile:
            info = dict(info)
            info["profile_dir"] = Path(args.firefox_profile).expanduser()
        try:
            cookies = firefox_login(info)
        except RuntimeError as e:
            print(f"\n[ERROR] {e}")
            sys.exit(1)
    elif args.extract:
        print(msg("extract_reading", name=info["name"]))
        try:
            cookies = extract_from_db(browser_key, browsers)
        except RuntimeError as e:
            print(f"\n[ERROR] {e}")
            sys.exit(1)
    else:
        cookies = cdp_login(browser_key, browsers, output_path)

    if not cookies:
        print(msg("err_no_cookies"))
        sys.exit(1)

    if not validate_cookies(cookies):
        missing = REQUIRED_COOKIES - {c["name"] for c in cookies}
        print(msg("err_missing_cookies", missing=", ".join(missing)))
        sys.exit(1)

    save_cookies(cookies, output_path)
    print(msg("cookies_saved", path=output_path, count=len(cookies)))

    print(msg("verifying_auth"))
    if check_auth(output_path):
        print(msg("setup_complete"))
    else:
        print(msg("warn_auth_failed"))
        sys.exit(1)


if __name__ == "__main__":
    main()
