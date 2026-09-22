"""Live release canary for notebooklm-tui.

Read-only by default when pointed at an existing notebook.  The optional
--write-smoke mode creates one disposable notebook, verifies source writes and
backup, then deletes only that notebook in a finally block.

This tool intentionally does not generate Studio artifacts.  For the release
profile, prepare a notebook in Gemini Notebook with one example of each target
artifact, then run the canary against that notebook.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from nlm_backup import (
    sanitize_filename,
    save_artifact,
    save_mindmaps,
    save_notes,
    save_source,
)
from notebooklm_client import (
    BASE_URL,
    UPLOAD_BASE_URL,
    AuthenticationError,
    NotebookLMClient,
    NotebookLMError,
)

RELEASE_REQUIRED_ARTIFACTS = frozenset(
    {
        "audio_overview",
        "video_overview",
        "slide_deck",
        "report",
        "data_table",
        "flashcards",
        "quiz",
        "interactive_mind_map",
        "infographic",
        "interactive_learning_overview",
    }
)


def _now_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _artifact_labels(artifact: dict) -> set[str]:
    """Return user-facing capability labels represented by one artifact row."""
    labels: set[str] = set()
    art_type = artifact.get("type")
    variant = artifact.get("variant")
    if isinstance(art_type, str) and art_type:
        labels.add(art_type)
    if isinstance(variant, str) and variant:
        labels.add(variant)

    # Current Interactive Learning Overview observations ride the report family.
    # We only apply this label when the row actually exposes interactive HTML or
    # structured content, rather than guessing from the title.
    if art_type == "report" and (
        artifact.get("app_html")
        or artifact.get("structured_content") is not None
    ):
        labels.add("interactive_learning_overview")
    return labels


def _required_for_profile(profile: str) -> set[str]:
    if profile == "inventory":
        return set()
    if profile == "release":
        return set(RELEASE_REQUIRED_ARTIFACTS)
    raise ValueError(f"unknown profile: {profile}")


def _evaluate_artifact_coverage(
    artifacts: Iterable[dict],
    export_results: Iterable[dict],
    required: set[str],
) -> dict:
    """Summarize which completed, successfully exported artifact families exist."""
    successful_labels: set[str] = set()
    failed_exports: list[dict] = []
    rows = list(artifacts)
    results = list(export_results)

    for artifact, result in zip(rows, results):
        labels = sorted(_artifact_labels(artifact))
        if artifact.get("status") == "completed" and result.get("saved"):
            successful_labels.update(labels)
        elif artifact.get("status") == "completed":
            failed_exports.append(
                {
                    "id": artifact.get("id"),
                    "title": artifact.get("title"),
                    "labels": labels,
                }
            )

    missing = sorted(required - successful_labels)
    return {
        "required": sorted(required),
        "successful_labels": sorted(successful_labels),
        "missing_required": missing,
        "failed_completed_exports": failed_exports,
        "passed": not missing and not failed_exports,
    }


def _write_report(report: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "canary-report.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def run_read_only_canary(
    client: NotebookLMClient,
    notebook_id: str,
    output_root: Path,
    profile: str,
) -> tuple[dict, Path]:
    notebooks = client.list_notebooks()
    notebook = next((nb for nb in notebooks if nb.get("id") == notebook_id), None)
    if notebook is None:
        raise NotebookLMError(f"Notebook not found or not accessible: {notebook_id}")

    safe_title = sanitize_filename(notebook.get("title", "") or notebook_id)
    output_dir = output_root / f"{safe_title}-canary-{_now_slug()}"
    output_dir.mkdir(parents=True, exist_ok=True)

    sources = client.list_sources(notebook_id)
    source_results: list[dict] = []
    for source in sources:
        try:
            result = save_source(client, source, output_dir)
            source_results.append(
                {
                    "id": source.get("id"),
                    "title": source.get("title"),
                    "type": source.get("type"),
                    "has_original_download": bool(source.get("download_url")),
                    "saved": bool(result.get("saved")),
                    "mode": result.get("mode"),
                    "files": [
                        str(Path(path).relative_to(output_dir))
                        for path in result.get("paths", [])
                    ],
                }
            )
        except (NotebookLMError, OSError) as exc:
            source_results.append(
                {
                    "id": source.get("id"),
                    "title": source.get("title"),
                    "type": source.get("type"),
                    "has_original_download": bool(source.get("download_url")),
                    "saved": False,
                    "mode": "exception",
                    "error": f"{type(exc).__name__}: {exc}",
                    "files": [],
                }
            )

    artifacts = client.list_artifacts(notebook_id)
    artifact_results: list[dict] = []
    artifact_report_rows: list[dict] = []
    for artifact in artifacts:
        try:
            result = save_artifact(client, artifact, output_dir)
            artifact_results.append(result)
            artifact_report_rows.append(
                {
                    "id": artifact.get("id"),
                    "title": artifact.get("title"),
                    "type": artifact.get("type"),
                    "type_code": artifact.get("type_code"),
                    "variant": artifact.get("variant"),
                    "status": artifact.get("status"),
                    "status_code": artifact.get("status_code"),
                    "labels": sorted(_artifact_labels(artifact)),
                    "saved": bool(result.get("saved")),
                    "dest": (
                        str(Path(result["dest"]).relative_to(output_dir))
                        if result.get("dest")
                        else None
                    ),
                    "raw_snapshot": str(
                        Path(result["raw_snapshot"]).relative_to(output_dir)
                    ),
                    "pptx_saved": result.get("pptx_saved"),
                    "pages_count": result.get("pages_count"),
                }
            )
        except (NotebookLMError, OSError) as exc:
            artifact_results.append({"saved": False})
            artifact_report_rows.append(
                {
                    "id": artifact.get("id"),
                    "title": artifact.get("title"),
                    "type": artifact.get("type"),
                    "variant": artifact.get("variant"),
                    "status": artifact.get("status"),
                    "labels": sorted(_artifact_labels(artifact)),
                    "saved": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    notes = client.list_notes(notebook_id)
    notes_saved = save_notes(notes, output_dir) if notes else 0

    mindmaps = client.list_mindmaps(notebook_id)
    mindmaps_saved = save_mindmaps(mindmaps, output_dir) if mindmaps else 0

    required = _required_for_profile(profile)
    coverage = _evaluate_artifact_coverage(artifacts, artifact_results, required)
    source_failures = [row for row in source_results if not row.get("saved")]

    report = {
        "mode": "read-only",
        "profile": profile,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "rpc_base_url": BASE_URL,
        "upload_base_url": UPLOAD_BASE_URL,
        "notebook": {
            "id": notebook_id,
            "title": notebook.get("title"),
            "source_count": len(sources),
            "artifact_count": len(artifacts),
            "note_count": len(notes),
            "mindmap_count": len(mindmaps),
        },
        "sources": source_results,
        "artifacts": artifact_report_rows,
        "notes": {"saved": notes_saved, "total": len(notes)},
        "mindmaps": {"saved": mindmaps_saved, "total": len(mindmaps)},
        "artifact_coverage": coverage,
        "source_failures": source_failures,
    }
    report["passed"] = (
        coverage["passed"]
        and not source_failures
        and notes_saved == len(notes)
        and mindmaps_saved == len(mindmaps)
    )
    report_path = _write_report(report, output_dir)
    return report, report_path


def run_write_smoke(
    client: NotebookLMClient,
    output_root: Path,
    smoke_url: str | None,
) -> tuple[dict, Path]:
    """Create one disposable notebook, verify source writes/backup, then delete it."""
    output_dir = output_root / f"write-smoke-{_now_slug()}"
    output_dir.mkdir(parents=True, exist_ok=True)
    title = f"nlm-canary-{_now_slug()}"
    notebook_id: str | None = None
    delete_ok = False

    report: dict = {
        "mode": "write-smoke",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "rpc_base_url": BASE_URL,
        "upload_base_url": UPLOAD_BASE_URL,
        "title": title,
        "smoke_url": smoke_url,
        "steps": [],
        "passed": False,
    }

    try:
        notebook_id = client.create_notebook(title)
        report["notebook_id"] = notebook_id
        report["steps"].append({"create_notebook": True})

        text_source_id = client.add_source_text(
            notebook_id,
            "canary.txt",
            "notebooklm-tui live canary source\n",
        )
        report["text_source_id"] = text_source_id
        report["steps"].append({"add_text_source": bool(text_source_id)})

        url_source_id = None
        if smoke_url:
            url_source_id = client.add_source_url(notebook_id, smoke_url)
            report["url_source_id"] = url_source_id
            report["steps"].append({"add_url_source": bool(url_source_id)})

        sources = client.list_sources(notebook_id)
        expected_ids = {source_id for source_id in (text_source_id, url_source_id) if source_id}
        observed_ids = {src.get("id") for src in sources if src.get("id")}
        ids_present = expected_ids.issubset(observed_ids)
        report["steps"].append({"list_sources_readback": ids_present})

        saved_rows = []
        for source in sources:
            try:
                result = save_source(client, source, output_dir)
                saved_rows.append(
                    {
                        "id": source.get("id"),
                        "saved": bool(result.get("saved")),
                        "mode": result.get("mode"),
                    }
                )
            except (NotebookLMError, OSError) as exc:
                saved_rows.append(
                    {
                        "id": source.get("id"),
                        "saved": False,
                        "mode": "exception",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        report["source_backups"] = saved_rows
        backups_ok = bool(saved_rows) and all(row["saved"] for row in saved_rows)
        report["steps"].append({"backup_sources": backups_ok})
        report["passed_before_cleanup"] = (
            bool(notebook_id)
            and bool(text_source_id)
            and ids_present
            and backups_ok
            and (not smoke_url or bool(url_source_id))
        )
    finally:
        if notebook_id:
            try:
                delete_ok = client.delete_notebook(notebook_id)
            except NotebookLMError as exc:
                report["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        report["cleanup_deleted_disposable_notebook"] = delete_ok
        report["passed"] = bool(report.get("passed_before_cleanup")) and delete_ok
        report_path = _write_report(report, output_dir)

    return report, report_path


def _print_summary(report: dict, report_path: Path) -> None:
    state = "PASS" if report.get("passed") else "FAIL"
    print(f"Canary: {state}")
    print(f"Report: {report_path}")
    if report.get("mode") == "read-only":
        coverage = report.get("artifact_coverage", {})
        missing = coverage.get("missing_required", [])
        failures = coverage.get("failed_completed_exports", [])
        if missing:
            print("Missing required artifact coverage: " + ", ".join(missing))
        if failures:
            print(f"Completed artifact export failures: {len(failures)}")
        source_failures = report.get("source_failures", [])
        if source_failures:
            print(f"Source backup failures: {len(source_failures)}")
    else:
        if not report.get("cleanup_deleted_disposable_notebook"):
            print("WARNING: disposable notebook cleanup was not confirmed.")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="nlm-canary",
        description="Live Gemini Notebook backup compatibility canary",
    )
    parser.add_argument(
        "--notebook-id",
        help="Existing notebook to audit without remote mutations",
    )
    parser.add_argument(
        "--profile",
        choices=("inventory", "release"),
        default="release",
        help="release requires the full artifact compatibility set; inventory only records what exists",
    )
    parser.add_argument(
        "--write-smoke",
        action="store_true",
        help="Create/delete one disposable notebook and verify source write + backup",
    )
    parser.add_argument(
        "--smoke-url",
        default=None,
        help="Optional URL to add during --write-smoke",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.cwd() / "canary-output",
        help="Local canary output root",
    )
    parser.add_argument(
        "--cookies",
        default=None,
        help="Cookie JSON path (default: normal nlm-login profile)",
    )
    args = parser.parse_args()

    if args.write_smoke and args.notebook_id:
        parser.error("use either --write-smoke or --notebook-id, not both")
    if args.smoke_url and not args.write_smoke:
        parser.error("--smoke-url requires --write-smoke")
    if not args.write_smoke and not args.notebook_id:
        parser.error("--notebook-id is required unless --write-smoke is used")

    try:
        client = NotebookLMClient(cookies_path=args.cookies)
        if args.write_smoke:
            report, report_path = run_write_smoke(client, args.output, args.smoke_url)
        else:
            report, report_path = run_read_only_canary(
                client,
                args.notebook_id,
                args.output,
                args.profile,
            )
    except AuthenticationError as exc:
        print(f"[AUTH] {exc}", file=sys.stderr)
        return 3
    except (NotebookLMError, OSError) as exc:
        print(f"[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    _print_summary(report, report_path)
    return 0 if report.get("passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
