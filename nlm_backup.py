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
    "infographic": ".png",
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

def save_artifacts(client: NotebookLMClient, artifacts: list[dict], out_dir: Path) -> int:
    """アーティファクトをダウンロード"""
    art_dir = out_dir / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    count = 0

    for art in artifacts:
        art_type = art.get("type", "unknown")
        status = art.get("status", "")

        if status != "completed":
            print(f"    [{art_type}] (status: {status}, skipped)")
            continue

        ext = ARTIFACT_EXTENSIONS.get(art_type, ".bin")
        raw_title = sanitize_filename(art.get("title", "") or "")
        stem = Path(raw_title).stem if raw_title else art_type
        if not stem:
            stem = art_type
        dest = art_dir / f"{stem}{ext}"

        # 同名ファイルがある場合はナンバリング
        if dest.exists():
            i = 2
            while dest.exists():
                dest = art_dir / f"{stem}_{i}{ext}"
                i += 1

        print(f"    [{art_type}] → {dest.name} ... ", end="", flush=True)

        if client.download_artifact(art, dest):
            print("OK")
            count += 1
        else:
            print("FAIL")

        # スライドデッキの PPTX をダウンロード
        if art.get("pptx_url"):
            pptx_dest = dest.with_suffix(".pptx")
            print(f"    [{art_type}] → {pptx_dest.name} ... ", end="", flush=True)
            if client.download_artifact_pptx(art, pptx_dest):
                print("OK")
            else:
                print("FAIL")

        # スライドデッキのページ画像をダウンロード
        if art.get("page_images"):
            stem = dest.stem
            pages_dir = art_dir / stem
            print(f"    [{art_type}] → {stem}/ (pages) ... ", end="", flush=True)
            pages = client.download_artifact_pages(art, pages_dir)
            print(f"OK ({len(pages)} pages)")

    return count


# ---------------------------------------------------------------------------
# ノートの保存
# ---------------------------------------------------------------------------

def save_notes(notes: list[dict], out_dir: Path) -> int:
    note_dir = out_dir / "notes"
    note_dir.mkdir(parents=True, exist_ok=True)
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
        print("OK")
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
    meta = {"id": notebook_id, "title": title}
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
        src_type = src.get("type", "unknown")
        src_title = src.get("title", "untitled")
        src_id = src["id"]

        print(f"    [{src_type}] {src_title} ... ", end="", flush=True)

        try:
            content = client.get_source_content(src_id)
        except NotebookLMError as e:
            print(f"FAIL ({e})")
            continue

        if src_type in ("text", "generated_text", "website", "document"):
            save_text_source(client, content, out_dir)
            print("OK")
            src_ok += 1
        elif src_type == "image":
            paths = save_image_source(client, content, out_dir)
            print(f"OK ({len(paths)} file)")
            src_ok += 1
        elif src_type == "pdf":
            paths = save_pdf_source(client, content, out_dir)
            print(f"OK ({len(paths)} pages)")
            src_ok += 1
        else:
            save_text_source(client, content, out_dir)
            print("OK (as text)")
            src_ok += 1

    # --- アーティファクト ---
    artifacts = client.list_artifacts(notebook_id)
    print(f"\n  [Artifacts] {len(artifacts)} 件")
    art_ok = save_artifacts(client, artifacts, out_dir) if artifacts else 0

    # --- ノート ---
    notes = client.list_notes(notebook_id)
    print(f"\n  [Notes] {len(notes)} 件")
    note_ok = save_notes(notes, out_dir) if notes else 0

    # --- サマリー ---
    print(f"\n  --- 完了 ---")
    print(f"  Sources:   {src_ok}/{len(sources)}")
    print(f"  Artifacts: {art_ok}/{len(artifacts)}")
    print(f"  Notes:     {note_ok}/{len(notes)}")
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
