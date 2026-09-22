"""
NotebookLM API Client

Google NotebookLM の内部 batchexecute API を直接操作する軽量クライアント。
Python 標準ライブラリのみで動作し、外部パッケージ依存はゼロ。

認証には nlm login で作成されたクッキーファイル、または手動エクスポートした
クッキーを使用します。
"""

import http.cookiejar
import html as html_lib
import json
import mimetypes
import os
import re
import ssl
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
}

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
        """type 4 アーティファクトのサブタイプを返す"""
        try:
            return APP_ARTIFACT_TYPES.get(art[9][1][0])
        except (IndexError, TypeError):
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
        download_info = self._extract_artifact_download(art, type_code)

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
        """type 4 HTML から app-root の JSON データを抽出"""
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
        """詳細 HTML から得たデータを含めて type 4 の種別を判定"""
        variant = artifact.get("variant")
        if variant:
            return variant
        if isinstance(app_data, dict):
            if "flashcards" in app_data:
                return "flashcards"
            if "quiz" in app_data:
                return "quiz"
        return artifact.get("type", "flashcards")

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
                for nested_key in ("text", "value", "answer", "label"):
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
        return self._first_artifact_text(
            item,
            ("questionType", "question_type", "format", "kind", "type"),
        )

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
        ):
            value = item.get(key)
            if not isinstance(value, list):
                continue
            for answer in value:
                if isinstance(answer, dict):
                    text = self._first_artifact_text(
                        answer,
                        ("text", "value", "answer", "label"),
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

    def _render_flashcards_markdown(
        self,
        title: str,
        cards: list[dict],
        html_name: str,
        json_name: str,
    ) -> str:
        lines = [
            f"# {title}",
            "",
            "- Artifact subtype: flashcards",
            f"- Card count: {len(cards)}",
            f"- Original HTML: `{html_name}`",
            f"- Structured data: `{json_name}`",
        ]
        for i, card in enumerate(cards, 1):
            front = self._normalize_artifact_text(card.get("f"))
            back = self._normalize_artifact_text(card.get("b"))
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
        return "\n".join(lines).rstrip() + "\n"

    def _render_quiz_markdown(
        self,
        title: str,
        questions: list[dict],
        html_name: str,
        json_name: str,
    ) -> str:
        lines = [
            f"# {title}",
            "",
            "- Artifact subtype: quiz",
            f"- Question count: {len(questions)}",
            f"- Original HTML: `{html_name}`",
            f"- Structured data: `{json_name}`",
        ]
        for i, item in enumerate(questions, 1):
            question = self._first_artifact_text(item, ("question", "prompt", "stem"))
            hint = self._first_artifact_text(item, ("hint",))
            question_format = self._quiz_question_format(item)
            options = self._quiz_options(item)
            correct_answers = self._quiz_correct_answers(item, options)
            explanation = self._first_artifact_text(
                item,
                ("rationale", "explanation", "feedback"),
            )
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
            if hint:
                lines.extend(["", f"Hint: {hint}"])
            if correct_answers:
                label = "Correct answers" if len(correct_answers) > 1 else "Correct answer"
                lines.extend(["", f"{label}: {', '.join(correct_answers)}"])
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
        name = self._normalize_artifact_text(tree.get("name")) or "(untitled)"
        lines = []
        if indent == 0:
            lines.extend([f"# {title}", "", "- Artifact subtype: interactive_mind_map", ""])
        lines.append("  " * indent + f"- {name}")
        for child in tree.get("children", []) or []:
            if isinstance(child, dict):
                child_md = self._render_mind_map_markdown(title, child, indent + 1)
                if indent + 1 > 0:
                    child_lines = child_md.splitlines()
                    if child_lines and child_lines[0].startswith("# "):
                        child_lines = child_lines[4:]
                    lines.extend(child_lines)
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
                cards = [c for c in app_data.get("flashcards", []) if isinstance(c, dict)]
                return self._render_flashcards_markdown(title, cards, html_name, json_name)
            if variant == "quiz":
                questions = [q for q in app_data.get("quiz", []) if isinstance(q, dict)]
                return self._render_quiz_markdown(title, questions, html_name, json_name)

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

    def _extract_artifact_download(self, art: list, type_code: int) -> dict:
        """アーティファクトからダウンロード情報を抽出"""
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
                            app_data = self._extract_app_artifact_data(payload)
                            if app_data is not None:
                                info["app_data"] = app_data
                        else:
                            info["content"] = payload
                    elif payload is not None:
                        # Newly introduced report formats may return structured data.
                        # Keep it exportable even before their schema is fully understood.
                        info["structured_content"] = payload
            elif type_code == 3:  # video
                self._find_media_url(art[8] if len(art) > 8 else [], "video/mp4", info)
            elif type_code == 7:  # infographic
                if len(art) > 14 and art[14]:
                    info["download_url"] = art[14][2][0][1][0]
            elif type_code == 8:  # slide_deck
                if len(art) > 16 and art[16] and len(art[16]) > 3:
                    info["download_url"] = art[16][3]
                    # PPTX download URL (art[16][4], available on newer slides)
                    if len(art[16]) > 4 and isinstance(art[16][4], str) and art[16][4].startswith("http"):
                        info["pptx_url"] = art[16][4]
                    # 各ページの画像URLを抽出
                    pages = art[16][2] if len(art[16]) > 2 and isinstance(art[16][2], list) else []
                    page_images = []
                    for page in pages:
                        try:
                            if isinstance(page, list) and page[0] and isinstance(page[0], list):
                                page_images.append(page[0][0])  # image URL
                        except (IndexError, TypeError):
                            continue
                    if page_images:
                        info["page_images"] = page_images
            elif type_code == 4:  # flashcards / quiz / interactive mind map
                variant = self._artifact_variant_from_raw(art)
                if variant == "interactive_mind_map":
                    tree = self._extract_interactive_mind_map_data(art)
                    if tree is not None:
                        info["structured_content"] = tree
                else:
                    html_content = art[9][0] if len(art) > 9 and art[9] else ""
                    if isinstance(html_content, str) and "<html" in html_content.lower():
                        info["app_html"] = html_content
                        app_data = self._extract_app_artifact_data(html_content)
                        if app_data is not None:
                            info["app_data"] = app_data
            elif type_code == 9:  # data_table
                try:
                    info["content"] = self._extract_data_table(art[18])
                except (IndexError, TypeError):
                    pass
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
        """type 4 artifact を subtype に応じて Markdown + JSON/HTML で保存"""
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
        if not isinstance(html_content, str) or not html_content:
            return False

        app_data = detail.get("app_data")
        if app_data is None:
            app_data = self._extract_app_artifact_data(html_content)

        html_dest = dest_path.with_suffix(".html")
        json_dest = dest_path.with_suffix(".json")
        markdown = self._render_app_artifact_markdown(detail, app_data, html_dest.name, json_dest.name)

        with open(dest_path, "w", encoding="utf-8") as f:
            f.write(markdown)
        with open(html_dest, "w", encoding="utf-8") as f:
            f.write(html_content)
        with open(json_dest, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "id": detail.get("id"),
                    "title": detail.get("title"),
                    "type": detail.get("type"),
                    "variant": self._artifact_variant_from_data(detail, app_data),
                    "status": detail.get("status"),
                    "data": app_data,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        return True

    def download_artifact(self, artifact: dict, dest_path: str | Path) -> bool:
        """アーティファクトをファイルにダウンロード"""
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        if artifact.get("type_code") == 4 or artifact.get("app_html"):
            return self._download_app_artifact(artifact, dest_path)

        # インラインコンテンツの場合（report, data_table）
        if "content" in artifact and artifact["content"]:
            with open(dest_path, "w", encoding="utf-8") as f:
                f.write(artifact["content"])
            return True

        # 未知/新形式の構造化コンテンツは JSON として保全
        if "structured_content" in artifact and artifact["structured_content"] is not None:
            with open(dest_path, "w", encoding="utf-8") as f:
                json.dump(artifact["structured_content"], f, ensure_ascii=False, indent=2)
            return True

        # URLダウンロードの場合
        if "download_url" in artifact and artifact["download_url"]:
            return self.download_url(artifact["download_url"], dest_path)

        # Known-but-not-yet-rendered artifact families are still preserved as
        # a first-class JSON export instead of being reported as a total loss.
        if artifact.get("type") in {"guided_view", "fantasy_map", "file", "mind_map"}:
            with open(dest_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "id": artifact.get("id"),
                        "title": artifact.get("title"),
                        "type": artifact.get("type"),
                        "type_code": artifact.get("type_code"),
                        "variant": artifact.get("variant"),
                        "status": artifact.get("status"),
                        "raw": artifact.get("_raw"),
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

        try:
            resp = self._opener.open(req)
            written = 0
            with open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)  # 1MB チャンク
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
            if written == 0:
                dest_path.unlink(missing_ok=True)
                return False
            return True
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            return False
