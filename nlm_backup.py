"""
nlm-backup: NotebookLM 一括バックアップツール

ソース（テキスト/画像/PDF）、アーティファクト（音声/動画/スライド等）、
ノートをまとめてダウンロードします。

Usage:
    nlm-backup <notebook-id>           # 指定ノートブックを丸ごとバックアップ
    nlm-backup --list                  # ノートブック一覧を表示
    nlm-backup --list --download       # 一覧から選んでダウンロード
    nlm-backup --all                   # 全ノートブックを一括バックアップ

外部依存: なし（Python 標準ライブラリのみ）
"""

import argparse
import json
import mimetypes
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from notebooklm_client import NotebookLMClient, NotebookLMError, AuthenticationError

# アーティファクトタイプ → デフォルト拡張子
ARTIFACT_EXTENSIONS = {
    "audio_overview": ".m4a",
    "video_overview": ".mp4",
    "slide_deck": ".pdf",
    "report": ".md",
    "data_table": ".csv",
    "flashcards": ".md",
    "mind_map": ".json",
    "fantasy_map": ".json",
    "infographic": ".png",
    "file": ".bin",
    "guided_view": ".json",
}


def sanitize_filename(name: str) -> str:
    """ファイル名に使えない文字を置換"""
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip()


def _unique_path(path: Path) -> Path:
    """既存パスと重複しないパスを返す"""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    i = 2
    while True:
        candidate = path.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
        i += 1


# ---------------------------------------------------------------------------
# ソースの保存
# ---------------------------------------------------------------------------

def save_text_source(client: NotebookLMClient, content: dict, out_dir: Path) -> Path:
    title = sanitize_filename(content.get("title", "untitled"))
    if not title.endswith(".md"):
        title = Path(title).stem + ".md"
    dest = _unique_path(out_dir / "sources" / title)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(content.get("content", ""))
    return dest


def save_image_source(client: NotebookLMClient, content: dict, out_dir: Path) -> list[Path]:
    title = sanitize_filename(content.get("title", "image"))
    raw = content.get("content", "")
    urls = re.findall(r'https://[^\s\])"\']+', raw)
    img_dir = out_dir / "sources"
    img_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    if len(urls) == 1:
        ext = Path(title).suffix or ".png"
        dest = _unique_path(img_dir / (Path(title).stem + ext))
        if client.download_url(urls[0], dest):
            saved.append(dest)
    else:
        for i, url in enumerate(urls, 1):
            dest = _unique_path(img_dir / f"{Path(title).stem}_p{i}.png")
            if client.download_url(url, dest):
                saved.append(dest)
    return saved


SOURCE_TEXT_TYPES = frozenset({
    "pasted_text",
    "web_page",
    "markdown",
    "youtube",
    "media",
    "powerpoint",
    "google_spreadsheet",
    "docx",
    "excel",
    "google_drive",
    "gmail",
    "csv",
    "epub",
    "gemini_chat",
    "ai_mode_chat",
    "expert_intelligence",
    "google_docs",
    "google_slides",
    "unknown",
})


def _source_original_path(source: dict, out_dir: Path) -> Path:
    title = sanitize_filename(source.get("title", "") or "")
    stem = Path(title).stem if title else source.get("id", "source")
    suffix = Path(title).suffix
    if not suffix:
        suffix = mimetypes.guess_extension(source.get("content_mime") or "") or {
            "pdf": ".pdf",
            "image": ".png",
            "media": ".bin",
        }.get(source.get("type"), ".bin")
    return _unique_path(out_dir / "sources" / f"{stem}{suffix}")


def _source_metadata_path(source: dict, out_dir: Path) -> Path:
    meta_dir = out_dir / "sources" / "_metadata"
    meta_dir.mkdir(parents=True, exist_ok=True)
    source_id = sanitize_filename(str(source.get("id") or "source"))
    return _unique_path(meta_dir / f"{source_id}.json")


