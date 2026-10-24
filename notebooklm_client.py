"""
NotebookLM API Client

Google NotebookLM の内部 batchexecute API を直接操作する軽量クライアント。
Python 標準ライブラリのみで動作し、外部パッケージ依存はゼロ。

認証には nlm login で作成されたクッキーファイル、または手動エクスポートした
クッキーを使用します。
"""

import contextlib
import http.client
import http.cookiejar
import html as html_lib
import json
import mimetypes
import os
import re
import ssl
import tempfile
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://notebook.google.com"
LEGACY_BASE_URL = "https://notebooklm.google.com"
BASE_URL = os.environ.get("NOTEBOOKLM_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
if BASE_URL not in (DEFAULT_BASE_URL, LEGACY_BASE_URL):
    raise ValueError(
        "NOTEBOOKLM_BASE_URL must be https://notebook.google.com "
        "or https://notebooklm.google.com"
    )
BATCHEXECUTE_URL = f"{BASE_URL}/_/LabsTailwindUi/data/batchexecute"

# Keep the web upload data plane independent from the rebranded RPC host.
# The legacy consumer upload host is the live-observed path; callers can opt
# into the new host explicitly once their account cohort supports it.
UPLOAD_BASE_URL = os.environ.get("NOTEBOOKLM_UPLOAD_BASE_URL", LEGACY_BASE_URL).rstrip("/")
if UPLOAD_BASE_URL not in (DEFAULT_BASE_URL, LEGACY_BASE_URL):
    raise ValueError(
        "NOTEBOOKLM_UPLOAD_BASE_URL must be https://notebook.google.com "
        "or https://notebooklm.google.com"
    )
UPLOAD_URL = f"{UPLOAD_BASE_URL}/upload/_/?authuser=0"
DEFAULT_BUILD_LABEL = "boq_labs-tailwind-frontend_20260108.06_p0"
BUILD_LABEL_PATTERN = re.compile(r"\bboq_[A-Za-z0-9_-]+_[0-9]{8}\.[0-9]+_p[0-9]+\b")

DEFAULT_COOKIES_PATH = Path.home() / ".notebooklm-mcp-cli" / "profiles" / "default" / "cookies.json"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
DOWNLOAD_SOCKET_TIMEOUT_SECONDS = 30
DOWNLOAD_TOTAL_TIMEOUT_SECONDS = 10 * 60
DOWNLOAD_CHUNK_BYTES = 256 * 1024

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
)

# Source type codes from the current Gemini Notebook web contract.
# Unknown future values intentionally remain "unknown" instead of being guessed.
SOURCE_TYPES = {
    1: "google_docs",
    2: "google_slides",
    3: "pdf",
    4: "pasted_text",
    5: "web_page",
    6: "powerpoint",
    7: "google_spreadsheet",
    8: "markdown",
    9: "youtube",
    10: "media",
    11: "docx",
    12: "excel",
    13: "image",
    14: "google_drive",
    15: "gmail",
    16: "csv",
    17: "epub",
    18: "gemini_chat",
    19: "ai_mode_chat",
    20: "expert_intelligence",
}

SOURCE_STATUS = {
    0: "unknown",
    1: "pending",
    2: "ready",
    3: "error",
    4: "pending_deletion",
    5: "preparing",
}

_TYPE_CODE_14_MIME_OVERRIDE = {
    "application/pdf": 3,
    "application/vnd.google-apps.spreadsheet": 7,
}

# Artifact type codes
ARTIFACT_TYPES = {
    1: "audio_overview",
    2: "report",
    3: "video_overview",
    4: "flashcards",  # quiz / flashcards / interactive mind map use variants
    5: "mind_map",
    6: "fantasy_map",
    7: "infographic",
    8: "slide_deck",
    9: "data_table",
    10: "file",
    11: "guided_view",
}

ARTIFACT_STATUS = {
    0: "unknown",
    1: "pending",
    2: "in_progress",
    3: "completed",
    4: "failed",
    5: "suggested",
    6: "pending_review",
}

APP_ARTIFACT_TYPES = {
    1: "flashcards",
    2: "quiz",
    3: "prototype",
    4: "interactive_mind_map",
    5: "interactive_learning_overview",
}

VIDEO_OVERVIEW_STYLES = {
    1: "explainer",
    2: "short",
    3: "cinematic",
}

GENERATED_FILE_MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/markdown": ".md",
    "text/x-markdown": ".md",
    "text/plain": ".txt",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/csv": ".csv",
    "application/csv": ".csv",
    "application/json": ".json",
    "text/json": ".json",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}

SUPPORTED_GENERATED_FILE_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".md",
    ".markdown",
    ".txt",
    ".xlsx",
    ".pptx",
    ".csv",
    ".json",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
}

BINARY_GENERATED_FILE_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
}

TEXT_GENERATED_FILE_EXTENSIONS = {
    ".md",
    ".markdown",
    ".txt",
    ".csv",
    ".json",
    ".svg",
}

_SENSITIVE_QUERY_KEYS = frozenset({
    "key",
    "token",
    "access_token",
    "auth",
    "sig",
    "signature",
    "x-goog-signature",
    "x-goog-credential",
    "x-goog-security-token",
    "sid",
    "session",
    "cookie",
})

_SENSITIVE_DICT_KEYS = frozenset({
    "cookies",
    "cookie",
    "csrf_token",
    "session_id",
    "authorization",
    "x-goog-authuser",
})

_CREDENTIAL_STRING_PATTERN = re.compile(
    r"(?:^|[;\s])(?:SAPISID|APISID|HSID|SSID|SID|__Secure-1PSID|__Secure-3PSID|__Secure-1PAPISID|__Secure-3PAPISID)=[^\s;]+",
    re.IGNORECASE,
)


def sanitize_for_backup(value):
    """Redact signed capability URLs, auth query tokens, and cookies from raw backup payloads."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("http://", "https://")):
            try:
                parsed = urllib.parse.urlsplit(stripped)
                host = (parsed.hostname or "").lower()
                if host == "googleusercontent.com" or host.endswith(".googleusercontent.com") or host.endswith(".usercontent.google.com"):
                    return f"{parsed.scheme}://{parsed.netloc}/[REDACTED_SIGNED_URL]"
                if parsed.query:
                    q_items = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
                    redacted_any = False
                    new_items = []
                    for k, v in q_items:
                        if k.lower() in _SENSITIVE_QUERY_KEYS:
                            new_items.append((k, "[REDACTED]"))
                            redacted_any = True
                        else:
                            new_items.append((k, v))
                    if redacted_any:
                        new_query = urllib.parse.urlencode(new_items)
                        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))
            except ValueError:
                pass
        if _CREDENTIAL_STRING_PATTERN.search(value):
            return _CREDENTIAL_STRING_PATTERN.sub(" [REDACTED_CREDENTIAL]", value).strip()
        return value
    if isinstance(value, list):
        return [sanitize_for_backup(item) for item in value]
    if isinstance(value, dict):
        sanitized = {}
        for k, v in value.items():
            key_lower = str(k).lower()
            if key_lower in _SENSITIVE_DICT_KEYS:
                sanitized[k] = "[REDACTED]"
            else:
                sanitized[k] = sanitize_for_backup(v)
        return sanitized
    return value

APP_ARTIFACT_DATA_PATTERN = re.compile(
    r"<app-root\b[^>]*\bdata-app-data=\"(.*?)\"",
    re.IGNORECASE | re.DOTALL,
)


class NotebookLMError(Exception):
    pass


class AuthenticationError(NotebookLMError):
    pass


def _template_block() -> list:
    """Gemini Notebook current request-options wrapper."""
    return [
        2,
        None,
        None,
        [1, None, None, None, None, None, None, None, None, None, [1]],
    ]


def _trusted_upload_origin(url: str) -> str:
    """Validate a server-named resumable upload URL and return its HTTPS origin."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "notebook.google.com",
        "notebooklm.google.com",
    }:
        raise NotebookLMError("Unexpected resumable upload URL origin")
    if parsed.username or parsed.password:
        raise NotebookLMError("Unexpected credentials in resumable upload URL")
    if not parsed.path.startswith("/upload/_/"):
        raise NotebookLMError("Unexpected resumable upload URL path")
    return f"https://{parsed.netloc}"


def _normalized_mime(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.split(";", 1)[0].strip().lower()


def _disambiguate_source_type_code(
    type_code: int | None,
    content_mime: str | None,
    drive_mime: str | None,
) -> int | None:
    """Refine the overloaded Drive code 14 using live-observed MIME evidence."""
    if type_code != 14:
        return type_code
    for candidate in (content_mime, drive_mime):
        normalized = _normalized_mime(candidate)
        if normalized in _TYPE_CODE_14_MIME_OVERRIDE:
            return _TYPE_CODE_14_MIME_OVERRIDE[normalized]
    return type_code


def _source_id_from_raw(raw_id) -> str | None:
    """Decode regular and Drive-backed source-id envelopes."""
    if isinstance(raw_id, str) and raw_id:
        return raw_id
    if not isinstance(raw_id, list) or not raw_id:
        return None
    if isinstance(raw_id[0], str) and raw_id[0]:
        return raw_id[0]
    if (
        len(raw_id) >= 3
        and raw_id[0] is None
        and isinstance(raw_id[2], list)
        and raw_id[2]
        and isinstance(raw_id[2][0], str)
    ):
        return raw_id[2][0]
    return None


def _is_youtube_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url.strip()).hostname or "").lower()
    except ValueError:
        return False
    return host in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }


