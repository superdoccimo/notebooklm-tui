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

from notebooklm_client import BASE_URL, NotebookLMClient, NotebookLMError, AuthenticationError

# Gemini Notebookがローカルファイルとして扱う形式。
# .txt / .md / .csv も pasted text へ変換せず、原本ファイルとして送る。
DOCUMENT_UPLOAD_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".epub",
    ".txt", ".md", ".markdown", ".csv",
}
IMAGE_UPLOAD_EXTENSIONS = {
    ".avif", ".bmp", ".gif", ".heic", ".heif", ".ico", ".jp2", ".jpe",
    ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp",
}
AUDIO_UPLOAD_EXTENSIONS = {
    ".3g2", ".3gp", ".aac", ".aif", ".aifc", ".aiff", ".amr", ".au",
    ".avi", ".cda", ".m4a", ".mid", ".mp3", ".mp4", ".mpeg", ".ogg",
    ".opus", ".ra", ".ram", ".snd", ".wav", ".wma",
}

# これらはファイルuploadせず、明示的な貼り付けテキストsourceとして追加する。
PASTED_TEXT_EXTENSIONS = {".tsv", ".json", ".xml", ".html", ".htm"}

# sidecarのない旧backupでは、.txt/.md/.csvが原本だったか抽出テキストだったか
# 判別できない。従来どおりtext sourceとして戻し、DEGRADED扱いにする。
LEGACY_TEXT_RESTORE_EXTENSIONS = PASTED_TEXT_EXTENSIONS | {".txt", ".md", ".csv"}

# 過去にbest-effortとして通していたが、現在の公開サポート範囲に含めない。
# サーバーへ投げて曖昧な失敗にせず、CLIで明確に拒否する。
UNVERIFIED_UPLOAD_EXTENSIONS = {
    ".doc", ".ppt", ".xls", ".xlsx", ".flac", ".mov", ".mkv", ".webm",
}

UPLOAD_FILE_TYPES = (
    DOCUMENT_UPLOAD_EXTENSIONS
    | IMAGE_UPLOAD_EXTENSIONS
    | AUDIO_UPLOAD_EXTENSIONS
)


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
            if ext in UPLOAD_FILE_TYPES:
                source_id = client.upload_file(notebook_id, f)
                if source_id:
                    print("OK (native file)")
                    ok += 1
                else:
                    print("FAIL")
                    fail += 1
            elif ext in PASTED_TEXT_EXTENSIONS:
                try:
                    text = f.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    print(f"SKIP (text fallback is not UTF-8: {ext})")
                    fail += 1
                    continue
                source_id = client.add_source_text(notebook_id, f.name, text)
                if source_id:
                    print("OK (pasted text)")
                    ok += 1
                else:
                    print("FAIL")
                    fail += 1
            elif ext in UNVERIFIED_UPLOAD_EXTENSIONS:
                print(
                    f"SKIP (not in the current documented upload set: {ext}; "
                    "convert to a supported format first)"
                )
                fail += 1
            else:
                print(f"SKIP (unsupported: {ext})")
                fail += 1
        except NotebookLMError as e:
            print(f"FAIL ({e})")
            fail += 1

    return ok, fail


def _read_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"JSON object expected: {path}")
    return data


def _metadata_rows(directory: Path) -> list[dict]:
    meta_dir = directory / "_metadata"
    if not meta_dir.is_dir():
        return []
    rows = []
    for path in sorted(meta_dir.glob("*.json")):
        try:
            row = _read_json(path)
            row["_metadata_file"] = str(path)
            rows.append(row)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            rows.append({
                "_metadata_file": str(path),
                "_metadata_error": f"{type(exc).__name__}: {exc}",
            })
    return rows


def _wait_restored_source(
    client: NotebookLMClient,
    notebook_id: str,
    source_id: str | None,
    wait_timeout: float,
) -> bool:
    if not source_id:
        return False
    client.wait_for_source_ready(
        notebook_id,
        source_id,
        timeout=wait_timeout,
    )
    return True


