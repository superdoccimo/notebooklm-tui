"""
nlm-upload: NotebookLM 一括アップロードツール

ローカルのファイルやフォルダを NotebookLM にまとめてアップロードします。
新規ノートブック作成 → ソース追加をワンコマンドで実行できます。

Usage:
    nlm-upload "My Notebook" file1.pdf file2.md          # 新規作成してアップロード
    nlm-upload "My Notebook" ./my_folder/                 # フォルダ内を一括アップロード
    nlm-upload --to <notebook-id> file1.pdf               # 既存ノートブックに追加
    nlm-upload --to <notebook-id> --url https://example.com  # URLを追加
    nlm-upload --restore ./downloads/My_Notebook/         # バックアップから復元

外部依存: なし（Python 標準ライブラリのみ）
"""

import argparse
import json
import sys
from pathlib import Path

from notebooklm_client import NotebookLMClient, NotebookLMError, AuthenticationError

# テキストとして読み込んで add_source_text で追加する拡張子
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm"}

# ファイルアップロード（resumable upload）が対応する拡張子
UPLOAD_FILE_TYPES = {
    ".pdf",
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".mp3", ".wav", ".m4a", ".ogg", ".flac",
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
}


def collect_files(paths: list[str]) -> list[Path]:
    """パスリストからアップロード対象ファイルを収集"""
    files = []
    for p in paths:
        path = Path(p)
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            for f in sorted(path.rglob("*")):
                if f.is_file() and not f.name.startswith("."):
                    files.append(f)
        else:
            print(f"  [WARN] 見つかりません: {p}", file=sys.stderr)
    return files


def upload_files(client: NotebookLMClient, notebook_id: str, files: list[Path]) -> tuple[int, int]:
    """ファイルリストをノートブックにアップロード"""
    ok = 0
    fail = 0

    for f in files:
        ext = f.suffix.lower()
        print(f"  {f.name} ... ", end="", flush=True)

        try:
            if ext in TEXT_EXTENSIONS:
                try:
                    text = f.read_text(encoding="utf-8")
                    source_id = client.add_source_text(notebook_id, f.name, text)
                    success_label = "OK (as text)"
                except UnicodeDecodeError:
                    source_id = client.upload_file(notebook_id, f)
                    success_label = "OK (as file)"
                if source_id:
                    print(success_label)
                    ok += 1
                else:
                    print("FAIL")
                    fail += 1
            elif ext in UPLOAD_FILE_TYPES:
                source_id = client.upload_file(notebook_id, f)
                if source_id:
                    print("OK")
                    ok += 1
                else:
                    print("FAIL")
                    fail += 1
            else:
                print(f"SKIP (unsupported: {ext})")
                fail += 1
        except NotebookLMError as e:
            print(f"FAIL ({e})")
            fail += 1

    return ok, fail


def restore_backup(client: NotebookLMClient, backup_dir: Path) -> bool:
    """バックアップディレクトリからノートブックを復元"""
    meta_path = backup_dir / "metadata.json"
    if not meta_path.exists():
        print(f"[ERROR] metadata.json が見つかりません: {backup_dir}", file=sys.stderr)
        return False

    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)

    title = meta.get("title", backup_dir.name)
    print(f"\n=== 復元: {title} ===")

    # 新規ノートブック作成
    print(f"  ノートブック作成中: {title} ... ", end="", flush=True)
    try:
        notebook_id = client.create_notebook(title)
    except NotebookLMError as e:
        print(f"FAIL ({e})")
        return False
    print(f"OK (ID: {notebook_id})")

    # sources/ 内のファイルをアップロード
    sources_dir = backup_dir / "sources"
    if sources_dir.exists():
        files = collect_files([str(sources_dir)])
        if files:
            print(f"\n  [Sources] {len(files)} 件")
            ok, fail = upload_files(client, notebook_id, files)
            print(f"  Sources: {ok} OK, {fail} FAIL")

    # notes/ 内のファイルをテキストソースとして追加
    notes_dir = backup_dir / "notes"
    if notes_dir.exists():
        note_files = list(notes_dir.glob("*.md"))
        if note_files:
            print(f"\n  [Notes] {len(note_files)} 件")
            for nf in note_files:
                print(f"  {nf.name} ... ", end="", flush=True)
                try:
                    text = nf.read_text(encoding="utf-8")
                    source_id = client.add_source_text(notebook_id, f"[Note] {nf.stem}", text)
                    if source_id:
                        print("OK")
                    else:
                        print("FAIL")
                except NotebookLMError as e:
                    print(f"FAIL ({e})")

    print(f"\n  復元完了! → Notebook ID: {notebook_id}")
    print(f"  https://notebooklm.google.com/notebook/{notebook_id}")
    return True