class NotebookLMClient:
    """NotebookLM batchexecute API クライアント"""

    def __init__(self, cookies_path: str | Path | None = None):
        self._cookies_path = Path(cookies_path) if cookies_path else DEFAULT_COOKIES_PATH
        self._csrf_token: str | None = None
        self._session_id: str | None = None
        self._build_label: str = DEFAULT_BUILD_LABEL
        self._cookie_jar = http.cookiejar.CookieJar()
        self._opener: urllib.request.OpenerDirector | None = None

        self._load_cookies()
        self._refresh_tokens()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _load_cookies(self):
        """クッキーファイルを読み込んで cookie jar にセット"""
        if not self._cookies_path.exists():
            raise AuthenticationError(
                f"クッキーファイルが見つかりません: {self._cookies_path}\n"
                "nlm login を実行するか、クッキーを手動でエクスポートしてください。"
            )

        with open(self._cookies_path, encoding="utf-8") as f:
            cookies = json.load(f)

        for c in cookies:
            name = c.get("name", "")
            value = c.get("value", "")
            domain = c.get("domain", ".google.com")
            path = c.get("path", "/")
            secure = c.get("secure", True)
            try:
                expires_raw = c.get("expires", 0)
                expires = int(float(expires_raw)) or None
            except (TypeError, ValueError):
                expires = None

            # .google.com 用
            cookie = http.cookiejar.Cookie(
                version=0, name=name, value=value,
                port=None, port_specified=False,
                domain=domain, domain_specified=True, domain_initial_dot=domain.startswith("."),
                path=path, path_specified=True,
                secure=secure, expires=expires, discard=expires is None,
                comment=None, comment_url=None, rest={}, rfc2109=False,
            )
            self._cookie_jar.set_cookie(cookie)

            # .googleusercontent.com 用（ダウンロードリダイレクト対応）
            if domain in (".google.com", "notebook.google.com", "notebooklm.google.com"):
                gu_cookie = http.cookiejar.Cookie(
                    version=0, name=name, value=value,
                    port=None, port_specified=False,
                    domain=".googleusercontent.com", domain_specified=True, domain_initial_dot=True,
                    path="/", path_specified=True,
                    secure=secure, expires=expires, discard=expires is None,
                    comment=None, comment_url=None, rest={}, rfc2109=False,
                )
                self._cookie_jar.set_cookie(gu_cookie)

        # SSL コンテキストとopener構築
        ctx = ssl.create_default_context()
        https_handler = urllib.request.HTTPSHandler(context=ctx)
        cookie_handler = urllib.request.HTTPCookieProcessor(self._cookie_jar)
        self._opener = urllib.request.build_opener(https_handler, cookie_handler)

    def _refresh_tokens(self):
        """NotebookLM トップページから CSRF トークンとセッションIDを取得"""
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        req = urllib.request.Request(BASE_URL + "/", headers=headers)
        resp = self._opener.open(req)
        html = resp.read().decode("utf-8", errors="replace")

        # リダイレクトチェック（accounts.google.com に飛ばされた場合は認証切れ）
        if "accounts.google.com" in resp.url:
            raise AuthenticationError(
                "認証が期限切れです。nlm login を再実行してください。"
            )

        csrf_match = re.search(r'"SNlM0e":"([^"]+)"', html)
        if csrf_match:
            self._csrf_token = csrf_match.group(1)

        sid_match = re.search(r'"FdrFJe":"([^"]+)"', html)
        if sid_match:
            self._session_id = sid_match.group(1)

        build_label = self._extract_build_label(html)
        if build_label:
            self._build_label = build_label

        if not self._csrf_token:
            raise AuthenticationError(
                "CSRFトークンの取得に失敗しました。認証が切れている可能性があります。\n"
                "nlm login を再実行してください。"
            )

    def _extract_build_label(self, html: str) -> str | None:
        """トップページHTMLから現在の build label (bl) を抽出"""
        cfb2h_match = re.search(r'"cfb2h":"([^"]+)"', html)
        if cfb2h_match and cfb2h_match.group(1).startswith("boq_"):
            return cfb2h_match.group(1)

        generic_match = BUILD_LABEL_PATTERN.search(html)
        if generic_match:
            return generic_match.group(0)
        return None

    # ------------------------------------------------------------------
    # batchexecute protocol
    # ------------------------------------------------------------------

    def _batchexecute(self, rpc_id: str, params: list, source_path: str = "/") -> list | dict | None:
        """batchexecute RPC を実行して結果を返す"""
        # リクエストボディ構築
        params_json = json.dumps(params, separators=(",", ":"))
        f_req = [[[rpc_id, params_json, None, "generic"]]]
        f_req_json = json.dumps(f_req, separators=(",", ":"))

        body = f"f.req={urllib.parse.quote(f_req_json, safe='')}"
        if self._csrf_token:
            body += f"&at={urllib.parse.quote(self._csrf_token, safe='')}"
        body += "&"

        # URL パラメータ
        query = urllib.parse.urlencode({
            "rpcids": rpc_id,
            "source-path": source_path,
            "bl": self._build_label,
            "hl": "en",
            "rt": "c",
            **({"f.sid": self._session_id} if self._session_id else {}),
        })
        url = f"{BATCHEXECUTE_URL}?{query}"

        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
            "User-Agent": USER_AGENT,
            "X-Same-Domain": "1",
        }
        if self._csrf_token:
            headers["X-Goog-Csrf-Token"] = self._csrf_token

        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")

        try:
            resp = self._opener.open(req)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthenticationError(f"認証エラー (HTTP {e.code})。nlm login を再実行してください。")
            raise NotebookLMError(f"HTTP Error {e.code}: {e.reason}")

        text = resp.read().decode("utf-8", errors="replace")
        return self._parse_response(text, rpc_id)

    def _parse_response(self, text: str, rpc_id: str) -> list | dict | None:
        """batchexecute レスポンスを解析"""
        # Anti-XSSI プレフィックス除去
        if text.startswith(")]}'"):
            text = text[4:]

        lines = text.strip().split("\n")
        chunks = []
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            try:
                int(line)  # byte count
                i += 1
                if i < len(lines):
                    try:
                        chunks.append(json.loads(lines[i]))
                    except json.JSONDecodeError:
                        pass
                i += 1
            except ValueError:
                try:
                    chunks.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
                i += 1

        # wrb.fr センチネルから結果を抽出
        for chunk in chunks:
            result = self._extract_wrb_fr(chunk, rpc_id)
            if result is not None:
                return result
        return None

    def _extract_wrb_fr(self, data, rpc_id: str):
        """再帰的に wrb.fr 結果を探す"""
        if not isinstance(data, list):
            return None
        for item in data:
            if isinstance(item, list) and len(item) >= 3:
                if item[0] == "wrb.fr" and item[1] == rpc_id:
                    # RPC Error 16 チェック
                    if len(item) > 5 and isinstance(item[5], list) and 16 in item[5]:
                        raise AuthenticationError("認証が期限切れです。nlm login を再実行してください。")
                    result_str = item[2]
                    if isinstance(result_str, str):
                        return json.loads(result_str)
                    return result_str
            if isinstance(item, list):
                result = self._extract_wrb_fr(item, rpc_id)
                if result is not None:
                    return result
        return None

    # ------------------------------------------------------------------
    # Notebooks
    # ------------------------------------------------------------------

    def list_notebooks(self) -> list[dict]:
        """ノートブック一覧を取得"""
        result = self._batchexecute("wXbhsf", [None, 1, None, [2]])
        if not result or not isinstance(result[0], list):
            return []

        notebooks = []
        for nb in result[0]:
            try:
                title = nb[0] or "Untitled"
                nb_id = nb[2]
                sources = nb[1] if isinstance(nb[1], list) else []
                meta = nb[5] if len(nb) > 5 and isinstance(nb[5], list) else []

                updated_ts = None
                if meta and len(meta) > 5 and isinstance(meta[5], list):
                    updated_ts = meta[5][0]

                notebooks.append({
                    "id": nb_id,
                    "title": title,
                    "source_count": len(sources),
                    "updated_at": updated_ts,
                })
            except (IndexError, TypeError):
                continue
        return notebooks

    def create_notebook(self, title: str) -> str:
        """ノートブックを作成してIDを返す"""
        params = [title, None, None, _template_block()]
        result = self._batchexecute("CCqFvf", params)
        if result and len(result) > 2:
            return result[2]
        raise NotebookLMError("ノートブック作成に失敗しました")

    def delete_notebook(self, notebook_id: str) -> bool:
        """ノートブックを削除"""
        self._batchexecute("WWINqb", [[notebook_id], [2]])
        return True

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------

    def list_sources(self, notebook_id: str) -> list[dict]:
        """ノートブック内のソース一覧を取得"""
        result = self._batchexecute(
            "rLM1Ne",
            [notebook_id, None, _template_block(), None, 0],
            source_path=f"/notebook/{notebook_id}",
        )
        if not result or not isinstance(result[0], list) or len(result[0]) < 2:
            return []

        raw_sources = result[0][1]
        if not isinstance(raw_sources, list):
            return []

        sources = []
        for src in raw_sources:
            try:
                src_id = _source_id_from_raw(src[0])
                if not src_id:
                    continue
                title = src[1] or "Untitled"
                meta = src[2] if len(src) > 2 and isinstance(src[2], list) else []

                # Current source rows carry status at source[3][1].
                status_code = None
                if (
                    len(src) > 3
                    and isinstance(src[3], list)
                    and len(src[3]) > 1
                    and isinstance(src[3][1], int)
                ):
                    status_code = src[3][1]
                status = SOURCE_STATUS.get(status_code, "unknown")

                # URL（Web/YouTube source）
                url = None
                if meta and len(meta) > 7 and isinstance(meta[7], list) and meta[7]:
                    url = meta[7][0]
                elif meta and len(meta) > 5 and isinstance(meta[5], list) and meta[5]:
                    url = meta[5][0]

                # Current source rows expose original uploaded-file download URL
                # at index 5 and the true content MIME at [7][2].
                download_url = None
                if len(src) > 5 and isinstance(src[5], str) and src[5].startswith("http"):
                    download_url = src[5]

                viewer_url = None
                if len(src) > 6 and isinstance(src[6], str) and src[6].startswith("http"):
                    viewer_url = src[6]

                content_mime = None
                if (
                    len(src) > 7
                    and isinstance(src[7], list)
                    and len(src[7]) > 2
                    and isinstance(src[7][2], str)
                ):
                    content_mime = src[7][2]

                drive_mime = None
                if len(meta) > 19 and isinstance(meta[19], str):
                    drive_mime = meta[19]
                elif (
                    len(meta) > 9
                    and isinstance(meta[9], list)
                    and len(meta[9]) > 2
                    and isinstance(meta[9][2], str)
                ):
                    drive_mime = meta[9][2]

                type_code = meta[4] if len(meta) > 4 and isinstance(meta[4], int) else None
                effective_type_code = _disambiguate_source_type_code(
                    type_code,
                    content_mime,
                    drive_mime,
                )
                source_type = SOURCE_TYPES.get(effective_type_code, "unknown")

                sources.append({
                    "id": src_id,
                    "title": title,
                    "type": source_type,
                    "type_code": effective_type_code,
                    "raw_type_code": type_code,
                    "status": status,
                    "status_code": status_code,
                    "url": url,
                    "download_url": download_url,
                    "viewer_url": viewer_url,
                    "content_mime": content_mime,
                    "drive_mime": drive_mime,
                    "_raw": src,
                })
            except (IndexError, TypeError):
                continue
        return sources

    def get_source_content(self, source_id: str) -> dict:
        """ソースの生コンテンツを取得"""
        result = self._batchexecute("hizoJc", [[source_id], [2], [2]])
        if not result:
            return {"content": "", "title": "", "source_type": "unknown"}

        # タイトル
        title = ""
        try:
            title = result[0][1] or ""
        except (IndexError, TypeError):
            pass

        # ソースタイプ（Drive code 14 は MIME で補正）
        source_type = "unknown"
        try:
            row = result[0]
            meta = row[2] if len(row) > 2 and isinstance(row[2], list) else []
            type_code = meta[4] if len(meta) > 4 and isinstance(meta[4], int) else None
            content_mime = (
                row[7][2]
                if len(row) > 7
                and isinstance(row[7], list)
                and len(row[7]) > 2
                and isinstance(row[7][2], str)
                else None
            )
            drive_mime = (
                meta[19]
                if len(meta) > 19 and isinstance(meta[19], str)
                else (
                    meta[9][2]
                    if len(meta) > 9
                    and isinstance(meta[9], list)
                    and len(meta[9]) > 2
                    and isinstance(meta[9][2], str)
                    else None
                )
            )
            effective_type_code = _disambiguate_source_type_code(
                type_code,
                content_mime,
                drive_mime,
            )
            source_type = SOURCE_TYPES.get(effective_type_code, "unknown")
        except (IndexError, TypeError):
            pass

        # コンテンツ抽出（テキストブロックから再帰的に文字列を集める）
        content_parts = []
        try:
            blocks = result[3][0] if result[3] else []
            self._extract_text_recursive(blocks, content_parts)
        except (IndexError, TypeError):
            pass

        content = "\n".join(content_parts)

        return {
            "content": content,
            "title": title,
            "source_type": source_type,
        }

    def wait_for_source_ready(
        self,
        notebook_id: str,
        source_id: str,
        timeout: float = 120.0,
        interval: float = 2.0,
    ) -> dict:
        """Poll a source until Gemini Notebook marks it ready or failed."""
        deadline = time.monotonic() + timeout
        last = None
        while True:
            for source in self.list_sources(notebook_id):
                if source.get("id") != source_id:
                    continue
                last = source
                status = source.get("status")
                # Older cohorts omitted the status slot entirely. If the row is
                # visible and there is no status code at all, preserve legacy
                # behavior instead of timing out forever. An unknown *present*
                # code remains non-terminal and will continue polling.
                if status == "ready" or source.get("status_code") is None:
                    return source
                if status in {"error", "pending_deletion"}:
                    raise NotebookLMError(
                        f"ソース処理に失敗しました: {source.get('title', source_id)} "
                        f"(status={status})"
                    )
                break
            if time.monotonic() >= deadline:
                status = last.get("status") if isinstance(last, dict) else "not-visible"
                raise NotebookLMError(
                    f"ソース準備がタイムアウトしました: {source_id} (status={status})"
                )
            time.sleep(max(0.1, interval))

    def _extract_text_recursive(self, data, parts: list):
        """ネストされた構造からテキスト文字列を再帰的に抽出"""
        if isinstance(data, str):
            if data.strip():
                parts.append(data)
        elif isinstance(data, list):
            for item in data:
                self._extract_text_recursive(item, parts)

    def add_source_url(self, notebook_id: str, url: str) -> str | None:
        """URL / YouTube ソースを追加"""
        if _is_youtube_url(url):
            source_data = [None, None, None, None, None, None, None, [url], None, None, 1]
        else:
            source_data = [None, None, [url], None, None, None, None, None, None, None, 1]
        params = [[source_data], notebook_id, _template_block()]
        result = self._batchexecute("izAoDd", params, source_path=f"/notebook/{notebook_id}")
        try:
            return result[0][0][0][0]
        except (IndexError, TypeError):
            return None

    def add_source_text(self, notebook_id: str, title: str, text: str) -> str | None:
        """テキストソースを追加"""
        source_data = [None, [title, text], None, 2, None, None, None, None, None, None, 1]
        params = [[source_data], notebook_id, _template_block()]
        result = self._batchexecute("izAoDd", params, source_path=f"/notebook/{notebook_id}")
        try:
            return result[0][0][0][0]
        except (IndexError, TypeError):
            return None

    def upload_file(self, notebook_id: str, file_path: str | Path) -> str | None:
        """ファイルをアップロード（3ステップ resumable upload）"""
        file_path = Path(file_path)
        if not file_path.exists():
            raise NotebookLMError(f"ファイルが見つかりません: {file_path}")

        filename = file_path.name
        file_size = file_path.stat().st_size
        if file_size <= 0:
            raise NotebookLMError(f"空ファイルはアップロードできません: {filename}")
        if file_size > MAX_UPLOAD_BYTES:
            raise NotebookLMError(
                f"ファイルが200 MiB上限を超えています: {filename} "
                f"({file_size} bytes)"
            )
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        # Step 1: ファイルソースを登録 → source_id 取得
        params = [[[filename]], notebook_id, _template_block()]
        result = self._batchexecute("o4cbdc", params, source_path=f"/notebook/{notebook_id}")
        source_id = self._extract_first_string(result)
        if not source_id:
            raise NotebookLMError("ファイル登録に失敗しました")

        # Step 2: resumable upload セッション開始
        upload_meta = json.dumps({
            "PROJECT_ID": notebook_id,
            "SOURCE_NAME": filename,
            "SOURCE_ID": source_id,
        })
        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": UPLOAD_BASE_URL,
            "Referer": f"{UPLOAD_BASE_URL}/",
            "User-Agent": USER_AGENT,
            "x-goog-authuser": "0",
            "x-goog-upload-command": "start",
            "x-goog-upload-header-content-length": str(file_size),
            "x-goog-upload-header-content-type": content_type,
            "x-goog-upload-protocol": "resumable",
        }
        req = urllib.request.Request(UPLOAD_URL, data=upload_meta.encode("utf-8"), headers=headers, method="POST")
        resp = self._opener.open(req)
        upload_url = resp.headers.get("x-goog-upload-url")
        if not upload_url:
            raise NotebookLMError("アップロードURLの取得に失敗しました")
        upload_origin = _trusted_upload_origin(upload_url)

        # Step 3: ファイルバイナリを送信（ストリーミング）
        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            "Content-Length": str(file_size),
            "Origin": upload_origin,
            "Referer": f"{upload_origin}/",
            "User-Agent": USER_AGENT,
            "x-goog-authuser": "0",
            "x-goog-upload-command": "upload, finalize",
            "x-goog-upload-offset": "0",
        }
        with open(file_path, "rb") as f:
            req = urllib.request.Request(upload_url, data=f, headers=headers, method="POST")
            self._opener.open(req)

        return source_id

    def _extract_first_string(self, data) -> str | None:
        """ネストされたデータから最初の文字列を抽出"""
        if isinstance(data, str):
            return data
        if isinstance(data, list):
            for item in data:
                result = self._extract_first_string(item)
                if result:
                    return result
        return None

    def _artifact_variant_from_raw(self, art: list) -> str | None:
        """type 4 / 2 / 11 アーティファクトのサブタイプを返す"""
        try:
            type_code = art[2]
        except (IndexError, TypeError):
            type_code = None

        if type_code == 4:
            try:
                return APP_ARTIFACT_TYPES.get(art[9][1][0])
            except (IndexError, TypeError):
                return None

        if type_code == 11:
            return "interactive_learning_overview"

        if type_code == 2:
            try:
                report_container = art[7] if len(art) > 7 else None
                payload = report_container[0] if isinstance(report_container, list) and report_container else report_container
                if isinstance(payload, str):
                    lowered = payload.lstrip().lower()
                    if "<html" in lowered or "<app-root" in lowered:
                        return "interactive_learning_overview"
                elif isinstance(payload, (dict, list)) and payload:
                    return "interactive_learning_overview"
            except (IndexError, TypeError):
                pass
        return None

    def _build_artifact_record(self, art: list) -> dict | None:
        """生アーティファクト配列を共通 dict 形式へ変換"""
        try:
            art_id = art[0]
            title = art[1] or "Untitled"
            type_code = art[2]
            status_code = art[4] if len(art) > 4 else None
        except (IndexError, TypeError):
            return None

        art_type = ARTIFACT_TYPES.get(type_code, f"unknown_{type_code}")
        status = ARTIFACT_STATUS.get(status_code, "unknown")
        variant = self._artifact_variant_from_raw(art)
        download_info = self._extract_artifact_download(art, type_code, title=title)
        if not variant and download_info.get("variant"):
            variant = download_info["variant"]

        return {
            "id": art_id,
            "title": title,
            "type": art_type,
            "type_code": type_code,
            "variant": variant,
            "status": status,
            "status_code": status_code,
            "_raw": art,
            **download_info,
        }

    def _extract_app_artifact_data(self, html_content: str) -> dict | None:
        """HTML から app-root の JSON データを抽出"""
        match = APP_ARTIFACT_DATA_PATTERN.search(html_content)
        if not match:
            return None
        try:
            return json.loads(html_lib.unescape(match.group(1)))
        except json.JSONDecodeError:
            return None

    def _extract_interactive_mind_map_data(self, art: list) -> dict | None:
        """type 4 / variant 4 の mind-map tree JSON を抽出"""
        try:
            raw = art[9][3]
        except (IndexError, TypeError):
            return None
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _artifact_variant_from_data(self, artifact: dict, app_data: dict | None) -> str:
        """詳細 HTML / structured payload から得たデータを含めて種別を判定"""
        variant = artifact.get("variant")
        if variant:
            return variant
        if isinstance(app_data, dict):
            # If the payload has learning overview sections/modules or mixes slides/sections with quiz/flashcards
            if any(
                k in app_data
                for k in (
                    "sections",
                    "modules",
                    "lessons",
                    "learningOverview",
                    "interactiveLearningOverview",
                    "overview",
                    "relatedArtifacts",
                    "embeddedArtifacts",
                    "slides",
                )
            ):
                return "interactive_learning_overview"
            if "flashcards" in app_data or "cards" in app_data:
                if "quiz" not in app_data and "questions" not in app_data:
                    return "flashcards"
                return "interactive_learning_overview"
            if "quiz" in app_data or "questions" in app_data:
                return "quiz"
        if artifact.get("type") in {"report", "guided_view"} and (
            artifact.get("app_html") or artifact.get("structured_content") is not None
        ):
            return "interactive_learning_overview"
        return artifact.get("type", "flashcards")

    def _extract_referenced_artifact_ids(self, data) -> list[str]:
        """Interactive Learning Overview 等の payload から関連アーティファクトIDを抽出"""
        refs: list[str] = []
        seen: set[str] = set()
        id_keys = {
            "artifactid",
            "artifact_id",
            "relatedartifactid",
            "related_artifact_id",
            "slidedeckid",
            "slide_deck_id",
            "quizid",
            "quiz_id",
            "flashcardsid",
            "flashcards_id",
            "mindmapid",
            "mind_map_id",
            "videoid",
            "video_id",
            "audioid",
            "audio_id",
        }
        list_keys = {
            "artifactids",
            "artifact_ids",
            "relatedartifactids",
            "related_artifact_ids",
            "relatedartifacts",
            "related_artifacts",
            "embeddedartifacts",
            "embedded_artifacts",
        }

        def add_ref(val):
            if isinstance(val, str):
                cleaned = val.strip()
                if cleaned and cleaned not in seen and not cleaned.startswith(("http://", "https://")):
                    seen.add(cleaned)
                    refs.append(cleaned)

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    kl = str(k).lower()
                    if kl in id_keys:
                        add_ref(v)
                    elif kl in list_keys and isinstance(v, list):
                        for item in v:
                            if isinstance(item, str):
                                add_ref(item)
                            elif isinstance(item, dict):
                                add_ref(item.get("id") or item.get("artifactId") or item.get("artifact_id"))
                                walk(item)
                    else:
                        walk(v)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(data)
        return refs

    def _normalize_artifact_text(self, value) -> str:
        if value is None:
            return ""
        return str(value).replace("\r\n", "\n").strip()

    def _first_artifact_text(self, item: dict, keys: tuple[str, ...]) -> str:
        """複数候補キーから最初の非空テキストを返す"""
        for key in keys:
            if key not in item:
                continue
            value = item.get(key)
            if isinstance(value, dict):
                for nested_key in ("text", "value", "answer", "label", "title", "content"):
                    text = self._normalize_artifact_text(value.get(nested_key))
                    if text:
                        return text
                continue
            text = self._normalize_artifact_text(value)
            if text:
                return text
        return ""

    def _quiz_question_format(self, item: dict) -> str:
        """新旧 Quiz schema の形式名を best-effort で抽出"""
        explicit = self._first_artifact_text(
            item,
            ("questionType", "question_type", "format", "kind", "type"),
        )
        if explicit:
            return explicit
        options = self._quiz_options(item)
        if options:
            correct_count = sum(
                1
                for opt in options
                if isinstance(opt, dict) and (opt.get("isCorrect") is True or opt.get("correct") is True)
            )
            if correct_count > 1 or item.get("multiSelect") is True or item.get("allowMultiple") is True:
                return "multiple_select"
            return "single_choice"
        question_text = self._first_artifact_text(item, ("question", "prompt", "stem"))
        if "___" in question_text or "blank" in str(item.get("blank", "")).lower():
            return "fill_in_the_blank"
        if self._quiz_correct_answers(item, []):
            return "short_answer"
        return ""

    def _quiz_options(self, item: dict) -> list[dict]:
        """answerOptions/options/choices の違いを吸収して正規化"""
        for key in ("answerOptions", "options", "choices"):
            raw_options = item.get(key)
            if not isinstance(raw_options, list):
                continue
            options = []
            for option in raw_options:
                if isinstance(option, dict):
                    options.append(option)
                elif isinstance(option, str):
                    options.append({"text": option})
            return options
        return []

    def _quiz_correct_answers(self, item: dict, options: list[dict]) -> list[str]:
        """multiple-choice 以外の回答 schema も含めて正答候補を抽出"""
        answers = []

        for option in options:
            if not isinstance(option, dict):
                continue
            if option.get("isCorrect") is True or option.get("correct") is True:
                text = self._first_artifact_text(option, ("text", "value", "answer", "label"))
                if text:
                    answers.append(text)

        for key in (
            "correctAnswers",
            "acceptedAnswers",
            "expectedAnswers",
            "answers",
            "blanks",
        ):
            value = item.get(key)
            if not isinstance(value, list):
                continue
            for answer in value:
                if isinstance(answer, dict):
                    text = self._first_artifact_text(
                        answer,
                        ("text", "value", "answer", "label", "acceptedAnswer"),
                    )
                else:
                    text = self._normalize_artifact_text(answer)
                if text:
                    answers.append(text)

        for key in (
            "correctAnswer",
            "acceptedAnswer",
            "expectedAnswer",
            "answer",
            "solution",
            "blankAnswer",
        ):
            if key not in item:
                continue
            value = item.get(key)
            if isinstance(value, list):
                for answer in value:
                    text = (
                        self._first_artifact_text(
                            answer,
                            ("text", "value", "answer", "label"),
                        )
                        if isinstance(answer, dict)
                        else self._normalize_artifact_text(answer)
                    )
                    if text:
                        answers.append(text)
            elif isinstance(value, dict):
                text = self._first_artifact_text(
                    value,
                    ("text", "value", "answer", "label"),
                )
                if text:
                    answers.append(text)
            else:
                text = self._normalize_artifact_text(value)
                if text:
                    answers.append(text)

        deduped = []
        seen = set()
        for answer in answers:
            if answer in seen:
                continue
            seen.add(answer)
            deduped.append(answer)
        return deduped

    def _extract_item_image_urls(self, item: dict) -> list[str]:
        urls = []
        for key in ("imageUrls", "image_urls", "images"):
            val = item.get(key)
            if isinstance(val, list):
                for entry in val:
                    if isinstance(entry, str) and entry.strip():
                        urls.append(entry.strip())
                    elif isinstance(entry, dict):
                        u = self._first_artifact_text(entry, ("url", "src", "imageUrl", "image_url"))
                        if u:
                            urls.append(u)
        for key in ("imageUrl", "image_url", "image", "frontImageUrl", "backImageUrl"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                urls.append(val.strip())
        deduped = []
        seen = set()
        for u in urls:
            if u not in seen:
                seen.add(u)
                deduped.append(u)
        return deduped

    def _render_flashcards_markdown(
        self,
        title: str,
        cards: list[dict],
        html_name: str,
        json_name: str,
        app_data: dict | None = None,
    ) -> str:
        lines = [
            f"# {title}",
            "",
            "- Artifact subtype: flashcards",
            f"- Card count: {len(cards)}",
            f"- Original HTML: `{html_name}`",
            f"- Structured data: `{json_name}`",
        ]
        if isinstance(app_data, dict):
            topic = self._first_artifact_text(app_data, ("topic", "summary", "description"))
            if topic:
                lines.append(f"- Summary: {topic}")
        for i, card in enumerate(cards, 1):
            front = self._first_artifact_text(card, ("f", "front", "term", "question", "prompt"))
            back = self._first_artifact_text(card, ("b", "back", "definition", "answer", "explanation"))
            hint = self._first_artifact_text(card, ("hint",))
            notes = self._first_artifact_text(card, ("notes", "rationale", "feedback"))
            image_urls = self._extract_item_image_urls(card)
            lines.extend(
                [
                    "",
                    f"## Card {i}",
                    "",
                    "**Front**",
                    "",
                    front or "(empty)",
                    "",
                    "**Back**",
                    "",
                    back or "(empty)",
                ]
            )
            if hint:
                lines.extend(["", f"Hint: {hint}"])
            if notes:
                lines.extend(["", f"Notes: {notes}"])
            if image_urls:
                lines.extend(["", "Images: " + ", ".join(f"`{u}`" for u in image_urls)])
        return "\n".join(lines).rstrip() + "\n"

    def _render_quiz_markdown(
        self,
        title: str,
        questions: list[dict],
        html_name: str,
        json_name: str,
        app_data: dict | None = None,
    ) -> str:
        lines = [
            f"# {title}",
            "",
            "- Artifact subtype: quiz",
            f"- Question count: {len(questions)}",
            f"- Original HTML: `{html_name}`",
            f"- Structured data: `{json_name}`",
        ]
        if isinstance(app_data, dict):
            difficulty = self._first_artifact_text(app_data, ("difficulty", "level"))
            if difficulty:
                lines.append(f"- Difficulty: {difficulty}")
            score_val = app_data.get("score") if "score" in app_data else app_data.get("userScore")
            if score_val is not None:
                lines.append(f"- Recorded score: {score_val}")
            overall_feedback = self._first_artifact_text(
                app_data,
                ("feedback", "summaryFeedback", "overallFeedback", "summary"),
            )
            if overall_feedback:
                lines.append(f"- Overall feedback: {overall_feedback}")
            topics = app_data.get("topics") or app_data.get("keepLearning")
            if isinstance(topics, list) and topics:
                topic_names = []
                for t in topics:
                    if isinstance(t, str) and t.strip():
                        topic_names.append(t.strip())
                    elif isinstance(t, dict):
                        t_name = self._first_artifact_text(t, ("title", "topic", "name", "label"))
                        if t_name:
                            topic_names.append(t_name)
                if topic_names:
                    lines.append(f"- Study topics: {', '.join(topic_names)}")

        for i, item in enumerate(questions, 1):
            question = self._first_artifact_text(item, ("question", "prompt", "stem", "text"))
            hint = self._first_artifact_text(item, ("hint",))
            question_format = self._quiz_question_format(item)
            options = self._quiz_options(item)
            correct_answers = self._quiz_correct_answers(item, options)
            explanation = self._first_artifact_text(
                item,
                ("rationale", "explanation", "feedback", "gradingNotes", "grading"),
            )
            user_answer = self._first_artifact_text(
                item,
                ("userAnswer", "user_answer", "submittedAnswer", "selectedAnswer"),
            )
            image_urls = self._extract_item_image_urls(item)
            lines.extend(
                [
                    "",
                    f"## Question {i}",
                    "",
                    question or "(empty)",
                ]
            )
            if question_format:
                lines.extend(["", f"Format: {question_format}"])
            if image_urls:
                lines.extend(["", "Images: " + ", ".join(f"`{u}`" for u in image_urls)])
            if hint:
                lines.extend(["", f"Hint: {hint}"])
            if correct_answers:
                label = "Correct answers" if len(correct_answers) > 1 else "Correct answer"
                lines.extend(["", f"{label}: {', '.join(correct_answers)}"])
            if user_answer:
                lines.extend(["", f"User answer: {user_answer}"])
            if explanation:
                lines.extend(["", f"Explanation: {explanation}"])
            if options:
                lines.extend(["", "### Options", ""])
                correct_set = set(correct_answers)
                for option in options:
                    text = self._first_artifact_text(
                        option,
                        ("text", "value", "answer", "label"),
                    )
                    is_correct = (
                        option.get("isCorrect") is True
                        or option.get("correct") is True
                        or (text and text in correct_set)
                    )
                    marker = "x" if is_correct else " "
                    rationale = self._first_artifact_text(
                        option,
                        ("rationale", "explanation", "feedback"),
                    )
                    lines.append(f"- [{marker}] {text or '(empty)'}")
                    if rationale:
                        lines.append(f"  Rationale: {rationale}")
        return "\n".join(lines).rstrip() + "\n"

    def _render_mind_map_markdown(self, title: str, tree: dict, indent: int = 0) -> str:
        """Interactive mind map tree を読みやすい Markdown に変換"""
        name = self._normalize_artifact_text(tree.get("name") or tree.get("title") or tree.get("label")) or "(untitled)"
        lines = []
        if indent == 0:
            lines.extend([f"# {title}", "", "- Artifact subtype: interactive_mind_map", ""])
        lines.append("  " * indent + f"- {name}")
        for child in tree.get("children", []) or tree.get("nodes", []) or []:
            if isinstance(child, dict):
                child_md = self._render_mind_map_markdown(title, child, indent + 1)
                if indent + 1 > 0:
                    child_lines = child_md.splitlines()
                    if child_lines and child_lines[0].startswith("# "):
                        child_lines = child_lines[4:]
                    lines.extend(child_lines)
        return "\n".join(lines).rstrip() + "\n"

    def _render_interactive_learning_overview_markdown(
        self,
        title: str,
        payload,
        html_name: str | None = None,
        json_name: str | None = None,
        referenced_ids: list[str] | None = None,
    ) -> str:
        """Interactive Learning Overview (HTML app_data または structured_content) を Markdown 化"""
        lines = [
            f"# {title}",
            "",
            "- Artifact subtype: interactive_learning_overview",
        ]
        if html_name:
            lines.append(f"- Original HTML: `{html_name}`")
        if json_name:
            lines.append(f"- Structured data: `{json_name}`")
        refs = referenced_ids if referenced_ids is not None else self._extract_referenced_artifact_ids(payload)
        if refs:
            lines.append(f"- Referenced artifacts: {', '.join(f'`{r}`' for r in refs)}")

        if isinstance(payload, dict):
            summary = self._first_artifact_text(
                payload,
                ("summary", "overview", "introduction", "description", "subtitle"),
            )
            if summary:
                lines.extend(["", "## Overview", "", summary])

            # Render sections / modules / lessons / blocks
            sections = (
                payload.get("sections")
                or payload.get("modules")
                or payload.get("lessons")
                or payload.get("chapters")
                or payload.get("blocks")
                or []
            )
            if isinstance(sections, list) and sections:
                lines.extend(["", "## Sections"])
                for idx, sec in enumerate(sections, 1):
                    if isinstance(sec, str):
                        lines.extend(["", f"### Section {idx}", "", sec.strip()])
                        continue
                    if not isinstance(sec, dict):
                        continue
                    sec_title = self._first_artifact_text(
                        sec,
                        ("title", "heading", "name", "topic", "label"),
                    ) or f"Section {idx}"
                    sec_body = self._first_artifact_text(
                        sec,
                        ("content", "body", "text", "summary", "description", "markdown"),
                    )
                    lines.extend(["", f"### {idx}. {sec_title}"])
                    if sec_body:
                        lines.extend(["", sec_body])
                    key_points = sec.get("keyPoints") or sec.get("bullets") or sec.get("takeaways") or sec.get("items")
                    if isinstance(key_points, list) and key_points:
                        lines.append("")
                        for kp in key_points:
                            if isinstance(kp, str) and kp.strip():
                                lines.append(f"- {kp.strip()}")
                            elif isinstance(kp, dict):
                                kp_text = self._first_artifact_text(kp, ("text", "title", "content", "point"))
                                if kp_text:
                                    lines.append(f"- {kp_text}")
                    # Section-level embedded slides/quiz/flashcards
                    sec_slides = sec.get("slides")
                    if isinstance(sec_slides, list) and sec_slides:
                        lines.extend(["", "#### Embedded Slides"])
                        for s_idx, slide in enumerate(sec_slides, 1):
                            if isinstance(slide, dict):
                                s_title = self._first_artifact_text(slide, ("title", "heading")) or f"Slide {s_idx}"
                                s_body = self._first_artifact_text(slide, ("content", "body", "text", "notes"))
                                lines.append(f"- **{s_title}**" + (f": {s_body}" if s_body else ""))
                    sec_quiz = sec.get("quiz") or sec.get("questions")
                    if isinstance(sec_quiz, list) and sec_quiz:
                        quiz_questions = [q for q in sec_quiz if isinstance(q, dict)]
                        if quiz_questions:
                            quiz_md = self._render_quiz_markdown(
                                f"{sec_title} - Quiz",
                                quiz_questions,
                                html_name or "",
                                json_name or "",
                            )
                            body_lines = quiz_md.splitlines()[6:]
                            lines.extend(["", "#### Embedded Quiz", *body_lines])
                    sec_cards = sec.get("flashcards") or sec.get("cards")
                    if isinstance(sec_cards, list) and sec_cards:
                        card_items = [c for c in sec_cards if isinstance(c, dict)]
                        if card_items:
                            cards_md = self._render_flashcards_markdown(
                                f"{sec_title} - Flashcards",
                                card_items,
                                html_name or "",
                                json_name or "",
                            )
                            body_lines = cards_md.splitlines()[6:]
                            lines.extend(["", "#### Embedded Flashcards", *body_lines])

            # Top-level embedded slides
            top_slides = payload.get("slides")
            if isinstance(top_slides, list) and top_slides:
                lines.extend(["", "## Embedded Slides", ""])
                for s_idx, slide in enumerate(top_slides, 1):
                    if isinstance(slide, dict):
                        s_title = self._first_artifact_text(slide, ("title", "heading", "name")) or f"Slide {s_idx}"
                        s_body = self._first_artifact_text(slide, ("content", "body", "text", "speakerNotes", "notes"))
                        lines.append(f"### Slide {s_idx}: {s_title}")
                        if s_body:
                            lines.extend(["", s_body])
                        bullets = slide.get("bullets") or slide.get("points")
                        if isinstance(bullets, list):
                            lines.append("")
                            for b in bullets:
                                b_text = self._normalize_artifact_text(b)
                                if b_text:
                                    lines.append(f"- {b_text}")
                        lines.append("")

            # Top-level embedded quiz
            top_quiz = payload.get("quiz") or payload.get("questions")
            if isinstance(top_quiz, list) and top_quiz:
                quiz_questions = [q for q in top_quiz if isinstance(q, dict)]
                if quiz_questions:
                    quiz_md = self._render_quiz_markdown(
                        f"{title} - Embedded Quiz",
                        quiz_questions,
                        html_name or "",
                        json_name or "",
                        payload,
                    )
                    lines.extend(["", "## Embedded Quiz", *quiz_md.splitlines()[6:]])

            # Top-level embedded flashcards
            top_cards = payload.get("flashcards") or payload.get("cards")
            if isinstance(top_cards, list) and top_cards:
                card_items = [c for c in top_cards if isinstance(c, dict)]
                if card_items:
                    cards_md = self._render_flashcards_markdown(
                        f"{title} - Embedded Flashcards",
                        card_items,
                        html_name or "",
                        json_name or "",
                        payload,
                    )
                    lines.extend(["", "## Embedded Flashcards", *cards_md.splitlines()[6:]])

            # Top-level embedded mindMap
            mind_map = payload.get("mindMap") or payload.get("mind_map")
            if isinstance(mind_map, dict) and ("name" in mind_map or "children" in mind_map or "nodes" in mind_map):
                mm_md = self._render_mind_map_markdown(title, mind_map)
                mm_lines = mm_md.splitlines()[4:]
                lines.extend(["", "## Embedded Mind Map", "", *mm_lines])

            # Citations / sources
            citations = payload.get("citations") or payload.get("sources") or payload.get("references")
            if isinstance(citations, list) and citations:
                lines.extend(["", "## Sources & Citations", ""])
                for c in citations:
                    if isinstance(c, str) and c.strip():
                        lines.append(f"- {c.strip()}")
                    elif isinstance(c, dict):
                        c_title = self._first_artifact_text(c, ("title", "name", "sourceTitle", "label", "id"))
                        c_quote = self._first_artifact_text(c, ("quote", "snippet", "text"))
                        if c_title and c_quote:
                            lines.append(f"- **{c_title}**: {c_quote}")
                        elif c_title or c_quote:
                            lines.append(f"- {c_title or c_quote}")

        elif isinstance(payload, list):
            lines.extend(["", "## Structured Items", ""])
            for idx, entry in enumerate(payload, 1):
                if isinstance(entry, dict):
                    e_title = self._first_artifact_text(entry, ("title", "heading", "name")) or f"Item {idx}"
                    e_body = self._first_artifact_text(entry, ("content", "body", "text", "summary"))
                    lines.append(f"### {idx}. {e_title}")
                    if e_body:
                        lines.extend(["", e_body, ""])
                elif isinstance(entry, str) and entry.strip():
                    lines.extend([f"- {entry.strip()}"])

        return "\n".join(lines).rstrip() + "\n"

    def _render_app_artifact_markdown(
        self,
        artifact: dict,
        app_data: dict | None,
        html_name: str,
        json_name: str,
    ) -> str:
        title = artifact.get("title", "Untitled")
        variant = self._artifact_variant_from_data(artifact, app_data)

        if isinstance(app_data, dict):
            if variant == "flashcards":
                raw_cards = app_data.get("flashcards") if "flashcards" in app_data else app_data.get("cards", [])
                cards = [c for c in (raw_cards or []) if isinstance(c, dict)]
                return self._render_flashcards_markdown(title, cards, html_name, json_name, app_data)
            if variant == "quiz":
                raw_q = app_data.get("quiz")
                if not isinstance(raw_q, list):
                    raw_q = app_data.get("questions") or app_data.get("items") or []
                questions = [q for q in raw_q if isinstance(q, dict)]
                return self._render_quiz_markdown(title, questions, html_name, json_name, app_data)
            if variant == "interactive_learning_overview":
                return self._render_interactive_learning_overview_markdown(
                    title,
                    app_data,
                    html_name=html_name,
                    json_name=json_name,
                    referenced_ids=artifact.get("referenced_artifact_ids"),
                )

        lines = [
            f"# {title}",
            "",
            f"- Artifact subtype: {variant}",
            f"- Original HTML: `{html_name}`",
            f"- Structured data: `{json_name}`",
            "",
            "NotebookLM returned an HTML app for this artifact, but the structured payload could not be parsed.",
        ]
        return "\n".join(lines) + "\n"

    def render_co_creation_review_markdown(self, artifact: dict) -> str:
        """Slide Deck / Video Overview の共同制作レビュー情報 (outline / producer notes) を Markdown 化"""
        title = artifact.get("title", "Untitled")
        art_type = artifact.get("type", "unknown")
        status = artifact.get("status", "unknown")
        notes = artifact.get("co_creation_notes") or {}
        gen_meta = artifact.get("generation_metadata") or {}

        lines = [
            f"# {title}",
            "",
            f"- Artifact type: {art_type}",
            f"- Status: {status}",
        ]
        if artifact.get("video_format"):
            lines.append(f"- Video format: {artifact['video_format']}")
        if gen_meta.get("language"):
            lines.append(f"- Language: {gen_meta['language']}")
        if gen_meta.get("duration_seconds"):
            lines.append(f"- Duration (seconds): {gen_meta['duration_seconds']}")
        if gen_meta.get("aspect_ratio"):
            lines.append(f"- Aspect ratio: {gen_meta['aspect_ratio']}")

        prompt = notes.get("prompt") or gen_meta.get("prompt")
        if prompt:
            lines.extend(["", "## Generation Prompt / Instructions", "", str(prompt).strip()])

        user_revisions = notes.get("user_revisions") or notes.get("revision_instructions")
        if user_revisions:
            lines.extend(["", "## User Revision Instructions", ""])
            if isinstance(user_revisions, list):
                for rev in user_revisions:
                    lines.append(f"- {self._normalize_artifact_text(rev)}")
            else:
                lines.append(self._normalize_artifact_text(user_revisions))

        outline_notes = notes.get("outline_notes")
        if outline_notes:
            lines.extend(["", "## Review Outline Notes (Slide Deck)", ""])
            if isinstance(outline_notes, list):
                for idx, slide_note in enumerate(outline_notes, 1):
                    if isinstance(slide_note, dict):
                        s_title = self._first_artifact_text(slide_note, ("title", "heading", "slideTitle", "name")) or f"Slide {idx}"
                        s_body = self._first_artifact_text(slide_note, ("notes", "outline", "content", "body", "description"))
                        lines.append(f"### Slide {idx}: {s_title}")
                        if s_body:
                            lines.extend(["", s_body])
                        bullets = slide_note.get("bullets") or slide_note.get("points")
                        if isinstance(bullets, list):
                            lines.append("")
                            for b in bullets:
                                lines.append(f"- {self._normalize_artifact_text(b)}")
                        lines.append("")
                    else:
                        lines.append(f"- {self._normalize_artifact_text(slide_note)}")
            else:
                lines.append(self._normalize_artifact_text(outline_notes))

        producer_notes = notes.get("producer_notes")
        if producer_notes:
            lines.extend(["", "## Review Producer Notes (Video Overview)", ""])
            if isinstance(producer_notes, list):
                for idx, scene_note in enumerate(producer_notes, 1):
                    if isinstance(scene_note, dict):
                        sc_title = self._first_artifact_text(scene_note, ("title", "scene", "heading", "name")) or f"Scene {idx}"
                        sc_body = self._first_artifact_text(scene_note, ("notes", "producerNotes", "script", "direction", "content", "body"))
                        lines.append(f"### {sc_title}")
                        if sc_body:
                            lines.extend(["", sc_body, ""])
                    else:
                        lines.append(f"- {self._normalize_artifact_text(scene_note)}")
            elif isinstance(producer_notes, dict):
                for k, v in producer_notes.items():
                    lines.extend([f"### {k}", "", self._normalize_artifact_text(v), ""])
            else:
                lines.append(self._normalize_artifact_text(producer_notes))

        return "\n".join(lines).rstrip() + "\n"

    # ------------------------------------------------------------------
    # Artifacts (Studio)
    # ------------------------------------------------------------------

    def list_artifacts(self, notebook_id: str) -> list[dict]:
        """アーティファクト一覧を取得"""
        result = self._batchexecute(
            "gArtLc",
            [[2], notebook_id, 'NOT artifact.status = "ARTIFACT_STATUS_SUGGESTED"'],
            source_path=f"/notebook/{notebook_id}",
        )
        if not result or not isinstance(result[0], list):
            return []

        artifacts = []
        for art in result[0]:
            record = self._build_artifact_record(art)
            if record:
                artifacts.append(record)
        return artifacts

    def get_artifact(self, artifact_id: str) -> dict | None:
        """アーティファクト詳細を取得"""
        result = self._batchexecute("v9rmvd", [artifact_id])
        if not result or not isinstance(result[0], list):
            return None
        return self._build_artifact_record(result[0])

    def _extract_video_metadata(self, video_slot, info: dict):
        """Video Overview (Explainer / Short / Cinematic) の形式とメタデータを抽出"""
        gen_meta = info.setdefault("generation_metadata", {})
        co_notes = {}

        def scan(node):
            if isinstance(node, dict):
                style = self._first_artifact_text(node, ("videoStyle", "style", "format", "videoFormat", "subtype"))
                if style:
                    sl = style.lower()
                    if "short" in sl or "brief" in sl or "vertical" in sl:
                        info["video_format"] = "short"
                    elif "cinematic" in sl:
                        info["video_format"] = "cinematic"
                    elif "explainer" in sl or "standard" in sl:
                        info["video_format"] = "explainer"
                    else:
                        info["video_format"] = sl
                lang = self._first_artifact_text(node, ("language", "locale", "lang"))
                if lang:
                    gen_meta["language"] = lang
                dur = node.get("durationSeconds") or node.get("duration_seconds") or node.get("duration")
                if isinstance(dur, (int, float)) and dur > 0:
                    gen_meta["duration_seconds"] = dur
                    if "video_format" not in info and dur <= 75:
                        info["video_format"] = "short"
                aspect = self._first_artifact_text(node, ("aspectRatio", "aspect_ratio", "orientation"))
                if aspect:
                    gen_meta["aspect_ratio"] = aspect
                    if "video_format" not in info and ("9:16" in aspect or "vertical" in aspect.lower() or "portrait" in aspect.lower()):
                        info["video_format"] = "short"
                p_notes = node.get("producerNotes") or node.get("producer_notes") or node.get("reviewProducerNotes") or node.get("scenes")
                if p_notes:
                    co_notes["producer_notes"] = p_notes
                prompt = self._first_artifact_text(node, ("prompt", "customPrompt", "instructions", "userInstructions"))
                if prompt:
                    co_notes["prompt"] = prompt
                    gen_meta["prompt"] = prompt
                rev = node.get("revisionInstructions") or node.get("userRevisions") or node.get("feedback")
                if rev:
                    co_notes["user_revisions"] = rev
                for v in node.values():
                    scan(v)
            elif isinstance(node, list):
                # Check common positional conventions in video_slot
                for item in node:
                    if isinstance(item, str):
                        low = item.strip().lower()
                        if low in {"explainer", "short", "cinematic", "short_video", "brief"}:
                            info["video_format"] = "short" if low in {"short", "short_video", "brief"} else low
                        elif re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", item.strip()):
                            gen_meta.setdefault("language", item.strip())
                        elif len(item.strip()) > 20 and not item.strip().startswith("http"):
                            # Producer notes or prompt string in video slot
                            co_notes.setdefault("producer_notes", item.strip())
                    elif isinstance(item, (dict, list)):
                        scan(item)

        if isinstance(video_slot, list) and len(video_slot) > 1 and isinstance(video_slot[1], int):
            mapped = VIDEO_OVERVIEW_STYLES.get(video_slot[1])
            if mapped:
                info["video_format"] = mapped
        scan(video_slot)
        if co_notes:
            info["co_creation_notes"] = {**info.get("co_creation_notes", {}), **co_notes}

    def _extract_slide_co_creation_metadata(self, slide_slot, info: dict):
        """Slide Deck の Review outline notes / prompt / 生成設定を抽出"""
        gen_meta = info.setdefault("generation_metadata", {})
        co_notes = {}

        def scan(node):
            if isinstance(node, dict):
                o_notes = (
                    node.get("outlineNotes")
                    or node.get("outline_notes")
                    or node.get("reviewOutlineNotes")
                    or node.get("outline")
                    or node.get("slidesOutline")
                )
                if o_notes:
                    co_notes["outline_notes"] = o_notes
                prompt = self._first_artifact_text(node, ("prompt", "customPrompt", "instructions", "userInstructions"))
                if prompt:
                    co_notes["prompt"] = prompt
                    gen_meta["prompt"] = prompt
                rev = node.get("revisionInstructions") or node.get("userRevisions") or node.get("feedback")
                if rev:
                    co_notes["user_revisions"] = rev
                lang = self._first_artifact_text(node, ("language", "locale", "lang"))
                if lang:
                    gen_meta["language"] = lang
                for v in node.values():
                    scan(v)
            elif isinstance(node, list):
                for item in node:
                    if isinstance(item, (dict, list)):
                        scan(item)

        scan(slide_slot)
        # Check positional outline notes on slide_slot (e.g., art[16][5] or art[16][1] when non-URL)
        if isinstance(slide_slot, list):
            for idx in (1, 5, 6, 7):
                if len(slide_slot) > idx and slide_slot[idx]:
                    candidate = slide_slot[idx]
                    if isinstance(candidate, str) and not candidate.startswith("http") and len(candidate.strip()) > 10:
                        co_notes.setdefault("outline_notes", candidate.strip())
                    elif isinstance(candidate, list) and all(isinstance(x, (str, dict)) for x in candidate) and candidate:
                        co_notes.setdefault("outline_notes", candidate)
        if co_notes:
            info["co_creation_notes"] = {**info.get("co_creation_notes", {}), **co_notes}

    def _infer_generated_file_extension(
        self,
        title: str | None,
        file_name: str | None,
        content_mime: str | None,
        download_url: str | None,
        inline_content: str | None,
        structured_content,
    ) -> str | None:
        """生成ファイル (type_code == 10) の拡張子をタイトル・MIME・URL・内容から推定"""
        for name_candidate in (file_name, title):
            if isinstance(name_candidate, str) and "." in name_candidate:
                ext = Path(name_candidate.strip()).suffix.lower()
                if ext == ".markdown":
                    return ".md"
                if ext == ".jpeg":
                    return ".jpg"
                if ext in SUPPORTED_GENERATED_FILE_EXTENSIONS:
                    return ext

        norm_mime = _normalized_mime(content_mime)
        if norm_mime in GENERATED_FILE_MIME_EXTENSIONS:
            return GENERATED_FILE_MIME_EXTENSIONS[norm_mime]
        if norm_mime:
            guessed = mimetypes.guess_extension(norm_mime)
            if guessed in SUPPORTED_GENERATED_FILE_EXTENSIONS:
                return ".jpg" if guessed == ".jpeg" else guessed

        if isinstance(download_url, str) and download_url.startswith("http"):
            try:
                url_path = urllib.parse.urlsplit(download_url).path
                ext = Path(url_path).suffix.lower()
                if ext in SUPPORTED_GENERATED_FILE_EXTENSIONS:
                    return ".jpg" if ext == ".jpeg" else (".md" if ext == ".markdown" else ext)
            except ValueError:
                pass

        if isinstance(inline_content, str) and inline_content.strip():
            stripped = inline_content.lstrip()
            if stripped.startswith("<svg") or "<svg" in stripped[:300].lower():
                return ".svg"
            if stripped.startswith(("{", "[")):
                try:
                    json.loads(stripped)
                    return ".json"
                except json.JSONDecodeError:
                    pass
            if stripped.startswith("#") or "\n## " in stripped:
                return ".md"
            return ".txt"

        if structured_content is not None:
            return ".json"

        return None

    def _extract_generated_file_info(self, art: list, title: str = "") -> dict:
        """type_code == 10 ('file') のダウンロードURL・MIME・ファイル名・インライン実体を抽出"""
        info: dict = {}
        file_name = None
        content_mime = None
        download_url = None
        inline_content = None
        structured_content = None

        def scan(node):
            nonlocal file_name, content_mime, download_url, inline_content, structured_content
            if isinstance(node, dict):
                if not download_url:
                    u = self._first_artifact_text(node, ("downloadUrl", "download_url", "url", "fileUrl", "mediaUrl"))
                    if u.startswith(("http://", "https://")):
                        download_url = u
                if not content_mime:
                    m = self._first_artifact_text(node, ("mimeType", "mime_type", "contentType", "content_mime"))
                    if "/" in m:
                        content_mime = m
                if not file_name:
                    fn = self._first_artifact_text(node, ("fileName", "filename", "file_name", "name"))
                    if fn and "." in fn:
                        file_name = fn
                if inline_content is None:
                    for key in ("content", "text", "markdown", "csv", "svg", "body"):
                        if isinstance(node.get(key), str) and node[key].strip():
                            inline_content = node[key]
                            break
                if structured_content is None and "data" in node and isinstance(node["data"], (dict, list)):
                    structured_content = node["data"]
                for v in node.values():
                    scan(v)
            elif isinstance(node, list):
                # Pattern [url, ..., mime_type] or [url, filename, mime_type]
                if (
                    len(node) >= 2
                    and isinstance(node[0], str)
                    and node[0].startswith(("http://", "https://"))
                ):
                    if not download_url:
                        download_url = node[0]
                    for elem in node[1:]:
                        if isinstance(elem, str):
                            if "/" in elem and not elem.startswith("http") and not content_mime:
                                content_mime = elem
                            elif "." in elem and not elem.startswith("http") and not file_name:
                                file_name = elem
                for item in node:
                    if isinstance(item, (dict, list)):
                        scan(item)
                    elif isinstance(item, str):
                        s = item.strip()
                        if not s:
                            continue
                        if s.startswith(("http://", "https://")) and not download_url:
                            download_url = s
                        elif (
                            "/" in s
                            and not s.startswith("http")
                            and " " not in s
                            and _normalized_mime(s) in GENERATED_FILE_MIME_EXTENSIONS
                            and not content_mime
                        ):
                            content_mime = s
                        elif (
                            "." in s
                            and " " not in s
                            and Path(s).suffix.lower() in SUPPORTED_GENERATED_FILE_EXTENSIONS
                            and not file_name
                        ):
                            file_name = s
                        elif (s.startswith("<svg") or s.startswith("# ") or "\n" in s) and inline_content is None:
                            inline_content = item

        # Scan payload slots starting after status_code (art[5:])
        for slot in art[5:]:
            scan(slot)

        ext = self._infer_generated_file_extension(
            title=title,
            file_name=file_name,
            content_mime=content_mime,
            download_url=download_url,
            inline_content=inline_content,
            structured_content=structured_content,
        )

        if file_name:
            info["file_name"] = file_name
        if content_mime:
            info["content_mime"] = _normalized_mime(content_mime)
        if ext:
            info["file_extension"] = ext
        if download_url:
            info["download_url"] = download_url

        # Never fake extracted text as an original binary file (.pdf, .docx, .xlsx, .pptx, .png, .jpg, .gif)
        if ext in BINARY_GENERATED_FILE_EXTENSIONS:
            if not download_url and inline_content:
                info["extracted_text_only"] = inline_content
        else:
            if inline_content is not None:
                info["content"] = inline_content
            elif structured_content is not None:
                info["structured_content"] = structured_content

        return info

    def _extract_artifact_download(self, art: list, type_code: int, title: str = "") -> dict:
        """アーティファクトからダウンロード情報・メタデータを抽出"""
        info = {}
        try:
            if type_code == 1:  # audio
                media_list = art[6][5] if len(art) > 6 and art[6] and len(art[6]) > 5 else []
                for m in (media_list or []):
                    if isinstance(m, list) and len(m) > 2 and m[2] == "audio/mp4":
                        info["download_url"] = m[0]
                        break
            elif type_code == 2:  # report / interactive learning overview
                if len(art) > 7 and art[7]:
                    report_container = art[7]
                    if isinstance(report_container, list):
                        payload = report_container[0] if report_container else None
                    else:
                        payload = report_container

                    if isinstance(payload, str):
                        lowered = payload.lstrip().lower()
                        if "<html" in lowered or "<app-root" in lowered:
                            info["app_html"] = payload
                            info["variant"] = "interactive_learning_overview"
                            app_data = self._extract_app_artifact_data(payload)
                            if app_data is not None:
                                info["app_data"] = app_data
                                refs = self._extract_referenced_artifact_ids(app_data)
                                if refs:
                                    info["referenced_artifact_ids"] = refs
                        else:
                            info["content"] = payload
                    elif payload is not None:
                        info["structured_content"] = payload
                        info["variant"] = "interactive_learning_overview"
                        refs = self._extract_referenced_artifact_ids(payload)
                        if refs:
                            info["referenced_artifact_ids"] = refs
            elif type_code == 3:  # video_overview
                video_slot = art[8] if len(art) > 8 else []
                self._find_media_url(video_slot, "video/mp4", info)
                self._extract_video_metadata(video_slot, info)
            elif type_code == 7:  # infographic
                if len(art) > 14 and art[14]:
                    info["download_url"] = art[14][2][0][1][0]
            elif type_code == 8:  # slide_deck
                slide_slot = art[16] if len(art) > 16 else []
                if slide_slot and len(slide_slot) > 3:
                    if isinstance(slide_slot[3], str) and slide_slot[3].startswith("http"):
                        info["download_url"] = slide_slot[3]
                    # PPTX download URL (art[16][4], available on newer slides)
                    if len(slide_slot) > 4 and isinstance(slide_slot[4], str) and slide_slot[4].startswith("http"):
                        info["pptx_url"] = slide_slot[4]
                    # 各ページの画像URLを抽出
                    pages = slide_slot[2] if len(slide_slot) > 2 and isinstance(slide_slot[2], list) else []
                    page_images = []
                    for page in pages:
                        try:
                            if isinstance(page, list) and page[0] and isinstance(page[0], list):
                                page_images.append(page[0][0])  # image URL
                        except (IndexError, TypeError):
                            continue
                    if page_images:
                        info["page_images"] = page_images
                self._extract_slide_co_creation_metadata(slide_slot, info)
            elif type_code == 4:  # flashcards / quiz / interactive mind map
                variant = self._artifact_variant_from_raw(art)
                if variant == "interactive_mind_map":
                    tree = self._extract_interactive_mind_map_data(art)
                    if tree is not None:
                        info["structured_content"] = tree
                else:
                    html_content = art[9][0] if len(art) > 9 and art[9] else ""
                    if isinstance(html_content, str) and ("<html" in html_content.lower() or "<app-root" in html_content.lower()):
                        info["app_html"] = html_content
                        app_data = self._extract_app_artifact_data(html_content)
                        if app_data is not None:
                            info["app_data"] = app_data
                            refs = self._extract_referenced_artifact_ids(app_data)
                            if refs:
                                info["referenced_artifact_ids"] = refs
                    try:
                        lang = art[9][1][3]
                        if isinstance(lang, str) and lang:
                            info.setdefault("generation_metadata", {})["language"] = lang
                    except (IndexError, TypeError):
                        pass
            elif type_code == 9:  # data_table
                try:
                    info["content"] = self._extract_data_table(art[18])
                except (IndexError, TypeError):
                    pass
            elif type_code == 10:  # generated file (PDF, DOCX, MD, TXT, XLSX, PPTX, CSV, JSON, PNG, JPG, GIF, SVG)
                info.update(self._extract_generated_file_info(art, title=title))
            elif type_code == 11:  # guided_view / interactive learning overview
                info["variant"] = "interactive_learning_overview"
                for slot in art[5:]:
                    if isinstance(slot, str):
                        low = slot.lstrip().lower()
                        if "<html" in low or "<app-root" in low:
                            info["app_html"] = slot
                            app_data = self._extract_app_artifact_data(slot)
                            if app_data is not None:
                                info["app_data"] = app_data
                            break
                        elif len(slot.strip()) > 0 and "content" not in info:
                            info["content"] = slot
                    elif isinstance(slot, (dict, list)) and slot and "structured_content" not in info:
                        if isinstance(slot, list) and len(slot) == 1 and isinstance(slot[0], str):
                            low = slot[0].lstrip().lower()
                            if "<html" in low or "<app-root" in low:
                                info["app_html"] = slot[0]
                                app_data = self._extract_app_artifact_data(slot[0])
                                if app_data is not None:
                                    info["app_data"] = app_data
                                break
                        info["structured_content"] = slot
                target_data = info.get("app_data") or info.get("structured_content")
                if target_data is not None:
                    refs = self._extract_referenced_artifact_ids(target_data)
                    if refs:
                        info["referenced_artifact_ids"] = refs
        except (IndexError, TypeError):
            pass
        return info

    def _find_media_url(self, data, mime_type: str, info: dict):
        """ネストされたリストからメディアURLを探す"""
        if isinstance(data, list):
            if len(data) > 2 and isinstance(data[0], str) and data[0].startswith("http"):
                if data[2] == mime_type:
                    info["download_url"] = data[0]
                    return
            for item in data:
                if isinstance(item, list):
                    self._find_media_url(item, mime_type, info)
                    if "download_url" in info:
                        return

    def _extract_data_table(self, data) -> str:
        """データテーブルをCSV文字列に変換"""
        import csv
        import io
        buf = io.StringIO()
        writer = csv.writer(buf)
        try:
            table = data[0][0][0][0][4][2]
            for row in table:
                writer.writerow([cell[0] if isinstance(cell, list) and cell else str(cell) for cell in row])
        except (IndexError, TypeError):
            return ""
        return buf.getvalue()

    def _download_app_artifact(self, artifact: dict, dest_path: str | Path) -> bool:
        """type 4 / Interactive Learning Overview artifact を subtype に応じて Markdown + JSON/HTML で保存"""
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        detail = artifact
        variant = artifact.get("variant")
        needs_detail = (
            not artifact.get("app_html")
            and artifact.get("structured_content") is None
        )
        if needs_detail:
            detail = self.get_artifact(artifact.get("id", ""))
            if not detail:
                return False
            variant = detail.get("variant") or variant

        if variant == "interactive_mind_map":
            tree = detail.get("structured_content")
            if tree is None:
                tree = self._extract_interactive_mind_map_data(detail.get("_raw", []))
            if not isinstance(tree, dict):
                return False
            json_dest = dest_path.with_suffix(".json")
            markdown = self._render_mind_map_markdown(
                detail.get("title", "Untitled"),
                tree,
            )
            with open(dest_path, "w", encoding="utf-8") as f:
                f.write(markdown)
            with open(json_dest, "w", encoding="utf-8") as f:
                json.dump(tree, f, ensure_ascii=False, indent=2)
            return True

        html_content = detail.get("app_html")
        structured_content = detail.get("structured_content")

        if isinstance(html_content, str) and html_content:
            app_data = detail.get("app_data")
            if app_data is None:
                app_data = self._extract_app_artifact_data(html_content)

            md_dest = dest_path if dest_path.suffix.lower() == ".md" else dest_path.with_suffix(".md")
            html_dest = dest_path.with_suffix(".html")
            json_dest = dest_path.with_suffix(".json")
            resolved_variant = self._artifact_variant_from_data(detail, app_data)
            referenced_ids = detail.get("referenced_artifact_ids") or self._extract_referenced_artifact_ids(app_data)
            markdown = self._render_app_artifact_markdown(detail, app_data, html_dest.name, json_dest.name)

            with open(md_dest, "w", encoding="utf-8") as f:
                f.write(markdown)
            with open(html_dest, "w", encoding="utf-8") as f:
                f.write(html_content)
            json_payload = {
                "id": detail.get("id"),
                "title": detail.get("title"),
                "type": detail.get("type"),
                "variant": resolved_variant,
                "status": detail.get("status"),
                "data": app_data,
            }
            if referenced_ids:
                json_payload["referenced_artifact_ids"] = referenced_ids
            with open(json_dest, "w", encoding="utf-8") as f:
                json.dump(json_payload, f, ensure_ascii=False, indent=2)
            return True

        if structured_content is not None and (
            variant == "interactive_learning_overview"
            or detail.get("type") in {"report", "guided_view"}
        ):
            md_dest = dest_path.with_suffix(".md")
            json_dest = dest_path.with_suffix(".json")
            referenced_ids = detail.get("referenced_artifact_ids") or self._extract_referenced_artifact_ids(structured_content)
            markdown = self._render_interactive_learning_overview_markdown(
                detail.get("title", "Untitled"),
                structured_content,
                html_name=None,
                json_name=json_dest.name,
                referenced_ids=referenced_ids,
            )
            with open(md_dest, "w", encoding="utf-8") as f:
                f.write(markdown)
            with open(json_dest, "w", encoding="utf-8") as f:
                json.dump(structured_content, f, ensure_ascii=False, indent=2)
            return True

        return False

    def download_artifact(self, artifact: dict, dest_path: str | Path) -> bool:
        """アーティファクトをファイルにダウンロード"""
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        art_type = artifact.get("type")
        type_code = artifact.get("type_code")

        # Generated file artifact (type_code == 10 / 'file'):
        # If detail was not in list row, try get_artifact once when possible.
        if (type_code == 10 or art_type == "file") and not (
            artifact.get("download_url")
            or artifact.get("content")
            or artifact.get("structured_content") is not None
        ):
            if hasattr(self, "get_artifact") and artifact.get("id"):
                try:
                    detail = self.get_artifact(artifact["id"])
                except Exception:
                    detail = None
                if isinstance(detail, dict):
                    for k in ("download_url", "content", "structured_content", "file_extension", "content_mime", "file_name", "extracted_text_only"):
                        if detail.get(k) is not None and artifact.get(k) is None:
                            artifact[k] = detail[k]

        # Never fake extracted text as a binary file (.pdf, .docx, .xlsx, .pptx, .png, .jpg, .gif)
        file_ext = (artifact.get("file_extension") or dest_path.suffix or "").lower()
        if (type_code == 10 or art_type == "file") and file_ext in BINARY_GENERATED_FILE_EXTENSIONS:
            if artifact.get("download_url"):
                return self.download_url(artifact["download_url"], dest_path)
            return False

        if (
            type_code == 4
            or artifact.get("app_html")
            or (
                artifact.get("variant") == "interactive_learning_overview"
                and artifact.get("structured_content") is not None
            )
        ):
            return self._download_app_artifact(artifact, dest_path)

        # URLダウンロードの場合（バイナリ/原本ファイル優先）
        if "download_url" in artifact and artifact["download_url"]:
            return self.download_url(artifact["download_url"], dest_path)

        # インラインコンテンツの場合（report, data_table, text-based generated file）
        if "content" in artifact and artifact["content"]:
            with open(dest_path, "w", encoding="utf-8") as f:
                f.write(artifact["content"])
            return True

        # 未知/新形式の構造化コンテンツは JSON として保全
        if "structured_content" in artifact and artifact["structured_content"] is not None:
            with open(dest_path, "w", encoding="utf-8") as f:
                json.dump(artifact["structured_content"], f, ensure_ascii=False, indent=2)
            return True

        # Known-but-not-yet-rendered artifact families are still preserved as
        # a first-class JSON export instead of being reported as a total loss,
        # provided dest_path is a JSON metadata/raw fallback (not a faked binary file).
        if art_type in {"guided_view", "fantasy_map", "file", "mind_map"} and dest_path.suffix.lower() == ".json":
            with open(dest_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "id": artifact.get("id"),
                        "title": artifact.get("title"),
                        "type": art_type,
                        "type_code": type_code,
                        "variant": artifact.get("variant"),
                        "status": artifact.get("status"),
                        "raw": sanitize_for_backup(artifact.get("_raw")),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            return True

        return False

    def download_artifact_pptx(self, artifact: dict, dest_path: str | Path) -> bool:
        """スライドデッキの PPTX をダウンロード"""
        if "pptx_url" not in artifact or not artifact["pptx_url"]:
            return False
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        return self.download_url(artifact["pptx_url"], dest_path)

    def download_artifact_pages(self, artifact: dict, dest_dir: str | Path) -> list[Path]:
        """アーティファクトのページ画像をダウンロード（slide_deck 用）"""
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        page_images = artifact.get("page_images", [])
        saved = []
        for i, url in enumerate(page_images, 1):
            dest = dest_dir / f"page{i}.png"
            if self.download_url(url, dest):
                saved.append(dest)
        return saved

    # ------------------------------------------------------------------
    # Notes
    # ------------------------------------------------------------------

    def update_note(self, notebook_id: str, note_id: str, content: str, title: str) -> bool:
        """既存Noteの本文とタイトルを更新"""
        params = [notebook_id, note_id, [[[content, title, [], 0]]]]
        self._batchexecute(
            "cYAfTb",
            params,
            source_path=f"/notebook/{notebook_id}",
        )
        return True

    def create_note(self, notebook_id: str, title: str, content: str = "") -> str:
        """Note rowを作成し、本文とタイトルを確定してIDを返す"""
        result = self._batchexecute(
            "CYK0Xb",
            [notebook_id, "", [1], None, title],
            source_path=f"/notebook/{notebook_id}",
        )
        note_id = self._extract_first_string(result)
        if not note_id:
            raise NotebookLMError("Note作成に失敗しました")
        self.update_note(notebook_id, note_id, content, title)
        return note_id

    @staticmethod
    def _looks_like_note_row(item) -> bool:
        if not isinstance(item, list) or not item:
            return False
        if isinstance(item[0], str):
            return True
        return (
            item[0] is None
            and len(item) > 1
            and isinstance(item[1], list)
            and bool(item[1])
            and isinstance(item[1][0], str)
        )

    def _note_rows_from_result(self, result) -> list[list]:
        """Normalize historical/current GET_NOTES response containers."""
        if not isinstance(result, list) or not result:
            return []

        direct = [item for item in result if self._looks_like_note_row(item)]
        if direct:
            return direct

        first = result[0]
        if isinstance(first, list):
            return [item for item in first if self._looks_like_note_row(item)]
        return []

    def _decode_note_row(self, item: list) -> dict | None:
        """Decode legacy, current and outer-current note wrappers."""
        if not self._looks_like_note_row(item):
            return None

        # Current outer wrapper: [None, [id, content, metadata, None, title], ...]
        if item[0] is None:
            inner = item[1]
            note_id = inner[0]
            content = inner[1] if len(inner) > 1 and isinstance(inner[1], str) else ""
            title = inner[4] if len(inner) > 4 and isinstance(inner[4], str) else ""
            return {
                "id": note_id,
                "title": title or "Untitled",
                "content": content,
            }

        note_id = item[0]
        slot = item[1] if len(item) > 1 else None

        # Soft-deleted row: [id, None, 2]
        if slot is None and len(item) > 2 and item[2] == 2:
            return None

        # Legacy row: [id, content_string]
        if isinstance(slot, str):
            return {
                "id": note_id,
                "title": "Untitled",
                "content": slot,
            }

        # Current normalized row: [id, [id, content, metadata, None, title]]
        if isinstance(slot, list):
            content = slot[1] if len(slot) > 1 and isinstance(slot[1], str) else ""
            title = slot[4] if len(slot) > 4 and isinstance(slot[4], str) else ""
            return {
                "id": note_id,
                "title": title or "Untitled",
                "content": content,
            }

        return None

    @staticmethod
    def _mind_map_data(content: str) -> dict | None:
        if not isinstance(content, str) or not content.lstrip().startswith("{"):
            return None
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(parsed, dict) and ("children" in parsed or "nodes" in parsed):
            return parsed
        return None

    def _list_note_records(self, notebook_id: str) -> list[dict]:
        result = self._batchexecute(
            "cFji9",
            [notebook_id],
            source_path=f"/notebook/{notebook_id}",
        )
        records = []
        for item in self._note_rows_from_result(result):
            record = self._decode_note_row(item)
            if record is not None:
                records.append(record)
        return records

    def list_notes(self, notebook_id: str) -> list[dict]:
        """ノート一覧を取得（mind map rowsは除外）"""
        notes = []
        for record in self._list_note_records(notebook_id):
            if self._mind_map_data(record.get("content", "")) is None:
                notes.append(record)
        return notes

    def list_mindmaps(self, notebook_id: str) -> list[dict]:
        """Note rowsからnote-backed mind mapを抽出"""
        mindmaps = []
        for record in self._list_note_records(notebook_id):
            parsed = self._mind_map_data(record.get("content", ""))
            if parsed is None:
                continue
            mindmaps.append({
                **record,
                "data": parsed,
            })
        return mindmaps

    # ------------------------------------------------------------------
    # Download utility
    # ------------------------------------------------------------------

    def download_url(self, url: str, dest_path: str | Path) -> bool:
        """認証クッキー付きでURLからファイルをダウンロード"""
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        headers = {
            "User-Agent": USER_AGENT,
        }
        req = urllib.request.Request(url, headers=headers)

        partial_path: Path | None = None
        try:
            # The socket timeout bounds stalled headers/reads; the deadline
            # also bounds a connection that keeps trickling bytes forever.
            with contextlib.closing(
                self._opener.open(req, timeout=DOWNLOAD_SOCKET_TIMEOUT_SECONDS)
            ) as resp:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=f".{dest_path.name}.",
                    suffix=".part",
                    dir=dest_path.parent,
                    delete=False,
                ) as partial:
                    partial_path = Path(partial.name)
                    written = 0
                    deadline = time.monotonic() + DOWNLOAD_TOTAL_TIMEOUT_SECONDS
                    while True:
                        if time.monotonic() >= deadline:
                            return False
                        chunk = resp.read(DOWNLOAD_CHUNK_BYTES)
                        if not chunk:
                            break
                        partial.write(chunk)
                        written += len(chunk)

            if not written:
                return False
            # Do not overwrite an existing good backup until the new file
            # has completed downloading and closed successfully.
            partial_path.replace(dest_path)
            partial_path = None
            return True
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, http.client.HTTPException):
            return False
        finally:
            if partial_path is not None:
                partial_path.unlink(missing_ok=True)