def _restore_one_source_v2(
    client: NotebookLMClient,
    notebook_id: str,
    sources_dir: Path,
    row: dict,
    wait_timeout: float,
) -> dict:
    """Restore one schema-v2 source without silently changing its semantic kind."""
    title = row.get("title") or "Untitled"
    source_type = row.get("type") or "unknown"
    mode = row.get("backup_mode")
    url = row.get("url")
    files = [
        sources_dir / rel
        for rel in row.get("files", [])
        if isinstance(rel, str)
    ]
    files = [path for path in files if path.is_file()]

    result = {
        "title": title,
        "type": source_type,
        "backup_mode": mode,
        "status": "failed",
        "restored_as": None,
        "source_id": None,
    }

    try:
        if source_type in {"web_page", "youtube"} and isinstance(url, str) and url.startswith("http"):
            source_id = client.add_source_url(notebook_id, url)
            result["source_id"] = source_id
            result["restored_as"] = "url"
            _wait_restored_source(client, notebook_id, source_id, wait_timeout)
            result["status"] = "restored"
            return result

        if mode == "original" and files:
            source_id = client.upload_file(notebook_id, files[0])
            result["source_id"] = source_id
            result["restored_as"] = "original_file"
            _wait_restored_source(client, notebook_id, source_id, wait_timeout)
            result["status"] = "restored"
            return result

        if source_type == "image" and mode == "rendered-image" and len(files) == 1:
            source_id = client.upload_file(notebook_id, files[0])
            result["source_id"] = source_id
            result["restored_as"] = "rendered_image"
            _wait_restored_source(client, notebook_id, source_id, wait_timeout)
            result["status"] = "degraded"
            result["warning"] = "Original image binary was unavailable; restored the rendered backup image."
            return result

        if mode == "rendered-pages":
            result["status"] = "preserved_only"
            result["reason"] = (
                "Original PDF binary was unavailable. Rendered pages remain local; "
                "they are not uploaded as separate image sources."
            )
            return result

        if mode == "text" and files:
            content = files[0].read_text(encoding="utf-8")
            source_id = client.add_source_text(notebook_id, title, content)
            result["source_id"] = source_id
            result["restored_as"] = "text"
            _wait_restored_source(client, notebook_id, source_id, wait_timeout)
            if source_type in {"pasted_text", "markdown"}:
                result["status"] = "restored"
            else:
                result["status"] = "degraded"
                result["warning"] = (
                    f"Original {source_type} representation was unavailable; "
                    "restored preserved extracted content as a text source."
                )
            return result

        result["status"] = "preserved_only"
        result["reason"] = "No semantics-preserving restore representation is available."
        return result
    except (NotebookLMError, OSError, UnicodeError) as exc:
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result


def _restore_sources_v2(
    client: NotebookLMClient,
    notebook_id: str,
    sources_dir: Path,
    wait_timeout: float,
) -> list[dict]:
    rows = _metadata_rows(sources_dir)
    results = []
    for row in rows:
        if row.get("_metadata_error"):
            results.append({
                "status": "failed",
                "type": "unknown",
                "title": Path(row["_metadata_file"]).name,
                "error": row["_metadata_error"],
            })
            continue
        result = _restore_one_source_v2(
            client,
            notebook_id,
            sources_dir,
            row,
            wait_timeout,
        )
        results.append(result)
        marker = {
            "restored": "OK",
            "degraded": "DEGRADED",
            "preserved_only": "PRESERVED ONLY",
            "failed": "FAIL",
        }.get(result["status"], result["status"].upper())
        print(f"  [{marker}] {result.get('title')} ({result.get('type')})")
        if result.get("warning"):
            print(f"    {result['warning']}")
        if result.get("reason"):
            print(f"    {result['reason']}")
        if result.get("error"):
            print(f"    {result['error']}")
    return results


