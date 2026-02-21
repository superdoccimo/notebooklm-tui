"""
NotebookLM API Client

Google NotebookLM の内部 batchexecute API を直接操作する軽量クライアント。
Python 標準ライブラリのみで動作し、外部パッケージ依存はゼロ。

認証には nlm login で作成されたクッキーファイル、または手動エクスポートした
クッキーを使用します。
"""

import http.cookiejar
import json
import re
import ssl
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://notebooklm.google.com"
BATCHEXECUTE_URL = f"{BASE_URL}/_/LabsTailwindUi/data/batchexecute"
UPLOAD_URL = f"{BASE_URL}/upload/_/?authuser=0"
DEFAULT_BUILD_LABEL = "boq_labs-tailwind-frontend_20260108.06_p0"
BUILD_LABEL_PATTERN = re.compile(r"\bboq_[A-Za-z0-9_-]+_[0-9]{8}\.[0-9]+_p[0-9]+\b")

DEFAULT_COOKIES_PATH = Path.home() / ".notebooklm-mcp-cli" / "profiles" / "default" / "cookies.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
)

# Source type codes from NotebookLM's internal enum
SOURCE_TYPES = {
    0: "text",
    1: "pdf",
    2: "generated_text",
    3: "pdf",           # uploaded PDF
    4: "website",
    5: "youtube",
    6: "audio",
    8: "document",      # uploaded text file (.md, .txt, etc.)
    9: "image",
    11: "google_doc",
    12: "google_slides",
    13: "image",         # uploaded image (.png, .jpg, etc.)
}

# Artifact type codes
ARTIFACT_TYPES = {
    1: "audio_overview",
    2: "report",
    3: "video_overview",
    4: "flashcards",  # also quiz (distinguished by sub-format)
    7: "infographic",
    8: "slide_deck",
    9: "data_table",
}

ARTIFACT_STATUS = {
    1: "in_progress",
    3: "completed",
    4: "failed",
}


class NotebookLMError(Exception):
    pass