def print_supported_types():
    """対応ファイル形式を表示"""
    print("\n対応ファイル形式:")
    categories = {
        "ドキュメント": [".pdf", ".txt", ".md", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"],
        "データ": [".csv", ".tsv", ".json", ".xml"],
        "ウェブ": [".html", ".htm"],
        "音声": [".mp3", ".wav", ".m4a", ".ogg", ".flac"],
        "動画": [".mp4", ".mov", ".avi", ".mkv", ".webm"],
        "画像": [".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"],
    }
    for cat, exts in categories.items():
        print(f"  {cat}: {', '.join(exts)}")


def main():
    parser = argparse.ArgumentParser(
        prog="nlm-upload",
        description="NotebookLM 一括アップロードツール",
    )
    parser.add_argument("title", nargs="?", help="新規ノートブックのタイトル")
    parser.add_argument("files", nargs="*", help="アップロードするファイルまたはフォルダ")
    parser.add_argument("--to", metavar="NOTEBOOK_ID", help="既存ノートブックに追加")
    parser.add_argument("--url", action="append", default=[], help="追加するURL（複数指定可）")
    parser.add_argument("--restore", metavar="BACKUP_DIR", help="バックアップから復元")
    parser.add_argument("--types", action="store_true", help="対応ファイル形式を表示")
    parser.add_argument("--cookies", type=str, default=None, help="クッキーファイルのパス")
    args = parser.parse_args()

    # 対応形式の表示
    if args.types:
        print_supported_types()
        return

    try:
        client = NotebookLMClient(cookies_path=args.cookies)
    except AuthenticationError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    # バックアップからの復元
    if args.restore:
        backup_dir = Path(args.restore)
        if not backup_dir.is_dir():
            print(f"[ERROR] ディレクトリが見つかりません: {args.restore}", file=sys.stderr)
            sys.exit(1)
        restore_backup(client, backup_dir)
        return

    # 既存ノートブックへの追加
    if args.to:
        notebook_id = args.to
        print(f"\n=== 既存ノートブックに追加 (ID: {notebook_id}) ===")
    # 新規ノートブック作成
    elif args.title:
        print(f"\n=== 新規ノートブック: {args.title} ===")
        print(f"  作成中 ... ", end="", flush=True)
        try:
            notebook_id = client.create_notebook(args.title)
        except NotebookLMError as e:
            print(f"FAIL ({e})")
            sys.exit(1)
        print(f"OK (ID: {notebook_id})")
    else:
        parser.print_help()
        print("\n例:")
        print('  nlm-upload "My Research" paper.pdf notes.md')
        print('  nlm-upload "Web Collection" --url https://example.com')
        print('  nlm-upload --to <notebook-id> new_file.pdf')
        print('  nlm-upload --restore ./downloads/My_Notebook/')
        return

    # ファイルのアップロード
    if args.files:
        files = collect_files(args.files)
        if files:
            print(f"\n  [Files] {len(files)} 件")
            ok, fail = upload_files(client, notebook_id, files)
            print(f"\n  結果: {ok} OK, {fail} FAIL")

    # URLの追加
    if args.url:
        print(f"\n  [URLs] {len(args.url)} 件")
        for url in args.url:
            print(f"  {url} ... ", end="", flush=True)
            try:
                source_id = client.add_source_url(notebook_id, url)
                if source_id:
                    print("OK")
                else:
                    print("FAIL")
            except NotebookLMError as e:
                print(f"FAIL ({e})")

    if not args.files and not args.url:
        print("\n  アップロードするファイルまたはURLを指定してください。")
        return

    print(f"\n  完了! → Notebook ID: {notebook_id}")
    print(f"  https://notebooklm.google.com/notebook/{notebook_id}")


if __name__ == "__main__":
    main()