def _restore_legacy_sources(
    client: NotebookLMClient,
    notebook_id: str,
    sources_dir: Path,
    wait_timeout: float,
) -> list[dict]:
    """Conservative old-backup fallback: never recurse into rendered-PDF page folders."""
    results = []
    for path in sorted(p for p in sources_dir.iterdir() if p.is_file() and not p.name.startswith(".")):
        print(f"  [legacy] {path.name} ... ", end="", flush=True)
        try:
            ext = path.suffix.lower()
            if ext in LEGACY_TEXT_RESTORE_EXTENSIONS:
                content = path.read_text(encoding="utf-8")
                source_id = client.add_source_text(notebook_id, path.name, content)
                restored_as = "text"
            else:
                source_id = client.upload_file(notebook_id, path)
                restored_as = "file"
            _wait_restored_source(client, notebook_id, source_id, wait_timeout)
            print("OK")
            results.append({
                "title": path.name,
                "status": "degraded",
                "restored_as": restored_as,
                "warning": "Legacy backup has no source sidecar; original source semantics cannot be proven.",
            })
        except (NotebookLMError, OSError, UnicodeError) as exc:
            print(f"FAIL ({exc})")
            results.append({
                "title": path.name,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            })

    nested = [
        p for p in sources_dir.iterdir()
        if p.is_dir() and p.name != "_metadata"
    ]
    for directory in nested:
        results.append({
            "title": directory.name,
            "status": "preserved_only",
            "reason": (
                "Legacy nested source directory was not uploaded recursively. "
                "This prevents rendered PDF pages from becoming separate image sources."
            ),
        })
        print(f"  [PRESERVED ONLY] {directory.name}/ (legacy nested source directory)")
    return results


