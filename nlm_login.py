"""
nlm_login.py: NotebookLM 認証ツール

ブラウザを起動して Google ログイン → クッキーを自動取得。
Chrome, Edge, Brave, Firefox に対応。
外部パッケージ依存ゼロ。

Usage:
    python nlm_login.py                  # OSに応じて既定ブラウザ候補でログイン
    python nlm_login.py --browser chrome # Chrome でログイン
    python nlm_login.py --browser firefox # Firefox でログイン
    python nlm_login.py --browser firefox --firefox-profile ~/.mozilla/firefox/xxxx.default-release
    python nlm_login.py --extract        # ブラウザ終了状態でDB直接読み取り
    python nlm_login.py --check          # 認証状態を確認
    python nlm_login.py --list-browsers  # 検出されたブラウザ一覧
"""

import argparse
import base64
import configparser
import ctypes
import ctypes.wintypes
import json
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
# ブラウザ検出
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
    """ブラウザ実行ファイルを探す"""
    info = BROWSER_REGISTRY.get(browser_key)
    if not info:
        return None

    # PATH から探す
    for name in info["exe_names"]:
        found = shutil.which(name)
        if found:
            return found

    # Windows レジストリから探す
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, info["registry_key"])
        value, _ = winreg.QueryValueEx(key, "")
        winreg.CloseKey(key)
        if Path(value).exists():
            return value
    except (OSError, ImportError):
        pass

    # 既知のパスから探す（環境によっては無効なパス文字列で OSError になるため保護）
    for path in info["known_paths"]:
        try:
            if Path(path).exists():
                return path
        except OSError:
            continue

    return None


def detect_browsers() -> dict[str, dict]:
    """利用可能なブラウザを検出"""
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

        # profiles.ini が無い場合のフォールバック（snap/portable 等）
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
# 簡易 WebSocket クライアント（CDP 用、stdlib のみ）
# ---------------------------------------------------------------------------

class SimpleWebSocket:
    """CDP 通信用の最小 WebSocket クライアント"""

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
        if opcode == 9:  # ping → pong
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
# CDP ログイン（メイン方式）
# ---------------------------------------------------------------------------

def cdp_login(browser_key: str, browsers: dict, output_path: Path) -> list[dict]:
    """ブラウザを起動してCDP経由でクッキーを取得"""
    if browser_key not in browsers:
        print(f"[ERROR] {browser_key} が見つかりません。")
        print(f"  利用可能: {', '.join(browsers.keys())}")
        sys.exit(1)

    info = browsers[browser_key]
    exe = info["exe"]
    tmpdir = tempfile.mkdtemp(prefix="nlm_login_")

    print(f"\n{info['name']} を起動します...")

    # ブラウザ起動（一時プロファイル + デバッグポート）
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

    print(f"\n  ブラウザで Google アカウントにログインしてください。")
    print(f"  NotebookLM のホーム画面が表示されたら、")

    try:
        input("  ここに戻って Enter を押してください > ")
    except (EOFError, KeyboardInterrupt):
        proc.terminate()
        shutil.rmtree(tmpdir, ignore_errors=True)
        print("\nキャンセルしました。")
        sys.exit(0)

    # CDP でクッキー取得
    print("\n  クッキーを取得中...")
    try:
        cookies = get_cookies_via_cdp()
    except Exception as e:
        print(f"  [ERROR] CDP 通信に失敗: {e}")
        cookies = []
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        # 少し待ってから一時フォルダ削除
        time.sleep(1)
        shutil.rmtree(tmpdir, ignore_errors=True)

    return cookies


