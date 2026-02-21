"""
nlm-tui-curses: NotebookLM TUI (curses, flicker-free)

On Windows, run `pip install windows-curses` first.

Key bindings:
  Up/Down (j/k) : Navigate
  Space         : Toggle selection
  Enter         : Show detail tree
  b             : Backup selected notebooks (current if none selected)
  u             : Upload menu (create new / add to existing)
  x             : Retry last failed items
  f             : Backup filter (Sources/Artifacts/Notes)
  r             : Reload notebook list
  a             : Select all / Deselect all
  q             : Quit / Back
"""

from __future__ import annotations

import argparse
import curses
import json
import locale
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from notebooklm_client import AuthenticationError, NotebookLMClient, NotebookLMError
from nlm_backup import ARTIFACT_EXTENSIONS, format_timestamp, sanitize_filename
from nlm_upload import TEXT_EXTENSIONS, UPLOAD_FILE_TYPES, collect_files


# ---------------------------------------------------------------------------
# BackupSelection (same as nlm_tui.py)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Helper functions (ported from nlm_tui.py)
# ---------------------------------------------------------------------------

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
    step("Saving metadata")

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
            finished.append(f"{nb_title}  FAIL: {e}")
            failures.append(
                {
                    "notebook_id": nb_id,
                    "title": nb_title,
                    "failed": {"sources": [], "artifacts": [], "notes": []},
                    "full_retry": True,
                }
            )
            if logger:
                logger(f"Backup fatal notebook={nb_title} error={e}")

        on_progress(idx, len(targets), nb_title, 1.0, "Done", finished)

    return finished, failures


# ---------------------------------------------------------------------------
# Color pair constants
# ---------------------------------------------------------------------------

CP_HEADER = 1
CP_CURSOR = 2
CP_SELECTED = 3
CP_CURSOR_SELECTED = 4
CP_FOOTER = 5
CP_PROGRESS = 6
CP_ERROR = 7


# ---------------------------------------------------------------------------
# CursesTUI
# ---------------------------------------------------------------------------