def _restore_notes(
    client: NotebookLMClient,
    notebook_id: str,
    notes_dir: Path,
) -> list[dict]:
    rows = _metadata_rows(notes_dir)
    if not rows:
        rows = [
            {"title": path.stem, "file": path.name}
            for path in sorted(notes_dir.glob("*.md"))
        ]

    results = []
    for row in rows:
        title = row.get("title") or "Untitled"
        rel = row.get("file")
        if not isinstance(rel, str):
            results.append({"title": title, "status": "failed", "error": "Missing note file metadata"})
            continue
        path = notes_dir / rel
        try:
            content = path.read_text(encoding="utf-8")
            note_id = client.create_note(notebook_id, title, content)
            results.append({
                "title": title,
                "status": "restored",
                "note_id": note_id,
            })
            print(f"  [OK] {title}")
        except (NotebookLMError, OSError, UnicodeError) as exc:
            results.append({
                "title": title,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"  [FAIL] {title}: {exc}")
    return results


def _restore_mindmaps(
    client: NotebookLMClient,
    notebook_id: str,
    mindmaps_dir: Path,
) -> list[dict]:
    rows = _metadata_rows(mindmaps_dir)
    if not rows:
        rows = [
            {"title": path.stem, "json_file": path.name}
            for path in sorted(mindmaps_dir.glob("*.json"))
            if path.parent.name != "_metadata"
        ]

    results = []
    for row in rows:
        title = row.get("title") or "Untitled"
        rel = row.get("json_file")
        if not isinstance(rel, str):
            results.append({"title": title, "status": "failed", "error": "Missing mind-map JSON metadata"})
            continue
        path = mindmaps_dir / rel
        try:
            raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict) or not ("children" in parsed or "nodes" in parsed):
                raise ValueError("Not a recognized mind-map tree")
            note_id = client.create_note(notebook_id, title, raw)
            results.append({
                "title": title,
                "status": "restored",
                "note_id": note_id,
            })
            print(f"  [OK] {title}")
        except (NotebookLMError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            results.append({
                "title": title,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"  [FAIL] {title}: {exc}")
    return results


def _artifact_preservation_summary(backup_dir: Path) -> dict:
    art_dir = backup_dir / "artifacts"
    if not art_dir.is_dir():
        return {"preserved": False, "file_count": 0}
    files = [
        path for path in art_dir.rglob("*")
        if path.is_file()
    ]
    return {
        "preserved": bool(files),
        "file_count": len(files),
        "recreated": 0,
        "reason": (
            "Studio artifacts are backed up locally but are not recreated from local files. "
            "Re-generation would create new AI output rather than restore the original artifact."
        ),
    }


def build_restore_plan(backup_dir: Path) -> dict:
    """Inspect a backup without authentication or remote writes."""
    meta_path = backup_dir / "metadata.json"
    if not meta_path.is_file():
        raise ValueError(f"metadata.json not found: {backup_dir}")
    meta = _read_json(meta_path)
    schema_version = int(meta.get("backup_schema_version") or 1)
    plan = {
        "mode": "dry-run",
        "backup_schema_version": schema_version,
        "source_notebook_id": meta.get("id"),
        "title": meta.get("title", backup_dir.name),
        "sources": [],
        "notes": [],
        "mindmaps": [],
        "artifacts": _artifact_preservation_summary(backup_dir),
    }

    sources_dir = backup_dir / "sources"
    if sources_dir.is_dir():
        if schema_version >= 2 and (sources_dir / "_metadata").is_dir():
            for row in _metadata_rows(sources_dir):
                if row.get("_metadata_error"):
                    plan["sources"].append({
                        "title": Path(row["_metadata_file"]).name,
                        "type": "unknown",
                        "status": "failed",
                        "action": "none",
                        "reason": row["_metadata_error"],
                    })
                    continue
                title = row.get("title") or "Untitled"
                source_type = row.get("type") or "unknown"
                mode = row.get("backup_mode")
                url = row.get("url")
                files = [
                    sources_dir / rel
                    for rel in row.get("files", [])
                    if isinstance(rel, str)
                ]
                files = [path for path in files if path.is_file()]
                item = {
                    "title": title,
                    "type": source_type,
                    "backup_mode": mode,
                    "status": "preserved_only",
                    "action": "none",
                }
                if source_type in {"web_page", "youtube"} and isinstance(url, str) and url.startswith("http"):
                    item.update(status="restored", action="readd_url")
                elif mode == "original" and files:
                    item.update(status="restored", action="upload_original_file", file=str(files[0]))
                elif source_type == "image" and mode == "rendered-image" and len(files) == 1:
                    item.update(
                        status="degraded",
                        action="upload_rendered_image",
                        file=str(files[0]),
                        reason="Original image binary is unavailable.",
                    )
                elif mode == "rendered-pages":
                    item.update(
                        status="preserved_only",
                        action="keep_local",
                        reason="Original PDF is unavailable; page images will not be uploaded separately.",
                    )
                elif mode == "text" and files:
                    exact = source_type in {"pasted_text", "markdown"}
                    item.update(
                        status="restored" if exact else "degraded",
                        action="restore_as_text",
                        file=str(files[0]),
                    )
                    if not exact:
                        item["reason"] = f"Original {source_type} representation is unavailable."
                else:
                    item["reason"] = "No semantics-preserving restore representation is available."
                plan["sources"].append(item)
        else:
            for path in sorted(p for p in sources_dir.iterdir() if p.is_file() and not p.name.startswith(".")):
                plan["sources"].append({
                    "title": path.name,
                    "type": "legacy_unknown",
                    "status": "degraded",
                    "action": "restore_legacy_top_level_file",
                    "file": str(path),
                    "reason": "Legacy backup has no source sidecar; original source semantics cannot be proven.",
                })
            for directory in sorted(
                p for p in sources_dir.iterdir()
                if p.is_dir() and p.name != "_metadata"
            ):
                plan["sources"].append({
                    "title": directory.name,
                    "type": "legacy_nested_directory",
                    "status": "preserved_only",
                    "action": "keep_local",
                    "reason": "Nested legacy directories are not recursively uploaded.",
                })

    notes_dir = backup_dir / "notes"
    if notes_dir.is_dir():
        rows = _metadata_rows(notes_dir) or [
            {"title": path.stem, "file": path.name}
            for path in sorted(notes_dir.glob("*.md"))
        ]
        for row in rows:
            title = row.get("title") or "Untitled"
            rel = row.get("file")
            path = notes_dir / rel if isinstance(rel, str) else None
            exists = bool(path and path.is_file())
            plan["notes"].append({
                "title": title,
                "status": "restored" if exists else "failed",
                "action": "create_native_note" if exists else "none",
                **({"file": str(path)} if path else {}),
                **({} if exists else {"reason": "Note backup file is missing."}),
            })

    mindmaps_dir = backup_dir / "mindmaps"
    if mindmaps_dir.is_dir():
        rows = _metadata_rows(mindmaps_dir) or [
            {"title": path.stem, "json_file": path.name}
            for path in sorted(mindmaps_dir.glob("*.json"))
        ]
        for row in rows:
            title = row.get("title") or "Untitled"
            rel = row.get("json_file")
            path = mindmaps_dir / rel if isinstance(rel, str) else None
            valid = False
            reason = "Mind-map JSON file is missing."
            if path and path.is_file():
                try:
                    parsed = json.loads(path.read_text(encoding="utf-8"))
                    valid = isinstance(parsed, dict) and ("children" in parsed or "nodes" in parsed)
                    if not valid:
                        reason = "JSON is not a recognized mind-map tree."
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    reason = f"{type(exc).__name__}: {exc}"
            plan["mindmaps"].append({
                "title": title,
                "status": "restored" if valid else "failed",
                "action": "create_json_backed_note" if valid else "none",
                **({"file": str(path)} if path else {}),
                **({} if valid else {"reason": reason}),
            })

    rows = plan["sources"] + plan["notes"] + plan["mindmaps"]
    restored = sum(row.get("status") == "restored" for row in rows)
    degraded = sum(row.get("status") in {"degraded", "preserved_only"} for row in rows)
    failed = sum(row.get("status") == "failed" for row in rows)
    artifact_limit = bool(plan["artifacts"].get("preserved"))
    if failed:
        state = "PARTIAL"
    elif degraded or artifact_limit:
        state = "COMPLETE WITH LIMITATIONS"
    else:
        state = "COMPLETE"
    plan["summary"] = {
        "would_restore": restored,
        "would_degrade_or_preserve_only": degraded,
        "would_fail_preflight": failed,
        "studio_artifacts_preserved_only": artifact_limit,
        "expected_state": state,
    }
    return plan


def print_restore_plan(plan: dict) -> None:
    print(f"\n=== RESTORE DRY RUN: {plan.get('title')} ===")
    print(f"Backup schema: v{plan.get('backup_schema_version')}")
    for section in ("sources", "notes", "mindmaps"):
        rows = plan.get(section, [])
        if not rows:
            continue
        print(f"\n  [{section.capitalize()}]")
        for row in rows:
            status = row.get("status", "unknown").upper()
            action = row.get("action", "none")
            print(f"  [{status}] {row.get('title')} -> {action}")
            if row.get("reason"):
                print(f"    {row['reason']}")
    artifacts = plan.get("artifacts", {})
    if artifacts.get("preserved"):
        print(
            f"\n  [Studio Artifacts] PRESERVED ONLY "
            f"({artifacts.get('file_count', 0)} local files; 0 recreated)"
        )
    summary = plan["summary"]
    print(f"\nExpected: {summary['expected_state']}")
    print(
        f"Would restore: {summary['would_restore']}, "
        f"Degraded/Preserved: {summary['would_degrade_or_preserve_only']}, "
        f"Preflight failures: {summary['would_fail_preflight']}"
    )


def restore_backup(
    client: NotebookLMClient,
    backup_dir: Path,
    *,
    wait_timeout: float = 120.0,
) -> bool:
    """Restore the parts of a backup that have semantics-preserving server write paths."""
    meta_path = backup_dir / "metadata.json"
    if not meta_path.exists():
        print(f"[ERROR] metadata.json が見つかりません: {backup_dir}", file=sys.stderr)
        return False

    try:
        meta = _read_json(meta_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[ERROR] metadata.json を読めません: {exc}", file=sys.stderr)
        return False

    title = meta.get("title", backup_dir.name)
    schema_version = int(meta.get("backup_schema_version") or 1)
    print(f"\n=== 復元: {title} (backup schema v{schema_version}) ===")

    print(f"  ノートブック作成中: {title} ... ", end="", flush=True)
    try:
        notebook_id = client.create_notebook(title)
    except NotebookLMError as exc:
        print(f"FAIL ({exc})")
        return False
    print(f"OK (ID: {notebook_id})")

    report = {
        "backup_schema_version": schema_version,
        "source_notebook_id": meta.get("id"),
        "restored_notebook_id": notebook_id,
        "title": title,
        "sources": [],
        "notes": [],
        "mindmaps": [],
        "artifacts": _artifact_preservation_summary(backup_dir),
    }

    sources_dir = backup_dir / "sources"
    if sources_dir.is_dir():
        print("\n  [Sources]")
        if schema_version >= 2 and (sources_dir / "_metadata").is_dir():
            report["sources"] = _restore_sources_v2(
                client,
                notebook_id,
                sources_dir,
                wait_timeout,
            )
        else:
            report["sources"] = _restore_legacy_sources(
                client,
                notebook_id,
                sources_dir,
                wait_timeout,
            )

    notes_dir = backup_dir / "notes"
    if notes_dir.is_dir():
        print("\n  [Notes]")
        report["notes"] = _restore_notes(client, notebook_id, notes_dir)

    mindmaps_dir = backup_dir / "mindmaps"
    if mindmaps_dir.is_dir():
        print("\n  [Mindmaps]")
        report["mindmaps"] = _restore_mindmaps(client, notebook_id, mindmaps_dir)

    if report["artifacts"].get("preserved"):
        print(
            "\n  [Studio Artifacts] PRESERVED ONLY "
            f"({report['artifacts']['file_count']} local files; 0 recreated)"
        )
        print(f"    {report['artifacts']['reason']}")

    all_rows = report["sources"] + report["notes"] + report["mindmaps"]
    failed = [row for row in all_rows if row.get("status") == "failed"]
    degraded = [
        row for row in all_rows
        if row.get("status") in {"degraded", "preserved_only"}
    ]
    restored = [row for row in all_rows if row.get("status") == "restored"]
    report["summary"] = {
        "restored": len(restored),
        "degraded_or_preserved_only": len(degraded),
        "failed": len(failed),
    }

    report_path = backup_dir / f"restore-report-{notebook_id}.json"
    try:
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"  [WARN] restore reportを書けませんでした: {exc}")

    has_limitations = bool(degraded) or bool(report["artifacts"].get("preserved"))
    if failed:
        state = "PARTIAL"
    elif has_limitations:
        state = "COMPLETE WITH LIMITATIONS"
    else:
        state = "COMPLETE"
    print(f"\n  復元 {state} → Notebook ID: {notebook_id}")
    print(f"  Restored: {len(restored)}, Degraded/Preserved: {len(degraded)}, Failed: {len(failed)}")
    print(f"  {BASE_URL}/notebook/{notebook_id}")
    print(f"  Report: {report_path}")
    return not failed


def print_supported_types():
    """対応ファイル形式を表示"""
    print("\n対応ファイル形式:")
    categories = {
        "ネイティブ文書/テキスト/データ": sorted(DOCUMENT_UPLOAD_EXTENSIONS),
        "音声/文字起こし": sorted(AUDIO_UPLOAD_EXTENSIONS),
        "画像": sorted(IMAGE_UPLOAD_EXTENSIONS),
        "貼り付けテキストとして追加": sorted(PASTED_TEXT_EXTENSIONS),
        "標準では拒否（要変換）": sorted(UNVERIFIED_UPLOAD_EXTENSIONS),
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
    parser.add_argument("--restore", metavar="BACKUP_DIR", help="バックアップから意味を保てる範囲を復元")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="--restoreの実行計画だけ表示。認証もNotebook作成も行わない",
    )
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=120.0,
        help="source復元後にreadyを待つ最大秒数 (default: 120)",
    )
    parser.add_argument("--types", action="store_true", help="対応ファイル形式を表示")
    parser.add_argument("--cookies", type=str, default=None, help="クッキーファイルのパス")
    args = parser.parse_args()

    # 対応形式の表示
    if args.types:
        print_supported_types()
        return

    if args.dry_run and not args.restore:
        parser.error("--dry-run requires --restore")

    # Dry-run is local-only by design: no auth, no remote reads/writes.
    if args.restore and args.dry_run:
        backup_dir = Path(args.restore)
        if not backup_dir.is_dir():
            print(f"[ERROR] ディレクトリが見つかりません: {args.restore}", file=sys.stderr)
            sys.exit(1)
        try:
            plan = build_restore_plan(backup_dir)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[ERROR] restore planを作成できません: {exc}", file=sys.stderr)
            sys.exit(1)
        plan_path = backup_dir / "restore-plan.json"
        try:
            plan_path.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"[ERROR] restore-plan.jsonを書けません: {exc}", file=sys.stderr)
            sys.exit(1)
        print_restore_plan(plan)
        print(f"Plan: {plan_path}")
        sys.exit(2 if plan["summary"]["would_fail_preflight"] else 0)

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
        if not restore_backup(client, backup_dir, wait_timeout=args.wait_timeout):
            sys.exit(1)
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
    print(f"  {BASE_URL}/notebook/{notebook_id}")


if __name__ == "__main__":
    main()