def extract_firefox_cookies(profile_dir: Path) -> list[dict]:
    """Firefox プロファイルの cookies.sqlite から Google クッキーを取得"""
    cookies_db = profile_dir / "cookies.sqlite"
    if not cookies_db.exists():
        raise RuntimeError(f"Firefox の cookies.sqlite が見つかりません: {cookies_db}")

    query = (
        "SELECT host, name, value, path, expiry, isSecure "
        "FROM moz_cookies "
        "WHERE host LIKE '%.google.com' OR host='google.com' OR host='notebooklm.google.com'"
    )

    tmp_dir = Path(tempfile.mkdtemp(prefix="nlm_firefox_cookies_"))
    tmp_db = tmp_dir / "cookies.sqlite"

    try:
        shutil.copy2(cookies_db, tmp_db)
        # WAL モードで稼働中の場合に備えて付随ファイルもコピー
        for suffix in ("-wal", "-shm"):
            src = profile_dir / f"cookies.sqlite{suffix}"
            if src.exists():
                shutil.copy2(src, tmp_dir / src.name)
    except OSError as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"Firefox クッキーDBのコピーに失敗: {e}")

    conn = None
    try:
        conn = sqlite3.connect(str(tmp_db))
        rows = conn.execute(query).fetchall()
    except sqlite3.Error as e:
        raise RuntimeError(f"Firefox クッキーDBの読み取りに失敗: {e}")
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
    """Firefox を開いてログインさせた後に、プロファイルDBからクッキーを取得"""
    profile_dir = info.get("profile_dir")
    temp_profile_dir: Path | None = None
    firefox_proc: subprocess.Popen | None = None
    use_temp_profile = profile_dir is None

    if use_temp_profile:
        # 初回起動前などで既存プロファイルが見つからない場合に備えて、
        # 一時プロファイルを作ってログインさせ、そこからクッキーを読む。
        temp_profile_dir = Path(tempfile.mkdtemp(prefix="nlm_firefox_profile_"))
        profile_dir = temp_profile_dir

    print(f"\n{info['name']} を起動します...")
    try:
        if use_temp_profile:
            print("  既存 Firefox プロファイルが未検出のため、一時プロファイルで起動します。")
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
        print(f"\n  [WARN] ブラウザ起動に失敗: {e}")
        print("  手動で Firefox を開いて NotebookLM にログインしてください。")

    print("\n  Firefox で NotebookLM にログインしてください。")
    if use_temp_profile:
        print("  ログイン後、この Firefox ウィンドウを閉じてから Enter を押してください。")
    try:
        input("  ログイン後、ここに戻って Enter を押してください > ")
    except (EOFError, KeyboardInterrupt):
        if firefox_proc and firefox_proc.poll() is None:
            firefox_proc.terminate()
            try:
                firefox_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                firefox_proc.kill()
        if temp_profile_dir:
            shutil.rmtree(temp_profile_dir, ignore_errors=True)
        print("\nキャンセルしました。")
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
    """CDP コマンドを送信してレスポンスを待つ"""
    cmd = {"id": msg_id, "method": method}
    if params:
        cmd["params"] = params
    ws.send(json.dumps(cmd))

    for _ in range(50):
        raw = ws.recv()
        result = json.loads(raw)
        if result.get("id") == msg_id:
            return result
    raise RuntimeError(f"CDP レスポンスがタイムアウト: {method}")


def get_cookies_via_cdp() -> list[dict]:
    """CDP WebSocket で全クッキーを取得"""
    # ページタブの WebSocket を取得
    resp = urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json", timeout=5)
    tabs = json.loads(resp.read())

    # NotebookLM または Google のページタブを探す
    ws_url = None
    for tab in tabs:
        if tab.get("type") != "page":
            continue
        url = tab.get("url", "")
        if "notebooklm" in url or "google.com" in url:
            ws_url = tab.get("webSocketDebuggerUrl")
            break
    if not ws_url:
        # 最初のページタブを使う
        for tab in tabs:
            if tab.get("type") == "page" and tab.get("webSocketDebuggerUrl"):
                ws_url = tab["webSocketDebuggerUrl"]
                break
    if not ws_url:
        raise RuntimeError("ページタブが見つかりません")

    ws = SimpleWebSocket(ws_url)
    try:
        # Network ドメインを有効化してからクッキー取得
        _cdp_send_recv(ws, 1, "Network.enable")
        result = _cdp_send_recv(ws, 2, "Network.getAllCookies")
    finally:
        ws.close()

    # Google クッキーをフィルタリング
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
# DB 直接読み取り（ブラウザ終了時のみ有効）
# ---------------------------------------------------------------------------

class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def dpapi_decrypt(encrypted: bytes) -> bytes:
    """Windows DPAPI で復号"""
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
    """AES-256-GCM 復号 (Windows CNG bcrypt.dll)"""
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
    """Local State から暗号化キーを取得"""
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
    """ブラウザのクッキーDBから直接読み取り（ブラウザ終了が必要）"""
    if browser_key not in browsers:
        raise RuntimeError(f"{browser_key} が見つかりません")

    info = browsers[browser_key]
    user_data = info["user_data"]

    # クッキーDBを探す
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
        raise RuntimeError(f"クッキーDBが見つかりません: {user_data}")

    # 暗号化キー取得
    key = get_chromium_key(user_data)

    # DBをコピーして読む
    tmp = Path(tempfile.gettempdir()) / "nlm_cookies_extract.db"
    try:
        shutil.copy2(cookies_db, tmp)
    except OSError as e:
        raise RuntimeError(
            f"クッキーDBがロックされています。\n"
            f"  {info['name']} を終了してから再実行してください。\n"
            f"  または python nlm_login.py (CDP方式) を使ってください。\n"
            f"  元のエラー: {e}"
        )

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
# 共通処理
# ---------------------------------------------------------------------------