def _write_source_metadata(source: dict, result: dict, out_dir: Path) -> Path:
    """Persist restore-safe source metadata without signed download capability URLs."""
    source_root = out_dir / "sources"
    files = []
    for path in result.get("paths", []):
        try:
            files.append(str(Path(path).relative_to(source_root)))
        except ValueError:
            files.append(str(Path(path)))

    payload = {
        "schema_version": 1,
        "id": source.get("id"),
        "title": source.get("title"),
        "type": source.get("type"),
        "type_code": source.get("type_code"),
        "raw_type_code": source.get("raw_type_code"),
        "status": source.get("status"),
        "status_code": source.get("status_code"),
        "url": source.get("url"),
        "content_mime": source.get("content_mime"),
        "drive_mime": source.get("drive_mime"),
        "backup_mode": result.get("mode"),
        "saved": bool(result.get("saved")),
        "files": files,
        "error": result.get("error"),
    }
    dest = _source_metadata_path(source, out_dir)
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return dest


def save_source(
    client: NotebookLMClient,
    source: dict,
    out_dir: Path,
    *,
    wait_timeout: float = 120.0,
) -> dict:
    """Save one source, preferring the original uploaded file when available."""
    source_id = source.get("id")
    if not source_id:
        result = {"saved": False, "mode": "missing-id", "paths": []}
        result["metadata_path"] = _write_source_metadata(source, result, out_dir)
        return result

    current = source
    status = current.get("status")
    if status in {"pending", "preparing"}:
        try:
            current = client.wait_for_source_ready(
                current.get("notebook_id") or "",
                source_id,
                timeout=wait_timeout,
            )
        except NotebookLMError as exc:
            result = {
                "saved": False,
                "mode": "not-ready",
                "paths": [],
                "error": str(exc),
            }
            result["metadata_path"] = _write_source_metadata(current, result, out_dir)
            return result
    elif status in {"error", "pending_deletion"}:
        result = {
            "saved": False,
            "mode": f"status-{status}",
            "paths": [],
        }
        result["metadata_path"] = _write_source_metadata(current, result, out_dir)
        return result

    try:
        original_url = current.get("download_url")
        if isinstance(original_url, str) and original_url.startswith("http"):
            dest = _source_original_path(current, out_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if client.download_url(original_url, dest):
                result = {"saved": True, "mode": "original", "paths": [dest]}
                result["metadata_path"] = _write_source_metadata(current, result, out_dir)
                return result
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass

        content = client.get_source_content(source_id)
        source_type = current.get("type", "unknown")
        if source_type == "image":
            paths = save_image_source(client, content, out_dir)
            result = {"saved": bool(paths), "mode": "rendered-image", "paths": paths}
        elif source_type == "pdf":
            paths = save_pdf_source(client, content, out_dir)
            result = {"saved": bool(paths), "mode": "rendered-pages", "paths": paths}
        else:
            path = save_text_source(client, content, out_dir)
            result = {"saved": True, "mode": "text", "paths": [path]}
    except (NotebookLMError, OSError) as exc:
        result = {
            "saved": False,
            "mode": "exception",
            "paths": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
        result["metadata_path"] = _write_source_metadata(current, result, out_dir)
        raise

    result["metadata_path"] = _write_source_metadata(current, result, out_dir)
    return result


def save_pdf_source(client: NotebookLMClient, content: dict, out_dir: Path) -> list[Path]:
    title = sanitize_filename(content.get("title", "document"))
    stem = Path(title).stem
    pdf_dir = _unique_path(out_dir / "sources" / stem)
    pdf_dir.mkdir(parents=True, exist_ok=True)

    raw = content.get("content", "")
    urls = re.findall(r'https://[^\s\])"\']+', raw)

    saved = []
    for i, url in enumerate(urls, 1):
        dest = pdf_dir / f"page{i}.png"
        if client.download_url(url, dest):
            saved.append(dest)
    return saved


# ---------------------------------------------------------------------------
# アーティファクトの保存
# ---------------------------------------------------------------------------

def save_artifact_raw_snapshot(artifact: dict, art_dir: Path, stem: str) -> Path:
    """Studio artifact の生 payload を将来の再解析用に JSON 保存"""
    raw_dir = art_dir / "_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_path(raw_dir / f"{stem}.json")
    snapshot = {
        "id": artifact.get("id"),
        "title": artifact.get("title"),
        "type": artifact.get("type"),
        "type_code": artifact.get("type_code"),
        "variant": artifact.get("variant"),
        "status": artifact.get("status"),
        "status_code": artifact.get("status_code"),
        "raw": artifact.get("_raw"),
    }
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    return dest


def _artifact_stem(artifact: dict) -> str:
    raw_title = sanitize_filename(artifact.get("title", "") or "")
    if raw_title:
        return Path(raw_title).stem
    return artifact.get("variant") or artifact.get("type", "artifact") or "artifact"


def _artifact_extension(artifact: dict) -> str:
    art_type = artifact.get("type", "unknown")
    variant = artifact.get("variant")
    if variant == "interactive_mind_map":
        return ".md"
    if (
        artifact.get("structured_content") is not None
        and not artifact.get("content")
        and not artifact.get("app_html")
        and artifact.get("type_code") != 4
    ):
        return ".json"
    return ARTIFACT_EXTENSIONS.get(art_type, ".bin")


def save_artifact(client: NotebookLMClient, artifact: dict, out_dir: Path) -> dict:
    """単一Studio artifactをCLI/TUI共通ルールで保存する"""
    art_dir = out_dir / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)

    stem = _artifact_stem(artifact)
    raw_snapshot = save_artifact_raw_snapshot(artifact, art_dir, stem)
    result = {
        "saved": False,
        "status": artifact.get("status", ""),
        "raw_snapshot": raw_snapshot,
        "dest": None,
        "pptx_saved": None,
        "pptx_dest": None,
        "pages_count": None,
    }

    if artifact.get("status") != "completed":
        return result

    ext = _artifact_extension(artifact)
    dest = _unique_path(art_dir / f"{stem}{ext}")
    result["dest"] = dest
    result["saved"] = client.download_artifact(artifact, dest)

    if artifact.get("pptx_url"):
        pptx_dest = _unique_path(dest.with_suffix(".pptx"))
        result["pptx_dest"] = pptx_dest
        result["pptx_saved"] = client.download_artifact_pptx(artifact, pptx_dest)

    if artifact.get("page_images"):
        pages_dir = art_dir / dest.stem
        result["pages_count"] = len(client.download_artifact_pages(artifact, pages_dir))

    return result


def save_artifacts(client: NotebookLMClient, artifacts: list[dict], out_dir: Path) -> int:
    """アーティファクトをダウンロード"""
    art_dir = out_dir / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    count = 0

    for art in artifacts:
        art_type = art.get("type", "unknown")
        result = save_artifact(client, art, out_dir)
        raw_snapshot = result["raw_snapshot"]

        if result["status"] != "completed":
            print(
                f"    [{art_type}] (status: {result['status']}, skipped; "
                f"raw: {raw_snapshot.relative_to(art_dir)})"
            )
            continue

        dest = result["dest"]
        print(f"    [{art_type}] → {dest.name} ... ", end="", flush=True)
        if result["saved"]:
            print("OK")
            count += 1
        else:
            print(f"FAIL (raw preserved: {raw_snapshot.relative_to(art_dir)})")

        if result["pptx_saved"] is not None:
            print(
                f"    [{art_type}] → {result['pptx_dest'].name} ... "
                f"{'OK' if result['pptx_saved'] else 'FAIL'}"
            )

        if result["pages_count"] is not None:
            print(
                f"    [{art_type}] → {dest.stem}/ (pages) ... "
                f"OK ({result['pages_count']} pages)"
            )

    return count


# ---------------------------------------------------------------------------
# ノートの保存
# ---------------------------------------------------------------------------

def save_notes(notes: list[dict], out_dir: Path) -> int:
    note_dir = out_dir / "notes"
    note_dir.mkdir(parents=True, exist_ok=True)
    meta_dir = note_dir / "_metadata"
    meta_dir.mkdir(parents=True, exist_ok=True)
    count = 0

    for note in notes:
        title = sanitize_filename(note.get("title", "untitled"))
        content = note.get("content", "")
        if not title.endswith(".md"):
            title += ".md"
        dest = note_dir / title
        # 同名ファイルがある場合はナンバリング
        if dest.exists():
            i = 2
            stem = Path(title).stem
            while dest.exists():
                dest = note_dir / f"{stem}_{i}.md"
                i += 1
        print(f"    {dest.name} ... ", end="", flush=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(content)
        note_meta = {
            "schema_version": 1,
            "id": note.get("id"),
            "title": note.get("title", "Untitled"),
            "file": str(dest.relative_to(note_dir)),
        }
        meta_name = sanitize_filename(str(note.get("id") or dest.stem)) + ".json"
        with open(_unique_path(meta_dir / meta_name), "w", encoding="utf-8") as f:
            json.dump(note_meta, f, ensure_ascii=False, indent=2)
        print("OK")
        count += 1
    return count


# ---------------------------------------------------------------------------
# マインドマップの保存
# ---------------------------------------------------------------------------

def mindmap_to_markdown(node: dict, indent: int = 0) -> str:
    """マインドマップのツリー構造を Markdown リストに変換"""
    prefix = "  " * indent + "- "
    lines = [prefix + node.get("name", "")]
    for child in node.get("children", []):
        lines.append(mindmap_to_markdown(child, indent + 1))
    return "\n".join(lines)


def save_mindmaps(mindmaps: list[dict], out_dir: Path) -> int:
    if not mindmaps:
        return 0
    mm_dir = out_dir / "mindmaps"
    mm_dir.mkdir(parents=True, exist_ok=True)
    meta_dir = mm_dir / "_metadata"
    meta_dir.mkdir(parents=True, exist_ok=True)
    count = 0

    for mm in mindmaps:
        title = sanitize_filename(mm.get("title", "untitled"))
        data = mm.get("data", {})

        # JSON として保存
        json_name = title if title.endswith(".json") else title + ".json"
        json_dest = _unique_path(mm_dir / json_name)
        print(f"    {json_dest.name} ... ", end="", flush=True)
        try:
            with open(json_dest, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print("OK")
        except OSError:
            print("FAIL")
            continue

        # Markdown としても保存
        md_name = Path(json_dest.stem).stem + ".md"
        md_dest = _unique_path(mm_dir / md_name)
        try:
            md_content = f"# {data.get('name', title)}\n\n{mindmap_to_markdown(data)}\n"
            with open(md_dest, "w", encoding="utf-8") as f:
                f.write(md_content)
        except OSError:
            pass

        mm_meta = {
            "schema_version": 1,
            "id": mm.get("id"),
            "title": mm.get("title", "Untitled"),
            "json_file": str(json_dest.relative_to(mm_dir)),
            "markdown_file": str(md_dest.relative_to(mm_dir)),
        }
        meta_name = sanitize_filename(str(mm.get("id") or json_dest.stem)) + ".json"
        with open(_unique_path(meta_dir / meta_name), "w", encoding="utf-8") as f:
            json.dump(mm_meta, f, ensure_ascii=False, indent=2)

        count += 1
    return count


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------

def download_notebook(client: NotebookLMClient, notebook_id: str, out_base: Path,
                      notebooks: list[dict] | None = None):
    """ノートブックの全データをダウンロード"""
    # タイトル取得
    title = notebook_id
    if notebooks:
        for nb in notebooks:
            if nb["id"] == notebook_id:
                title = nb["title"]
                break
    if title == notebook_id:
        # notebooks リストがない場合、API から取得
        all_nbs = client.list_notebooks()
        for nb in all_nbs:
            if nb["id"] == notebook_id:
                title = nb["title"]
                notebooks = all_nbs
                break

    safe_title = sanitize_filename(title)
    out_dir = out_base / safe_title
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"  ID: {notebook_id}")
    print(f"  保存先: {out_dir}")
    print(f"{'='*60}")

    # --- メタデータ保存 ---
    meta = {
        "id": notebook_id,
        "title": title,
        "backup_schema_version": 2,
        "restore_semantics": {
            "sources": "semantic when source sidecars are present",
            "notes": "native notes",
            "mindmaps": "note-backed JSON restore",
            "studio_artifacts": "local backup only; not recreated from local files",
        },
    }
    if notebooks:
        for nb in notebooks:
            if nb["id"] == notebook_id:
                meta.update(nb)
                break
    with open(out_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # --- ソース ---
    sources = client.list_sources(notebook_id)

    print(f"\n  [Sources] {len(sources)} 件")
    src_ok = 0
    for src in sources:
        src = dict(src)
        src["notebook_id"] = notebook_id
        src_type = src.get("type", "unknown")
        src_title = src.get("title", "untitled")
        src_id = src["id"]

        print(f"    [{src_type}] {src_title} ... ", end="", flush=True)

        try:
            result = save_source(client, src, out_dir)
        except (NotebookLMError, OSError) as e:
            print(f"FAIL ({e})")
            continue

        if result["saved"]:
            detail = result["mode"]
            if result["paths"]:
                detail += f", {len(result['paths'])} file(s)"
            print(f"OK ({detail})")
            src_ok += 1
        else:
            print(f"FAIL ({result['mode']})")

    # --- アーティファクト ---
    artifacts = client.list_artifacts(notebook_id)
    print(f"\n  [Artifacts] {len(artifacts)} 件")
    art_ok = save_artifacts(client, artifacts, out_dir) if artifacts else 0

    # --- ノート ---
    notes = client.list_notes(notebook_id)
    print(f"\n  [Notes] {len(notes)} 件")
    note_ok = save_notes(notes, out_dir) if notes else 0

    # --- マインドマップ ---
    mindmaps = client.list_mindmaps(notebook_id)
    print(f"\n  [Mindmaps] {len(mindmaps)} 件")
    mm_ok = save_mindmaps(mindmaps, out_dir) if mindmaps else 0

    # --- サマリー ---
    print(f"\n  --- 完了 ---")
    print(f"  Sources:   {src_ok}/{len(sources)}")
    print(f"  Artifacts: {art_ok}/{len(artifacts)}")
    print(f"  Notes:     {note_ok}/{len(notes)}")
    print(f"  Mindmaps:  {mm_ok}/{len(mindmaps)}")
    print(f"  → {out_dir}\n")


def interactive_select(notebooks: list[dict]) -> str | None:
    print("\n番号を入力してダウンロード (q: 終了):")
    try:
        choice = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if choice.lower() == "q":
        return None
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(notebooks):
            return notebooks[idx]["id"]
    except ValueError:
        pass
    print("[ERROR] 無効な番号です")
    return None


def format_timestamp(ts) -> str:
    """Unix タイムスタンプを日付文字列に変換"""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return str(ts)[:10]


def print_notebook_list(notebooks: list[dict]):
    print(f"\n{'No':>3}  {'Src':>4}  {'Updated':>12}  Title")
    print("-" * 70)
    for i, nb in enumerate(notebooks, 1):
        updated = format_timestamp(nb.get("updated_at"))
        print(f"{i:>3}  {nb['source_count']:>4}  {updated:>12}   {nb['title']}")


def main():
    parser = argparse.ArgumentParser(
        prog="nlm-backup",
        description="NotebookLM 一括バックアップツール",
    )
    parser.add_argument("notebook_id", nargs="?", help="ノートブックID")
    parser.add_argument("--list", action="store_true", help="ノートブック一覧を表示")
    parser.add_argument("--download", action="store_true", help="--list と併用: 選択してダウンロード")
    parser.add_argument("--all", action="store_true", help="全ノートブックを一括バックアップ")
    parser.add_argument("-o", "--output", type=str, default=None, help="出力ディレクトリ (default: ./downloads)")
    parser.add_argument("--cookies", type=str, default=None, help="クッキーファイルのパス")
    args = parser.parse_args()

    out_base = Path(args.output) if args.output else Path.cwd() / "downloads"

    try:
        client = NotebookLMClient(cookies_path=args.cookies)
    except AuthenticationError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    if args.list or (not args.notebook_id and not args.all):
        notebooks = client.list_notebooks()
        print_notebook_list(notebooks)

        if args.download:
            nb_id = interactive_select(notebooks)
            if nb_id:
                download_notebook(client, nb_id, out_base, notebooks)
        elif not args.notebook_id:
            print(f"\n使い方:")
            print(f"  nlm-backup <notebook-id>")
            print(f"  nlm-backup --list --download")
            print(f"  nlm-backup --all")
        return

    if args.all:
        notebooks = client.list_notebooks()
        print(f"\n全 {len(notebooks)} ノートブックをバックアップします...")
        for nb in notebooks:
            download_notebook(client, nb["id"], out_base, notebooks)
        print(f"\n全ノートブックのバックアップが完了しました → {out_base}")
        return

    download_notebook(client, args.notebook_id, out_base)


if __name__ == "__main__":
    main()