class CursesTUI:
    def __init__(
        self,
        stdscr: curses.window,
        client: NotebookLMClient,
        out_base: Path,
        log_path: Path,
        notebooks: list[dict],
    ):
        self.stdscr = stdscr
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
        self.h = 0
        self.w = 0
        self._init_colors()
        self._update_dimensions()
        _append_log(self.log_path, "Session start mode=curses")

    # ------------------------------------------------------------------ #
    # Initialization helpers
    # ------------------------------------------------------------------ #

    def _init_colors(self):
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(CP_HEADER, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(CP_CURSOR, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(CP_SELECTED, curses.COLOR_YELLOW, -1)
        curses.init_pair(CP_CURSOR_SELECTED, curses.COLOR_YELLOW, curses.COLOR_WHITE)
        curses.init_pair(CP_FOOTER, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(CP_PROGRESS, curses.COLOR_GREEN, -1)
        curses.init_pair(CP_ERROR, curses.COLOR_RED, -1)

    def _update_dimensions(self):
        self.h, self.w = self.stdscr.getmaxyx()

    # ------------------------------------------------------------------ #
    # Safe drawing
    # ------------------------------------------------------------------ #

    def _safe_addstr(self, win: curses.window, y: int, x: int, text: str, attr: int = 0):
        """addstr that silently ignores writes to the bottom-right corner."""
        h, w = win.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        max_len = w - x
        if max_len <= 0:
            return
        truncated = text[:max_len]
        try:
            win.addstr(y, x, truncated, attr)
        except curses.error:
            # Writing to the last cell of a window raises an error after
            # the character is actually written — safe to ignore.
            pass

    # ------------------------------------------------------------------ #
    # Key input
    # ------------------------------------------------------------------ #

    def _read_key(self) -> str:
        """Read a key press and return a normalized string name."""
        try:
            ch = self.stdscr.get_wch()
        except curses.error:
            return ""

        if isinstance(ch, int):
            mapping = {
                curses.KEY_UP: "up",
                curses.KEY_DOWN: "down",
                curses.KEY_LEFT: "left",
                curses.KEY_RIGHT: "right",
                curses.KEY_PPAGE: "pgup",
                curses.KEY_NPAGE: "pgdn",
                curses.KEY_ENTER: "enter",
                curses.KEY_RESIZE: "resize",
                curses.KEY_HOME: "home",
                curses.KEY_END: "end",
            }
            return mapping.get(ch, "")

        # str character
        if ch == "\n" or ch == "\r":
            return "enter"
        if ch == " ":
            return "space"
        if ch == "\x1b":
            return "esc"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch.lower()

    # ------------------------------------------------------------------ #
    # Main list view
    # ------------------------------------------------------------------ #

    def _draw_list(self):
        self._update_dimensions()
        self.stdscr.erase()

        # Header
        header = " NotebookLM TUI (curses) "
        self._safe_addstr(self.stdscr, 0, 0, header.center(self.w), curses.color_pair(CP_HEADER) | curses.A_BOLD)

        help_line = "Up/Dn:Move Space:Select Enter:Detail b:Backup u:Upload x:Retry f:Filter a:All r:Reload q:Quit"
        self._safe_addstr(self.stdscr, 1, 0, help_line[:self.w])
        self._safe_addstr(self.stdscr, 2, 0, "─" * min(self.w, 80))

        if not self.notebooks:
            self._safe_addstr(self.stdscr, 4, 2, "No notebooks found. Press r to reload, q to quit.")
            self._draw_footer()
            self.stdscr.noutrefresh()
            curses.doupdate()
            return

        list_height = max(3, self.h - 7)
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
            is_cur = idx == self.current
            is_sel = idx in self.selected

            mark = "[x]" if is_sel else "[ ]"
            cursor = ">" if is_cur else " "
            updated = format_timestamp(nb.get("updated_at"))
            line = f"{cursor} {mark} {idx + 1:>3}  {nb['title']}  (src:{nb['source_count']}, updated:{updated})"

            if is_cur and is_sel:
                attr = curses.color_pair(CP_CURSOR_SELECTED) | curses.A_BOLD
            elif is_cur:
                attr = curses.color_pair(CP_CURSOR)
            elif is_sel:
                attr = curses.color_pair(CP_SELECTED) | curses.A_BOLD
            else:
                attr = 0

            y = 3 + row
            if y >= self.h - 3:
                break
            # Fill the whole line for highlight
            self._safe_addstr(self.stdscr, y, 0, " " * (self.w - 1), attr)
            self._safe_addstr(self.stdscr, y, 0, line, attr)

        self._draw_footer()
        self.stdscr.noutrefresh()
        curses.doupdate()

    def _draw_footer(self):
        queued = sum(_entry_failure_count(x) for x in self.last_failures)
        footer_filter = f" Filter: {self.selection.label()}  RetryQueue: {queued}  Selected: {len(self.selected)}  Log: {self.log_path.name} "
        footer_status = f" Status: {self.status} "

        y_filter = self.h - 2
        y_status = self.h - 1
        if y_filter < 4:
            return

        self._safe_addstr(self.stdscr, y_filter, 0, footer_filter.ljust(self.w - 1), curses.color_pair(CP_FOOTER))
        self._safe_addstr(self.stdscr, y_status, 0, footer_status.ljust(self.w - 1), curses.color_pair(CP_FOOTER))

    # ------------------------------------------------------------------ #
    # Detail view with pad scrolling
    # ------------------------------------------------------------------ #

    def _browse_lines(self, title: str, lines: list[str]):
        pos = 0
        while True:
            self._update_dimensions()
            view_h = max(3, self.h - 4)
            total_lines = len(lines)
            max_pos = max(0, total_lines - view_h)
            pos = max(0, min(pos, max_pos))

            self.stdscr.erase()
            self._safe_addstr(self.stdscr, 0, 0, f" {title} ", curses.color_pair(CP_HEADER) | curses.A_BOLD)
            self._safe_addstr(self.stdscr, 1, 0, "Up/Dn:Scroll  PgUp/PgDn:Page  q/Enter:Back")
            self._safe_addstr(self.stdscr, 2, 0, "─" * min(self.w, 80))

            for i in range(view_h):
                line_idx = pos + i
                if line_idx >= total_lines:
                    break
                self._safe_addstr(self.stdscr, 3 + i, 0, lines[line_idx])

            # Scroll indicator
            if total_lines > view_h:
                indicator = f" [{pos + 1}-{min(pos + view_h, total_lines)}/{total_lines}] "
                self._safe_addstr(self.stdscr, self.h - 1, 0, indicator, curses.color_pair(CP_FOOTER))

            self.stdscr.noutrefresh()
            curses.doupdate()

            key = self._read_key()
            if key in ("q", "enter", "esc"):
                return
            if key in ("down", "j"):
                pos = min(pos + 1, max_pos)
            elif key in ("up", "k"):
                pos = max(0, pos - 1)
            elif key == "pgdn":
                pos = min(pos + view_h, max_pos)
            elif key == "pgup":
                pos = max(0, pos - view_h)
            elif key == "home":
                pos = 0
            elif key == "end":
                pos = max_pos
            elif key == "resize":
                pass  # just redraw

    def _open_detail(self):
        if not self.notebooks:
            return
        nb = self.notebooks[self.current]

        self.stdscr.erase()
        self._safe_addstr(self.stdscr, self.h // 2, 2, f"Loading detail: {nb['title']} ...")
        self.stdscr.noutrefresh()
        curses.doupdate()

        try:
            lines = _build_detail_lines(self.client, nb)
        except NotebookLMError as e:
            self.status = f"Failed to load detail: {e}"
            return
        self._browse_lines(f"Detail - {nb['title']}", lines)

    # ------------------------------------------------------------------ #
    # Progress drawing (backup / upload)
    # ------------------------------------------------------------------ #

    def _draw_progress(self, title: str, lines: list[str]):
        """Generic progress screen: erase + write lines + doupdate."""
        self._update_dimensions()
        self.stdscr.erase()
        for i, line in enumerate(lines):
            if i >= self.h:
                break
            self._safe_addstr(self.stdscr, i, 0, line)
        self.stdscr.noutrefresh()
        curses.doupdate()

    def _draw_backup(self, nb_index: int, nb_total: int, nb_title: str, nb_ratio: float, message: str, finished: list[str]):
        self._update_dimensions()
        overall_ratio = ((nb_index - 1) + nb_ratio) / max(1, nb_total)
        bar_w = max(10, min(60, self.w - 20))

        self.stdscr.erase()
        self._safe_addstr(self.stdscr, 0, 0, " Backup Running ", curses.color_pair(CP_HEADER) | curses.A_BOLD)
        self._safe_addstr(self.stdscr, 2, 0, f"Notebook {nb_index}/{nb_total}: {nb_title}")
        self._safe_addstr(self.stdscr, 3, 0, f"Current : {_bar(bar_w, nb_ratio)} {int(nb_ratio * 100):>3}%", curses.color_pair(CP_PROGRESS))
        self._safe_addstr(self.stdscr, 4, 0, f"Overall : {_bar(bar_w, overall_ratio)} {int(overall_ratio * 100):>3}%", curses.color_pair(CP_PROGRESS))
        self._safe_addstr(self.stdscr, 6, 0, f"Step: {message}")
        self._safe_addstr(self.stdscr, 8, 0, "Completed:")

        max_items = max(0, self.h - 10)
        for i, item in enumerate(finished[-max_items:]):
            y = 9 + i
            if y >= self.h:
                break
            self._safe_addstr(self.stdscr, y, 2, item)

        self.stdscr.noutrefresh()
        curses.doupdate()

    def _draw_upload(self, nb_title: str, ratio: float, message: str, finished: list[str]):
        self._update_dimensions()
        bar_w = max(10, min(60, self.w - 20))

        self.stdscr.erase()
        self._safe_addstr(self.stdscr, 0, 0, " Upload Running ", curses.color_pair(CP_HEADER) | curses.A_BOLD)
        self._safe_addstr(self.stdscr, 2, 0, f"Notebook: {nb_title}")
        self._safe_addstr(self.stdscr, 3, 0, f"Progress: {_bar(bar_w, ratio)} {int(ratio * 100):>3}%", curses.color_pair(CP_PROGRESS))
        self._safe_addstr(self.stdscr, 5, 0, f"Step: {message}")
        self._safe_addstr(self.stdscr, 7, 0, "Completed:")

        max_items = max(0, self.h - 9)
        for i, item in enumerate(finished[-max_items:]):
            y = 8 + i
            if y >= self.h:
                break
            self._safe_addstr(self.stdscr, y, 2, item)

        self.stdscr.noutrefresh()
        curses.doupdate()

    # ------------------------------------------------------------------ #
    # Message screens
    # ------------------------------------------------------------------ #

    def _show_finished(self, lines: list[str]):
        all_lines = ["Backup Finished", "", f"Output: {self.out_base}", ""]
        all_lines.extend(lines)
        all_lines.append("")
        all_lines.append("Press Enter to return to list")
        self._browse_lines("Backup Finished", all_lines)

    def _show_message_wait(self, title: str, messages: list[str]):
        """Show messages and wait for Enter."""
        self._update_dimensions()
        self.stdscr.erase()
        self._safe_addstr(self.stdscr, 0, 0, f" {title} ", curses.color_pair(CP_HEADER) | curses.A_BOLD)
        for i, msg in enumerate(messages):
            if i + 2 >= self.h:
                break
            self._safe_addstr(self.stdscr, 2 + i, 0, msg)
        self.stdscr.noutrefresh()
        curses.doupdate()
        while True:
            key = self._read_key()
            if key in ("enter", "q", "esc"):
                return

    # ------------------------------------------------------------------ #
    # Text input (exit curses temporarily for IME support)
    # ------------------------------------------------------------------ #

    def _prompt_line(self, title: str, prompt: str, default: str = "") -> str:
        """Leave curses temporarily so that input() works with IME."""
        curses.endwin()
        print(title)
        print("-" * 80)
        print(prompt)
        if default:
            print(f"(default: {default})")
        try:
            value = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            value = ""
        # Restore curses
        self.stdscr.refresh()
        if not value:
            return default
        return value

    # ------------------------------------------------------------------ #
    # Upload menu
    # ------------------------------------------------------------------ #

    def _upload_menu(self):
        while True:
            self._update_dimensions()
            self.stdscr.erase()
            self._safe_addstr(self.stdscr, 0, 0, " Upload Menu ", curses.color_pair(CP_HEADER) | curses.A_BOLD)
            self._safe_addstr(self.stdscr, 1, 0, "─" * min(self.w, 80))
            self._safe_addstr(self.stdscr, 3, 2, "1: Create new notebook and upload")
            self._safe_addstr(self.stdscr, 4, 2, "2: Upload to current notebook")
            self._safe_addstr(self.stdscr, 5, 2, "q/Enter: Back")
            self.stdscr.noutrefresh()
            curses.doupdate()

            key = self._read_key()
            if key in ("q", "enter", "esc"):
                return
            if key == "1":
                title = self._prompt_line("Create New Notebook", "Enter notebook title")
                if not title:
                    self.status = "Title is empty"
                    return
                try:
                    notebook_id = self.client.create_notebook(title)
                except NotebookLMError as e:
                    self.status = f"Creation failed: {e}"
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

    def _run_upload_flow(self, target_notebook_id: str, target_notebook_title: str):
        paths_raw = self._prompt_line(
            f"Upload -> {target_notebook_title}",
            "Enter file/folder paths separated by ';' (empty to skip)",
        )
        urls_raw = self._prompt_line(
            f"Upload -> {target_notebook_title}",
            "Enter URLs separated by ';' (empty to skip)",
        )

        paths = _split_user_list(paths_raw, separators=";")
        urls = _split_user_list(urls_raw, separators=";")
        files = collect_files(paths) if paths else []

        if not files and not urls:
            self.status = "No upload targets"
            return

        if paths and not files and not urls:
            self.status = "No uploadable files found in specified paths"
            return

        self._log(
            f"Upload start notebook={target_notebook_title} files={len(files)} urls={len(urls)}"
        )

        finished: list[str] = []

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

        view = [line]
        if summary["failed_files"]:
            view.append("Failed files:")
            for p in summary["failed_files"][:20]:
                view.append(f"  {p}")
        if summary["failed_urls"]:
            view.append("Failed URLs:")
            for u in summary["failed_urls"][:20]:
                view.append(f"  {u}")
        view.append("")
        view.append("Press Enter to return to list")
        self._show_message_wait("Upload Finished", view)

        if summary["files_fail"] or summary["urls_fail"]:
            self.status = "Upload finished (with failures)"
        else:
            self.status = "Upload finished"

        # Refresh notebook list to reflect new source counts
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

    # ------------------------------------------------------------------ #
    # Filter configuration
    # ------------------------------------------------------------------ #

    def _configure_filter(self):
        while True:
            self._update_dimensions()
            self.stdscr.erase()
            self._safe_addstr(self.stdscr, 0, 0, " Backup Filter ", curses.color_pair(CP_HEADER) | curses.A_BOLD)
            self._safe_addstr(self.stdscr, 1, 0, "1:Sources  2:Artifacts  3:Notes  Enter/q:Back")
            self._safe_addstr(self.stdscr, 2, 0, "─" * min(self.w, 50))
            self._safe_addstr(self.stdscr, 4, 2, f"1 Sources  : {'ON ' if self.selection.sources else 'OFF'}")
            self._safe_addstr(self.stdscr, 5, 2, f"2 Artifacts: {'ON ' if self.selection.artifacts else 'OFF'}")
            self._safe_addstr(self.stdscr, 6, 2, f"3 Notes    : {'ON ' if self.selection.notes else 'OFF'}")
            self._safe_addstr(self.stdscr, 8, 2, f"Current: {self.selection.label()}")
            if self.status and self.status != "Ready":
                self._safe_addstr(self.stdscr, 10, 2, self.status, curses.color_pair(CP_ERROR))
            self.stdscr.noutrefresh()
            curses.doupdate()

            key = self._read_key()
            if key in ("enter", "q", "esc"):
                return
            if key == "1":
                if not _toggle_selection_component(self.selection, "sources"):
                    self.status = "At least one must remain ON"
            elif key == "2":
                if not _toggle_selection_component(self.selection, "artifacts"):
                    self.status = "At least one must remain ON"
            elif key == "3":
                if not _toggle_selection_component(self.selection, "notes"):
                    self.status = "At least one must remain ON"
            elif key == "resize":
                pass

    # ------------------------------------------------------------------ #
    # Backup / retry
    # ------------------------------------------------------------------ #

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
            self.status = f"{mode_label} done: {queued} failed (press x to retry)"
        else:
            self.status = f"{mode_label} done: all succeeded"
        self._show_finished(finished)

    def _run_backup(self):
        if not self.notebooks:
            return
        targets_idx = sorted(self.selected) if self.selected else [self.current]
        targets = [{"notebook_id": self.notebooks[i]["id"], "title": self.notebooks[i]["title"]} for i in targets_idx]
        self._run_targets(targets, "Backup")

    def _retry_failures(self):
        if not self.last_failures:
            self.status = "No items to retry"
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

    # ------------------------------------------------------------------ #
    # Selection helpers
    # ------------------------------------------------------------------ #

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
            self.status = f"Reloaded: {len(self.notebooks)} notebooks"
            self._log(f"Reload notebook list count={len(self.notebooks)}")
        except NotebookLMError as e:
            self.status = f"Reload failed: {e}"

    # ------------------------------------------------------------------ #
    # Logging
    # ------------------------------------------------------------------ #

    def _log(self, message: str):
        _append_log(self.log_path, message)

    # ------------------------------------------------------------------ #
    # Main event loop
    # ------------------------------------------------------------------ #

    def run(self):
        curses.curs_set(0)  # Hide cursor
        self.stdscr.keypad(True)
        self.stdscr.timeout(-1)  # Blocking input

        while True:
            self._draw_list()
            key = self._read_key()
            if key in ("q", "esc"):
                self._log("Session end")
                return
            if key in ("down", "j"):
                if self.current < len(self.notebooks) - 1:
                    self.current += 1
            elif key in ("up", "k"):
                if self.current > 0:
                    self.current -= 1
            elif key == "space":
                if self.notebooks:
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
            elif key == "resize":
                self._update_dimensions()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    locale.setlocale(locale.LC_ALL, "")

    parser = argparse.ArgumentParser(
        prog="nlm-tui-curses",
        description="NotebookLM TUI (curses, flicker-free)",
    )
    parser.add_argument("-o", "--output", type=str, default=None, help="Output directory (default: ./downloads)")
    parser.add_argument("--cookies", type=str, default=None, help="Path to cookies file")
    parser.add_argument("--log", type=str, default=None, help="Path to log file (default: <output>/nlm_tui_curses.log)")
    args = parser.parse_args()

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("[ERROR] nlm-tui-curses requires an interactive terminal.", file=sys.stderr)
        return 1

    out_base = Path(args.output) if args.output else Path.cwd() / "downloads"
    log_path = Path(args.log) if args.log else out_base / "nlm_tui_curses.log"

    try:
        client = NotebookLMClient(cookies_path=args.cookies)
    except AuthenticationError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    try:
        notebooks = client.list_notebooks()
    except NotebookLMError as e:
        print(f"[ERROR] Failed to list notebooks: {e}", file=sys.stderr)
        return 1

    def _curses_main(stdscr: curses.window):
        app = CursesTUI(stdscr, client, out_base, log_path, notebooks)
        app.run()

    try:
        curses.wrapper(_curses_main)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