def validate_cookies(cookies: list[dict]) -> bool:
    return REQUIRED_COOKIES.issubset({c["name"] for c in cookies})


def save_cookies(cookies: list[dict], output_path: Path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)

    # 可能な環境では最小権限に寄せる（Windows では ACL までは制御しない）
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
        print(f"クッキーファイルが見つかりません: {cookies_path}")
        return False
    try:
        from notebooklm_client import NotebookLMClient
        client = NotebookLMClient(cookies_path=cookies_path)
        notebooks = client.list_notebooks()
        print(f"認証OK - {len(notebooks)} 件のノートブックにアクセスできます。")
        return True
    except Exception as e:
        print(f"認証エラー ({type(e).__name__}): {e}")
        return False


def select_browser(browsers: dict, preferred: str | None = None) -> str:
    if preferred:
        if preferred in browsers:
            return preferred
        print(f"[ERROR] '{preferred}' が見つかりません。")
        print(f"  利用可能: {', '.join(browsers.keys())}")
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
        description="NotebookLM 認証ツール - ブラウザからクッキーを取得",
        epilog="例:\n"
               "  python nlm_login.py              # 既定ブラウザ候補でログイン\n"
               "  python nlm_login.py -b chrome     # Chrome でログイン\n"
               "  python nlm_login.py -b firefox    # Firefox でログイン\n"
               "  python nlm_login.py -b firefox --firefox-profile ~/.mozilla/firefox/xxxx.default-release\n"
               "  python nlm_login.py --extract     # DB直接読み取り(ブラウザ終了必要)\n"
               "  python nlm_login.py --check       # 認証確認\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--browser", "-b", help="ブラウザを指定 (edge/chrome/brave/firefox)")
    parser.add_argument("--extract", action="store_true",
                        help="DB直接読み取り (ブラウザ終了が必要)")
    parser.add_argument("--check", action="store_true", help="認証状態を確認")
    parser.add_argument("--list-browsers", action="store_true", help="検出ブラウザ一覧")
    parser.add_argument(
        "--firefox-profile",
        type=str,
        default=None,
        help="Firefox プロファイルディレクトリ (cookies.sqlite があるパス)",
    )
    parser.add_argument("--output", "-o", type=str, default=None, help="クッキー出力先")
    args = parser.parse_args()

    output_path = Path(args.output) if args.output else DEFAULT_COOKIES_PATH

    if args.check:
        ok = check_auth(output_path)
        sys.exit(0 if ok else 1)

    if args.extract and sys.platform != "win32":
        print("[ERROR] --extract は現在 Windows でのみサポートしています。")
        print("  Linux/macOS では通常モード（CDP方式）を使用してください。")
        sys.exit(1)

    browsers = detect_browsers()
    if not browsers:
        print("[ERROR] 対応ブラウザが見つかりません (Edge, Chrome, Brave, Firefox)")
        sys.exit(1)

    if args.list_browsers:
        print("\n検出されたブラウザ:")
        for key, info in browsers.items():
            print(f"  {key:>10}  {info['name']}")
            print(f"             {info['exe']}")
            if key == "firefox":
                profile_dir = info.get("profile_dir")
                if profile_dir:
                    print(f"             profile: {profile_dir}")
                else:
                    print("             profile: (未検出: 実行時に一時プロファイルでフォールバック)")
        return

    browser_key = select_browser(browsers, args.browser)
    info = browsers[browser_key]

    # クッキー取得
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
        print(f"\n{info['name']} のクッキーDB から直接読み取ります...")
        try:
            cookies = extract_from_db(browser_key, browsers)
        except RuntimeError as e:
            print(f"\n[ERROR] {e}")
            sys.exit(1)
    else:
        cookies = cdp_login(browser_key, browsers, output_path)

    if not cookies:
        print("\n[ERROR] Google クッキーが取得できませんでした。")
        print("  NotebookLM にログインしてから再実行してください。")
        sys.exit(1)

    if not validate_cookies(cookies):
        missing = REQUIRED_COOKIES - {c["name"] for c in cookies}
        print(f"\n[ERROR] 必要なクッキーが不足: {', '.join(missing)}")
        print("  NotebookLM にログインしてから再実行してください。")
        sys.exit(1)

    save_cookies(cookies, output_path)
    print(f"\n  クッキー保存完了 → {output_path}")
    print(f"  ({len(cookies)} 件の Google クッキー)")

    print("\n認証を確認中...")
    if check_auth(output_path):
        print("\nセットアップ完了! nlm-backup / nlm-upload が使えます。")
    else:
        print("\n[WARN] 認証に失敗しました。再度ログインしてください。")
        sys.exit(1)


if __name__ == "__main__":
    main()