class AuthenticationError(NotebookLMError):
    pass


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
            if domain in (".google.com", "notebooklm.google.com"):
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
        params = [
            title, None, None, [2],
            [1, None, None, None, None, None, None, None, None, None, [1]],
        ]
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
            [notebook_id, None, [2], None, 0],
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
                src_id = src[0][0] if isinstance(src[0], list) else src[0]
                title = src[1] or "Untitled"
                meta = src[2] if len(src) > 2 and isinstance(src[2], list) else []

                # ソースタイプの判定
                type_code = None
                if meta and len(meta) > 4:
                    type_code = meta[4]
                source_type = SOURCE_TYPES.get(type_code, "unknown")

                # URL（もしあれば）
                url = None
                if meta and len(meta) > 7 and isinstance(meta[7], list) and meta[7]:
                    url = meta[7][0]

                sources.append({
                    "id": src_id,
                    "title": title,
                    "type": source_type,
                    "type_code": type_code,
                    "url": url,
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

        # ソースタイプ
        source_type = "unknown"
        try:
            type_code = result[0][2][4]
            source_type = SOURCE_TYPES.get(type_code, "unknown")
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

    def _extract_text_recursive(self, data, parts: list):
        """ネストされた構造からテキスト文字列を再帰的に抽出"""
        if isinstance(data, str):
            if data.strip():
                parts.append(data)
        elif isinstance(data, list):
            for item in data:
                self._extract_text_recursive(item, parts)

    def add_source_url(self, notebook_id: str, url: str) -> str | None:
        """URLソースを追加"""
        source_data = [None, None, [url], None, None, None, None, None, None, None, 1]
        params = [
            [source_data], notebook_id, [2],
            [1, None, None, None, None, None, None, None, None, None, [1]],
        ]
        result = self._batchexecute("izAoDd", params, source_path=f"/notebook/{notebook_id}")
        try:
            return result[0][0][0][0]
        except (IndexError, TypeError):
            return None

    def add_source_text(self, notebook_id: str, title: str, text: str) -> str | None:
        """テキストソースを追加"""
        source_data = [None, [title, text], None, 2, None, None, None, None, None, None, 1]
        params = [
            [source_data], notebook_id, [2],
            [1, None, None, None, None, None, None, None, None, None, [1]],
        ]
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

        # Step 1: ファイルソースを登録 → source_id 取得
        params = [
            [[filename]], notebook_id, [2],
            [1, None, None, None, None, None, None, None, None, None, [1]],
        ]
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
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
            "User-Agent": USER_AGENT,
            "x-goog-authuser": "0",
            "x-goog-upload-command": "start",
            "x-goog-upload-header-content-length": str(file_size),
            "x-goog-upload-protocol": "resumable",
        }
        req = urllib.request.Request(UPLOAD_URL, data=upload_meta.encode("utf-8"), headers=headers, method="POST")
        resp = self._opener.open(req)
        upload_url = resp.headers.get("x-goog-upload-url")
        if not upload_url:
            raise NotebookLMError("アップロードURLの取得に失敗しました")

        # Step 3: ファイルバイナリを送信（ストリーミング）
        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            "Content-Length": str(file_size),
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
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
            try:
                art_id = art[0]
                title = art[1] or "Untitled"
                type_code = art[2]
                status_code = art[4] if len(art) > 4 else None

                art_type = ARTIFACT_TYPES.get(type_code, f"unknown_{type_code}")
                status = ARTIFACT_STATUS.get(status_code, "unknown")

                # ダウンロードURL/コンテンツの抽出
                download_info = self._extract_artifact_download(art, type_code)

                artifacts.append({
                    "id": art_id,
                    "title": title,
                    "type": art_type,
                    "type_code": type_code,
                    "status": status,
                    "status_code": status_code,
                    "_raw": art,  # ダウンロード時に使用
                    **download_info,
                })
            except (IndexError, TypeError):
                continue
        return artifacts

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
            elif type_code == 2:  # report
                if len(art) > 7 and art[7] and art[7][0]:
                    info["content"] = art[7][0]
            elif type_code == 3:  # video
                self._find_media_url(art[8] if len(art) > 8 else [], "video/mp4", info)
            elif type_code == 7:  # infographic
                if len(art) > 14 and art[14]:
                    info["download_url"] = art[14][2][0][1][0]
            elif type_code == 8:  # slide_deck
                if len(art) > 16 and art[16] and len(art[16]) > 3:
                    info["download_url"] = art[16][3]
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

    def download_artifact(self, artifact: dict, dest_path: str | Path) -> bool:
        """アーティファクトをファイルにダウンロード"""
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # インラインコンテンツの場合（report, data_table）
        if "content" in artifact and artifact["content"]:
            with open(dest_path, "w", encoding="utf-8") as f:
                f.write(artifact["content"])
            return True

        # URLダウンロードの場合
        if "download_url" in artifact and artifact["download_url"]:
            return self.download_url(artifact["download_url"], dest_path)

        return False

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

    def list_notes(self, notebook_id: str) -> list[dict]:
        """ノート一覧を取得"""
        result = self._batchexecute(
            "cFji9", [notebook_id],
            source_path=f"/notebook/{notebook_id}",
        )
        if not result or not isinstance(result[0], list):
            return []

        notes = []
        for item in result[0]:
            try:
                if not isinstance(item, list) or len(item) < 2:
                    continue
                note_id = item[0]
                detail = item[1]
                if detail is None:
                    continue  # 削除済み

                content = detail[1] if len(detail) > 1 else ""
                title = detail[4] if len(detail) > 4 else ""

                # マインドマップを除外（JSONコンテンツ）
                if isinstance(content, str):
                    try:
                        parsed = json.loads(content)
                        if isinstance(parsed, dict) and ("children" in parsed or "nodes" in parsed):
                            continue
                    except (json.JSONDecodeError, TypeError):
                        pass

                notes.append({
                    "id": note_id,
                    "title": title or "Untitled",
                    "content": content or "",
                })
            except (IndexError, TypeError):
                continue
        return notes

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
