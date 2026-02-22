"""
nlm-tui-en: NotebookLM TUI tool (Windows / Linux fallback)

Main keys:
  Up/Down (j/k): Move
  Space        : Select/unselect
  Enter        : Open detail tree
  b            : Batch backup selected notebooks (current row if none selected)
  u            : Upload menu (new notebook / append to current)
  x            : Retry only failed items from last backup
  f            : Backup target filter (Sources/Artifacts/Notes)
  r            : Reload notebook list
  a            : Select all / clear all
  q            : Quit / back
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

try:
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None

try:
    import select
    import termios
    import tty
except ImportError:  # pragma: no cover
    select = None
    termios = None
    tty = None

from notebooklm_client import AuthenticationError, NotebookLMClient, NotebookLMError
from nlm_backup import ARTIFACT_EXTENSIONS, format_timestamp, sanitize_filename
from nlm_upload import TEXT_EXTENSIONS, UPLOAD_FILE_TYPES


@dataclass
class BackupSelection:
    sources: bool = True
    artifacts: bool = True
    notes: bool = True

    def label(self) -> str:
        return " ".join(
            [
                "S:on" if self.sources else "S:off",
                "A:on" if self.artifacts else "A:off",
                "N:on" if self.notes else "N:off",
            ]
        )

    def enabled_count(self) -> int:
        return int(self.sources) + int(self.artifacts) + int(self.notes)


def _bar(width: int, ratio: float) -> str:
    ratio = max(0.0, min(1.0, ratio))
    fill = int(width * ratio)
    return "[" + ("#" * fill) + ("-" * (width - fill)) + "]"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    i = 2
    while True:
        cand = path.with_name(f"{stem}_{i}{suffix}")
        if not cand.exists():
            return cand
        i += 1


def _append_log(log_path: Path, message: str):
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def _en_error_text(err: Exception) -> str:
    text = str(err).strip()
    text = re.sub(
        r"認証エラー \(HTTP (\d+)\)。nlm login を再実行してください。",
        r"Authentication error (HTTP \1). Please run nlm-login again.",
        text,
    )
    replacements = {
        "クッキーファイルが見つかりません:": "Cookie file not found:",
        "nlm login を実行するか、クッキーを手動でエクスポートしてください。": (
            "Run nlm-login or export cookies manually."
        ),
        "認証が期限切れです。nlm login を再実行してください。": (
            "Authentication expired. Please run nlm-login again."
        ),
        "CSRFトークンの取得に失敗しました。認証が切れている可能性があります。": (
            "Failed to retrieve CSRF token. Authentication may be expired."
        ),
        "ノートブック作成に失敗しました": "Failed to create notebook.",
        "ファイルが見つかりません:": "File not found:",
        "ファイル登録に失敗しました": "Failed to register file.",
        "アップロードURLの取得に失敗しました": "Failed to get upload URL.",
    }
    for ja, en in replacements.items():
        text = text.replace(ja, en)
    return text


def _toggle_selection_component(selection: BackupSelection, component: str) -> bool:
    current = getattr(selection, component)
    if current and selection.enabled_count() == 1:
        return False
    setattr(selection, component, not current)
    return True


def _failure_count(failed: dict) -> int:
    return len(failed.get("sources", [])) + len(failed.get("artifacts", [])) + len(failed.get("notes", []))


def _entry_failure_count(entry: dict) -> int:
    if entry.get("full_retry"):
        return 1
    return _failure_count(entry.get("failed", {}))


def _save_text_source(content: dict, out_dir: Path) -> Path:
    title = sanitize_filename(content.get("title", "untitled"))
    if not title.endswith(".md"):
        title = Path(title).stem + ".md"
    dest = _unique_path(out_dir / "sources" / title)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(content.get("content", ""))
    return dest


def _save_image_source(client: NotebookLMClient, content: dict, out_dir: Path) -> list[Path]:
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


def _save_pdf_source(client: NotebookLMClient, content: dict, out_dir: Path) -> list[Path]:
    title = sanitize_filename(content.get("title", "document"))
    stem = Path(title).stem
    pdf_dir = out_dir / "sources" / stem
    pdf_dir.mkdir(parents=True, exist_ok=True)

    raw = content.get("content", "")
    urls = re.findall(r'https://[^\s\])"\']+', raw)

    saved = []
    for i, url in enumerate(urls, 1):
        dest = _unique_path(pdf_dir / f"page{i}.png")
        if client.download_url(url, dest):
            saved.append(dest)
    return saved


def _build_detail_lines(client: NotebookLMClient, nb: dict) -> list[str]:
    sources = client.list_sources(nb["id"])
    artifacts = client.list_artifacts(nb["id"])
    notes = client.list_notes(nb["id"])

    lines = [f"Notebook: {nb['title']}", f"ID: {nb['id']}", ""]
    lines.append(f"Sources ({len(sources)})")
    for src in sources:
        lines.append(f"  - [{src.get('type', 'unknown')}] {src.get('title', 'Untitled')}")
    lines.append("")
    lines.append(f"Artifacts ({len(artifacts)})")
    for art in artifacts:
        lines.append(f"  - [{art.get('status', 'unknown')}] [{art.get('type', 'unknown')}] {art.get('title', 'Untitled')}")
    lines.append("")
    lines.append(f"Notes ({len(notes)})")
    for note in notes:
        lines.append(f"  - {note.get('title', 'Untitled')}")
    return lines


def _split_user_list(raw: str, separators: str = ";") -> list[str]:
    if not raw.strip():
        return []
    pattern = "[" + re.escape(separators) + "]"
    parts = re.split(pattern, raw)
    cleaned = []
    for p in parts:
        item = p.strip().strip('"').strip("'")
        if item:
            cleaned.append(item)
    return cleaned


def _collect_files_en(paths: list[str]) -> list[Path]:
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
            print(f"  [WARN] Not found: {p}", file=sys.stderr)
    return files


def _upload_to_notebook(
    client: NotebookLMClient,
    notebook_id: str,
    files: list[Path],
    urls: list[str],
    on_progress: Callable[[float, str], None],
    logger: Callable[[str], None] | None = None,
) -> dict:
    total = max(1, len(files) + len(urls))
    done = 0
    files_ok = 0
    files_fail = 0
    urls_ok = 0
    urls_fail = 0
    failed_files = []
    failed_urls = []

    def step(message: str):
        nonlocal done
        done += 1
        on_progress(done / total, message)

    for i, f in enumerate(files, 1):
        ext = f.suffix.lower()
        msg_name = f.name
        try:
            if ext in TEXT_EXTENSIONS:
                try:
                    text = f.read_text(encoding="utf-8")
                    source_id = client.add_source_text(notebook_id, f.name, text)
                except UnicodeDecodeError:
                    source_id = client.upload_file(notebook_id, f)
            elif ext in UPLOAD_FILE_TYPES:
                source_id = client.upload_file(notebook_id, f)
            else:
                source_id = None

            if source_id:
                files_ok += 1
                if logger:
                    logger(f"Upload file ok notebook={notebook_id} file={f}")
            else:
                files_fail += 1
                failed_files.append(str(f))
                if logger:
                    logger(f"Upload file fail notebook={notebook_id} file={f}")
        except (NotebookLMError, OSError):
            files_fail += 1
            failed_files.append(str(f))
            if logger:
                logger(f"Upload file exception notebook={notebook_id} file={f}")
        step(f"Files {i}/{len(files)}: {msg_name}")

    for i, url in enumerate(urls, 1):
        try:
            source_id = client.add_source_url(notebook_id, url)
            if source_id:
                urls_ok += 1
                if logger:
                    logger(f"Upload url ok notebook={notebook_id} url={url}")
            else:
                urls_fail += 1
                failed_urls.append(url)
                if logger:
                    logger(f"Upload url fail notebook={notebook_id} url={url}")
        except NotebookLMError:
            urls_fail += 1
            failed_urls.append(url)
            if logger:
                logger(f"Upload url exception notebook={notebook_id} url={url}")
        step(f"URLs {i}/{len(urls)}: {url}")

    return {
        "files_total": len(files),
        "files_ok": files_ok,
        "files_fail": files_fail,
        "urls_total": len(urls),
        "urls_ok": urls_ok,
        "urls_fail": urls_fail,
        "failed_files": failed_files,
        "failed_urls": failed_urls,
    }


def _backup_notebook(
    client: NotebookLMClient,
    notebook_id: str,
    out_base: Path,
    notebooks: list[dict],
    selection: BackupSelection,
    retry_plan: dict | None,
    on_progress: Callable[[float, str], None],
    logger: Callable[[str], None] | None = None,
) -> dict:
    title = notebook_id
    for nb in notebooks:
        if nb["id"] == notebook_id:
            title = nb["title"]
            break

    safe_title = sanitize_filename(title)
    out_dir = out_base / safe_title
    out_dir.mkdir(parents=True, exist_ok=True)

    meta = {"id": notebook_id, "title": title}
    for nb in notebooks:
        if nb["id"] == notebook_id:
            meta.update(nb)
            break
    with open(out_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    if selection.sources:
        sources = retry_plan.get("sources", []) if retry_plan is not None else client.list_sources(notebook_id)
    else:
        sources = []
    if selection.artifacts:
        artifacts = retry_plan.get("artifacts", []) if retry_plan is not None else [
            a for a in client.list_artifacts(notebook_id) if a.get("status") == "completed"
        ]
    else:
        artifacts = []
    if selection.notes:
        notes = retry_plan.get("notes", []) if retry_plan is not None else client.list_notes(notebook_id)
    else:
        notes = []

    total = max(1, 1 + len(sources) + len(artifacts) + len(notes))
    done = 0

    def step(message: str):
        nonlocal done
        done += 1
        on_progress(done / total, message)

    failed_sources = []
    failed_artifacts = []
    failed_notes = []
    step("Save metadata")

    src_ok = 0
    src_fail = 0
    for i, src in enumerate(sources, 1):
        src_id = src.get("id")
        src_type = src.get("type", "unknown")
        src_title = src.get("title", "untitled")
        if not src_id:
            src_fail += 1
            failed_sources.append({"id": src_id, "type": src_type, "title": src_title})
            step(f"Sources {i}/{len(sources)}: {src_title}")
            continue
        try:
            content = client.get_source_content(src_id)
            if src_type in ("text", "generated_text", "website", "document"):
                _save_text_source(content, out_dir)
            elif src_type == "image":
                _save_image_source(client, content, out_dir)
            elif src_type == "pdf":
                _save_pdf_source(client, content, out_dir)
            else:
                _save_text_source(content, out_dir)
            src_ok += 1
        except (NotebookLMError, OSError):
            src_fail += 1
            failed_sources.append({"id": src_id, "type": src_type, "title": src_title})
        step(f"Sources {i}/{len(sources)}: {src_title}")

    art_dir = out_dir / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    art_ok = 0
    art_fail = 0
    for i, art in enumerate(artifacts, 1):
        art_type = art.get("type", "unknown")
        art_title = art.get("title", "untitled")
        ext = ARTIFACT_EXTENSIONS.get(art_type, ".bin")
        dest = _unique_path(art_dir / f"{art_type}{ext}")
        if client.download_artifact(art, dest):
            art_ok += 1
        else:
            art_fail += 1
            failed_artifacts.append(art)
        # Download PPTX for slide decks
        if art.get("pptx_url"):
            pptx_dest = dest.with_suffix(".pptx")
            client.download_artifact_pptx(art, pptx_dest)
        # Download page images for slide deck artifacts.
        if art.get("page_images"):
            pages_dir = art_dir / dest.stem
            client.download_artifact_pages(art, pages_dir)
        step(f"Artifacts {i}/{len(artifacts)}: {art_title}")

    note_dir = out_dir / "notes"
    note_dir.mkdir(parents=True, exist_ok=True)
    note_ok = 0
    note_fail = 0
    for i, note in enumerate(notes, 1):
        note_title = sanitize_filename(note.get("title", "untitled"))
        content = note.get("content", "")
        if not note_title.endswith(".md"):
            note_title += ".md"
        dest = _unique_path(note_dir / note_title)
        try:
            with open(dest, "w", encoding="utf-8") as f:
                f.write(content)
            note_ok += 1
        except OSError:
            note_fail += 1
            failed_notes.append({"title": note.get("title", "untitled"), "content": content})
        step(f"Notes {i}/{len(notes)}: {note_title}")

    summary = {
        "sources_total": len(sources),
        "artifacts_total": len(artifacts),
        "notes_total": len(notes),
        "sources_ok": src_ok,
        "sources_fail": src_fail,
        "artifacts_ok": art_ok,
        "artifacts_fail": art_fail,
        "notes_ok": note_ok,
        "notes_fail": note_fail,
        "failed": {
            "sources": failed_sources,
            "artifacts": failed_artifacts,
            "notes": failed_notes,
        },
    }
    if logger:
        logger(
            f"Backup summary notebook={title} "
            f"S={src_ok}/{len(sources)} A={art_ok}/{len(artifacts)} N={note_ok}/{len(notes)} "
            f"failures={_failure_count(summary['failed'])}"
        )
    return summary


def _run_backup_batch(
    client: NotebookLMClient,
    out_base: Path,
    notebooks: list[dict],
    targets: list[dict],
    selection: BackupSelection,
    on_progress: Callable[[int, int, str, float, str, list[str]], None],
    logger: Callable[[str], None] | None = None,
) -> tuple[list[str], list[dict]]:
    finished = []
    failures = []
    for idx, target in enumerate(targets, 1):
        nb_id = target["notebook_id"]
        nb_title = target.get("title", nb_id)
        retry_plan = target.get("retry_plan")

        if logger:
            logger(f"Backup start notebook={nb_title} retry={'yes' if retry_plan is not None else 'no'}")

        def progress(ratio: float, msg: str):
            on_progress(idx, len(targets), nb_title, ratio, msg, finished)

        try:
            summary = _backup_notebook(
                client=client,
                notebook_id=nb_id,
                out_base=out_base,
                notebooks=notebooks,
                selection=selection,
                retry_plan=retry_plan,
                on_progress=progress,
                logger=logger,
            )
            line = (
                f"{nb_title}  "
                f"S:{summary['sources_ok']}/{summary['sources_total']}  "
                f"A:{summary['artifacts_ok']}/{summary['artifacts_total']}  "
                f"N:{summary['notes_ok']}/{summary['notes_total']}"
            )
            count = _failure_count(summary["failed"])
            if count:
                line += f"  FAIL:{count}"
                failures.append(
                    {
                        "notebook_id": nb_id,
                        "title": nb_title,
                        "failed": summary["failed"],
                        "full_retry": False,
                    }
                )
            finished.append(line)
        except NotebookLMError as e:
            finished.append(f"{nb_title}  FAIL: {_en_error_text(e)}")
            failures.append(
                {
                    "notebook_id": nb_id,
                    "title": nb_title,
                    "failed": {"sources": [], "artifacts": [], "notes": []},
                    "full_retry": True,
                }
            )
            if logger:
                logger(f"Backup fatal notebook={nb_title} error={_en_error_text(e)}")

        on_progress(idx, len(targets), nb_title, 1.0, "Done", finished)

    return finished, failures


class TerminalFallbackTUI:
    def __init__(self, client: NotebookLMClient, out_base: Path, log_path: Path, notebooks: list[dict]):
        self.client = client
        self.out_base = out_base
        self.log_path = log_path
        self.notebooks = notebooks
        self.selected: set[int] = set()
        self.current = 0
        self.offset = 0
        self.selection = BackupSelection()
        self.last_failures: list[dict] = []
        self.status = "Ready"
        self.key_mode = "windows" if sys.platform == "win32" and msvcrt is not None else "posix"
        self._posix_pending_esc = False
        self._posix_alt_screen = False
        _append_log(self.log_path, f"Session start mode=fallback key={self.key_mode}")

    def _log(self, message: str):
        _append_log(self.log_path, message)

    def _term(self) -> tuple[int, int]:
        size = shutil.get_terminal_size((120, 30))
        return size.lines, size.columns

    def _clear(self):
        if self.key_mode == "windows":
            os.system("cls")
        else:
            # Home + clear to end (less flicker than full terminal reset).
            sys.stdout.write("\033[H\033[J")
            sys.stdout.flush()

    def _setup_screen(self):
        if self.key_mode != "windows" and not self._posix_alt_screen:
            # Switch to alternate screen buffer and hide cursor.
            sys.stdout.write("\033[?1049h\033[?25l")
            sys.stdout.flush()
            self._posix_alt_screen = True

    def _teardown_screen(self):
        if self.key_mode != "windows" and self._posix_alt_screen:
            # Restore cursor and main screen buffer.
            sys.stdout.write("\033[?25h\033[?1049l")
            sys.stdout.flush()
            self._posix_alt_screen = False

    def _render(self, lines: list[str]):
        self._clear()
        out = "\n".join(lines)
        if out:
            sys.stdout.write(out)
            if not out.endswith("\n"):
                sys.stdout.write("\n")
        sys.stdout.flush()

    def _read_key(self) -> str:
        if self.key_mode == "windows":
            return self._read_key_windows()
        return self._read_key_posix()

    def _is_esc_action(self, key: str) -> bool:
        return key == "esc" and self.key_mode == "windows"

    def _read_key_windows(self) -> str:
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            ext = msvcrt.getwch()
            return {"H": "up", "P": "down", "I": "pgup", "Q": "pgdn"}.get(ext, "")
        if ch == "\r":
            return "enter"
        if ch == " ":
            return "space"
        if ch == "\x1b":
            return "esc"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch.lower()

    def _read_key_posix(self) -> str:
        if termios is None or tty is None or select is None:
            raise RuntimeError("POSIX key input is not available on this environment.")

        def parse_escape_sequence(seq: str) -> str:
            if seq in ("A", "[A", "OA") or seq.endswith("A"):
                return "up"
            if seq in ("B", "[B", "OB") or seq.endswith("B"):
                return "down"
            if seq in ("C", "[C", "OC") or seq.endswith("C"):
                return "right"
            if seq in ("D", "[D", "OD") or seq.endswith("D"):
                return "left"
            if seq in ("[5~", "O5~") or seq.endswith("5~"):
                return "pgup"
            if seq in ("[6~", "O6~") or seq.endswith("6~"):
                return "pgdn"
            return ""

        def read_escape_sequence(first: str) -> str:
            seq = first

            # At least one tail byte may arrive with a delay.
            ready, _, _ = select.select([sys.stdin], [], [], 0.18)
            if not ready:
                return parse_escape_sequence(seq)
            seq += sys.stdin.read(1)
            parsed = parse_escape_sequence(seq)
            if parsed:
                return parsed

            while len(seq) < 8:
                ready, _, _ = select.select([sys.stdin], [], [], 0.03)
                if not ready:
                    break
                seq += sys.stdin.read(1)
                parsed = parse_escape_sequence(seq)
                if parsed:
                    return parsed
                if seq[-1].isalpha() or seq[-1] == "~":
                    break

            return parse_escape_sequence(seq)

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)

            if self._posix_pending_esc:
                self._posix_pending_esc = False
                if ch in ("[", "O", "A", "B", "C", "D"):
                    return read_escape_sequence(ch)
                # If ESC was a standalone key, treat the next byte as a normal key.

            # Some terminals may deliver only the trailing arrow byte (A/B/C/D) late.
            # Interpret those as arrow keys, not normal a/b/c/d input.
            if ch in ("A", "B", "C", "D"):
                return parse_escape_sequence(ch)

            if ch == "\x03":
                raise KeyboardInterrupt
            if ch in ("\r", "\n"):
                return "enter"
            if ch == " ":
                return "space"
            if ch == "\x1b":
                # Distinguish bare ESC from escape-sequences (arrows/page keys).
                # Some terminals send bytes with a small delay.
                ready, _, _ = select.select([sys.stdin], [], [], 0.18)
                if not ready:
                    # On Linux, ESC is not used as a quit key, so hold it briefly.
                    self._posix_pending_esc = True
                    return ""
                seq = sys.stdin.read(1)
                if seq not in ("[", "O", "A", "B", "C", "D"):
                    return ""
                return read_escape_sequence(seq)

            # Rarely ESC is consumed first and sequence starts with '['.
            if ch in ("[", "O"):
                ready, _, _ = select.select([sys.stdin], [], [], 0.01)
                if ready:
                    parsed = read_escape_sequence(ch)
                    if parsed:
                        return parsed
                return ""
            return ch.lower()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def _draw_list(self):
        lines: list[str] = []
        h, _w = self._term()
        lines.append("NotebookLM TUI (fallback)")
        lines.append(
            "Up/Down:Move  Space:Select  Enter:Detail  b:Backup  u:Upload  "
            "x:Retry Failed  f:Filter  a:Select All  r:Reload  q:Quit"
        )
        lines.append("-" * 80)

        if not self.notebooks:
            lines.append("No notebooks found. Press r to reload, q to quit.")
            lines.append("")
            lines.append(f"Filter: {self.selection.label()}  RetryQueue: {len(self.last_failures)}")
            lines.append(f"Status: {self.status}")
            self._render(lines)
            return

        list_height = max(3, h - 10)
        self.current = max(0, min(self.current, len(self.notebooks) - 1))
        if self.current < self.offset:
            self.offset = self.current
        if self.current >= self.offset + list_height:
            self.offset = self.current - list_height + 1

        for row in range(list_height):
            idx = self.offset + row
            if idx >= len(self.notebooks):
                break
            nb = self.notebooks[idx]
            mark = "[x]" if idx in self.selected else "[ ]"
            cursor = ">" if idx == self.current else " "
            updated = format_timestamp(nb.get("updated_at"))
            lines.append(f"{cursor} {mark} {idx + 1:>3}  {nb['title']}  (src:{nb['source_count']}, updated:{updated})")

        queued = sum(_entry_failure_count(x) for x in self.last_failures)
        lines.append("")
        lines.append(f"Filter: {self.selection.label()}  RetryQueue: {queued}  Log: {self.log_path.name}")
        lines.append(f"Selected: {len(self.selected)}")
        lines.append(f"Status: {self.status}")
        self._render(lines)

    def _browse_lines(self, title: str, lines: list[str]):
        pos = 0
        while True:
            h, _w = self._term()
            view_h = max(5, h - 5)
            pos = max(0, min(pos, max(0, len(lines) - view_h)))
            view: list[str] = [title, "Up/Down:Scroll  q/Enter:Back", "-" * 80]
            for i in range(view_h):
                idx = pos + i
                if idx >= len(lines):
                    break
                view.append(lines[idx])
            self._render(view)
            key = self._read_key()
            if key in ("q", "enter") or self._is_esc_action(key):
                return
            if key in ("down", "j"):
                pos = min(pos + 1, max(0, len(lines) - view_h))
            elif key in ("up", "k"):
                pos = max(0, pos - 1)
            elif key == "pgdn":
                pos = min(pos + view_h, max(0, len(lines) - view_h))
            elif key == "pgup":
                pos = max(0, pos - view_h)

    def _open_detail(self):
        if not self.notebooks:
            return
        nb = self.notebooks[self.current]
        self._render([f"Loading detail: {nb['title']} ..."])
        try:
            lines = _build_detail_lines(self.client, nb)
        except NotebookLMError as e:
            self.status = f"Failed to load detail: {_en_error_text(e)}"
            return
        self._browse_lines(f"Detail - {nb['title']}", lines)

    def _draw_backup(self, nb_index: int, nb_total: int, nb_title: str, nb_ratio: float, message: str, finished: list[str]):
        h, w = self._term()
        overall_ratio = ((nb_index - 1) + nb_ratio) / max(1, nb_total)
        bar_w = max(10, min(60, w - 20))
        lines = [
            "Backup Running",
            "",
            f"Notebook {nb_index}/{nb_total}: {nb_title}",
            f"Current : {_bar(bar_w, nb_ratio)} {int(nb_ratio * 100):>3}%",
            f"Overall : {_bar(bar_w, overall_ratio)} {int(overall_ratio * 100):>3}%",
            "",
            f"Step: {message}",
            "",
            "Completed:",
        ]
        for item in finished[-max(0, h - 12):]:
            lines.append(f"  {item}")
        self._render(lines)

    def _show_finished(self, lines: list[str]):
        view = ["Backup Finished", "", f"Output: {self.out_base}", ""]
        for line in lines:
            view.append(line)
        view.append("")
        view.append("Press Enter to return to the list")
        self._render(view)
        while self._read_key() != "enter":
            pass

    def _prompt_line(self, title: str, prompt: str, default: str = "") -> str:
        self._clear()
        print(title)
        print("-" * 80)
        print(prompt)
        if default:
            print(f"(default: {default})")
        if self.key_mode != "windows":
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()
        try:
            value = input("> ").strip()
        finally:
            if self.key_mode != "windows":
                sys.stdout.write("\033[?25l")
                sys.stdout.flush()
        if not value:
            return default
        return value

    def _draw_upload(self, nb_title: str, ratio: float, message: str, finished: list[str]):
        h, w = self._term()
        bar_w = max(10, min(60, w - 20))
        lines = [
            "Upload Running",
            "",
            f"Notebook: {nb_title}",
            f"Progress: {_bar(bar_w, ratio)} {int(ratio * 100):>3}%",
            "",
            f"Step: {message}",
            "",
            "Completed:",
        ]
        for item in finished[-max(0, h - 11):]:
            lines.append(f"  {item}")
        self._render(lines)

    def _run_upload_flow(self, target_notebook_id: str, target_notebook_title: str):
        paths_raw = self._prompt_line(
            f"Upload -> {target_notebook_title}",
            "Enter file/folder paths separated by ';' (leave blank to skip)",
        )
        urls_raw = self._prompt_line(
            f"Upload -> {target_notebook_title}",
            "Enter URLs separated by ';' (leave blank to skip)",
        )

        paths = _split_user_list(paths_raw, separators=";")
        urls = _split_user_list(urls_raw, separators=";")
        files = _collect_files_en(paths) if paths else []

        if not files and not urls:
            self.status = "No upload targets provided"
            return

        if paths and not files and not urls:
            self.status = "No uploadable files found from the specified paths"
            return

        self._log(
            f"Upload start notebook={target_notebook_title} files={len(files)} urls={len(urls)}"
        )

        finished = []

        def on_progress(ratio: float, msg: str):
            self._draw_upload(target_notebook_title, ratio, msg, finished)

        summary = _upload_to_notebook(
            client=self.client,
            notebook_id=target_notebook_id,
            files=files,
            urls=urls,
            on_progress=on_progress,
            logger=self._log,
        )

        line = (
            f"{target_notebook_title}  "
            f"Files:{summary['files_ok']}/{summary['files_total']}  "
            f"URLs:{summary['urls_ok']}/{summary['urls_total']}"
        )
        if summary["files_fail"] or summary["urls_fail"]:
            line += f"  FAIL:{summary['files_fail'] + summary['urls_fail']}"
        finished.append(line)

        view = ["Upload Finished", "", line]
        if summary["failed_files"]:
            view.append("Failed files:")
            for p in summary["failed_files"][:20]:
                view.append(f"  {p}")
        if summary["failed_urls"]:
            view.append("Failed URLs:")
            for u in summary["failed_urls"][:20]:
                view.append(f"  {u}")
        view.append("")
        view.append("Press Enter to return to the list")
        self._render(view)
        while self._read_key() != "enter":
            pass

        if summary["files_fail"] or summary["urls_fail"]:
            self.status = "Upload finished (with failures)"
        else:
            self.status = "Upload finished"

        # Refresh list to reflect updated counts/timestamps after upload.
        try:
            current_id = target_notebook_id
            self.notebooks = self.client.list_notebooks()
            for i, nb in enumerate(self.notebooks):
                if nb["id"] == current_id:
                    self.current = i
                    break
            self.offset = max(0, self.current - 3)
        except NotebookLMError:
            pass

        self._log(
            f"Upload summary notebook={target_notebook_title} "
            f"files={summary['files_ok']}/{summary['files_total']} "
            f"urls={summary['urls_ok']}/{summary['urls_total']}"
        )

    def _upload_menu(self):
        while True:
            self._render(
                [
                    "Upload Menu",
                    "-" * 80,
                    "1: Create a new notebook and upload",
                    "2: Upload to current notebook",
                    "q/Enter: Back",
                ]
            )
            key = self._read_key()
            if key in ("q", "enter") or self._is_esc_action(key):
                return
            if key == "1":
                title = self._prompt_line("Create New Notebook", "Enter notebook title")
                if not title:
                    self.status = "Title is empty"
                    return
                try:
                    notebook_id = self.client.create_notebook(title)
                except NotebookLMError as e:
                    self.status = f"Failed to create notebook: {_en_error_text(e)}"
                    return

                self.notebooks.append(
                    {"id": notebook_id, "title": title, "source_count": 0, "updated_at": None}
                )
                self.current = len(self.notebooks) - 1
                self.offset = max(0, self.current - 3)
                self._log(f"Notebook created id={notebook_id} title={title}")
                self._run_upload_flow(notebook_id, title)
                return
            if key == "2":
                if not self.notebooks:
                    self.status = "No notebooks available"
                    return
                nb = self.notebooks[self.current]
                self._run_upload_flow(nb["id"], nb["title"])
                return

    def _run_targets(self, targets: list[dict], mode_label: str):
        if not targets:
            self.status = "No targets"
            return
        finished, failures = _run_backup_batch(
            client=self.client,
            out_base=self.out_base,
            notebooks=self.notebooks,
            targets=targets,
            selection=self.selection,
            on_progress=self._draw_backup,
            logger=self._log,
        )
        self.last_failures = failures
        if failures:
            queued = sum(_entry_failure_count(x) for x in failures)
            self.status = f"{mode_label} finished: {queued} failed item(s) (press x to retry)"
        else:
            self.status = f"{mode_label} finished: all succeeded"
        self._show_finished(finished)

    def _run_backup(self):
        if not self.notebooks:
            return
        targets_idx = sorted(self.selected) if self.selected else [self.current]
        targets = [{"notebook_id": self.notebooks[i]["id"], "title": self.notebooks[i]["title"]} for i in targets_idx]
        self._run_targets(targets, "Backup")

    def _retry_failures(self):
        if not self.last_failures:
            self.status = "No retry targets"
            return
        targets = []
        for entry in self.last_failures:
            targets.append(
                {
                    "notebook_id": entry["notebook_id"],
                    "title": entry.get("title", entry["notebook_id"]),
                    "retry_plan": None if entry.get("full_retry") else entry.get("failed"),
                }
            )
        self._run_targets(targets, "Retry")

    def _toggle_all(self):
        if len(self.selected) == len(self.notebooks):
            self.selected.clear()
        else:
            self.selected = set(range(len(self.notebooks)))

    def _reload(self):
        try:
            self.notebooks = self.client.list_notebooks()
            self.current = 0
            self.offset = 0
            self.selected.clear()
            self.status = f"Reload complete: {len(self.notebooks)} notebook(s)"
            self._log(f"Reload notebook list count={len(self.notebooks)}")
        except NotebookLMError as e:
            self.status = f"Reload failed: {_en_error_text(e)}"

    def _configure_filter(self):
        while True:
            self._render(
                [
                    "Backup Target Filter",
                    "1:Sources  2:Artifacts  3:Notes  Enter/q:Back",
                    "-" * 50,
                    f"1 Sources  : {'ON ' if self.selection.sources else 'OFF'}",
                    f"2 Artifacts: {'ON ' if self.selection.artifacts else 'OFF'}",
                    f"3 Notes    : {'ON ' if self.selection.notes else 'OFF'}",
                    "",
                    f"Current: {self.selection.label()}",
                ]
            )
            key = self._read_key()
            if key in ("enter", "q") or self._is_esc_action(key):
                return
            if key == "1":
                if not _toggle_selection_component(self.selection, "sources"):
                    self.status = "At least one target must stay ON"
            elif key == "2":
                if not _toggle_selection_component(self.selection, "artifacts"):
                    self.status = "At least one target must stay ON"
            elif key == "3":
                if not _toggle_selection_component(self.selection, "notes"):
                    self.status = "At least one target must stay ON"

    def run(self):
        self._setup_screen()
        try:
            while True:
                self._draw_list()
                key = self._read_key()
                if key == "q" or self._is_esc_action(key):
                    self._log("Session end")
                    return
                if key in ("down", "j"):
                    if self.current < len(self.notebooks) - 1:
                        self.current += 1
                elif key in ("up", "k"):
                    if self.current > 0:
                        self.current -= 1
                elif key == "space":
                    if self.current in self.selected:
                        self.selected.remove(self.current)
                    else:
                        self.selected.add(self.current)
                elif key == "enter":
                    self._open_detail()
                elif key == "b":
                    self._run_backup()
                elif key == "u":
                    self._upload_menu()
                elif key == "x":
                    self._retry_failures()
                elif key == "f":
                    self._configure_filter()
                elif key == "r":
                    self._reload()
                elif key == "a":
                    self._toggle_all()
        finally:
            self._teardown_screen()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="nlm-tui-en",
        description="NotebookLM TUI (browse + batch backup)",
    )
    parser.add_argument("-o", "--output", type=str, default=None, help="Output directory (default: ./downloads)")
    parser.add_argument("--cookies", type=str, default=None, help="Path to cookie file")
    parser.add_argument("--log", type=str, default=None, help="Log file path (default: <output>/nlm_tui.log)")
    args = parser.parse_args()

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("[ERROR] nlm-tui-en must be run in an interactive terminal.", file=sys.stderr)
        return 1

    if sys.platform == "win32" and msvcrt is None:
        print("[ERROR] Failed to load Windows key input module (msvcrt).", file=sys.stderr)
        return 1

    if sys.platform != "win32" and (termios is None or tty is None or select is None):
        print("[ERROR] Failed to load POSIX key input modules (termios/tty/select).", file=sys.stderr)
        return 1

    out_base = Path(args.output) if args.output else Path.cwd() / "downloads"
    log_path = Path(args.log) if args.log else out_base / "nlm_tui.log"

    try:
        client = NotebookLMClient(cookies_path=args.cookies)
    except AuthenticationError as e:
        print(f"[ERROR] {_en_error_text(e)}", file=sys.stderr)
        return 1

    try:
        notebooks = client.list_notebooks()
    except NotebookLMError as e:
        print(f"[ERROR] Failed to fetch notebook list: {_en_error_text(e)}", file=sys.stderr)
        return 1

    try:
        app = TerminalFallbackTUI(client, out_base, log_path, notebooks)
        app.run()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
