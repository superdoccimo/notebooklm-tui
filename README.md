# notebooklm-tui

[Japanese README](README.ja.md)

**Gemini Notebook (formerly NotebookLM) has no official full-workspace backup. This tool lets you export your notebook data.**

> If this tool saved your Gemini Notebook / NotebookLM data, please consider giving it a star.

A zero-dependency Python toolkit that backs up Gemini Notebook (formerly NotebookLM) workspaces and semantically restores the parts that have faithful server-side write paths.

- Back up sources, preferring original uploaded files when Gemini Notebook exposes a direct download; otherwise save extracted text/rendered content
- Export artifacts (audio, video, slides + PPTX, reports, infographics, data tables, mindmaps)
- Save flashcards and quizzes with schema-tolerant parsing for newer quiz formats
- Preserve a raw JSON snapshot of every Studio artifact for forward compatibility
- Backup notes
- Restore URLs/original files, native Notes, and note-backed mind maps without silently turning fallback files into different source types
- No external Python packages required

> **September 2026 compatibility:** The client now uses the current `notebook.google.com` host and migrated request wrappers for notebook create/read, URL/text source add, and file registration. Quiz export tolerates newer short-answer / multiple-select / fill-in-the-blank schema shapes, interactive mind-map artifacts export as Markdown + JSON, all CLI/TUI variants share the same artifact exporter, and every Studio artifact also gets a raw snapshot under `artifacts/_raw/`. Interactive Learning Overview payloads are preserved as HTML/structured data when exposed by the current artifact row; formats not yet observed live are not claimed as losslessly reconstructed.

## Video & Articles

- Video (English): https://youtu.be/SJuvHdte7tw
- Article (English): https://betelgeuse.work/notebooklm-tui/
- Article (Spanish): https://ehrigite.com/notebooklm-tui/

- `nlm-login`: Get NotebookLM auth cookies from your browser (Edge/Chrome/Brave/Firefox)
- `nlm-backup`: Download sources, artifacts, and notes
- `nlm-upload`: Upload files/URLs and perform semantics-aware restore from backup folders
- `nlm-tui`: Japanese UI terminal TUI
- `nlm-tui-en`: English UI terminal TUI
- `nlm-tui-curses`: Optional curses-based flicker-reduced TUI (experimental; may require extra setup on Windows)
- `nlm-canary`: Live compatibility check for a real Gemini Notebook before release

Core CLI tools and standard TUIs run on Python standard library only (no third-party packages required).
On Windows/Python 3.14, prefer `nlm_tui.py` or `nlm_tui_en.py`. `nlm_tui_curses.py` may require extra setup on Windows (see below).

## Quick Start

```bash
# 1) Clone the repository
git clone https://github.com/superdoccimo/notebooklm-tui.git
cd notebooklm-tui

# 2) Authenticate (opens your browser)
python nlm_login.py

# 3) List notebooks
python nlm_backup.py --list

# 4) Backup all notebooks
python nlm_backup.py --all
```

Upload examples:

```bash
# Upload files to a new notebook
python nlm_upload.py "My Research" paper.pdf notes.md

# Semantics-aware restore from a previous backup folder
python nlm_upload.py --restore ./downloads/My_Notebook/

# Optional: how long each restored source may take to become ready
python nlm_upload.py --restore ./downloads/My_Notebook/ --wait-timeout 300
```

TUI example:

```bash
# Recommended on Windows / Python 3.14 (no extra packages)
# Interactive notebook browser + backup (Japanese UI)
python nlm_tui.py

# Interactive notebook browser + backup (English UI)
python nlm_tui_en.py

# Optional when curses is available
# Interactive notebook browser + backup (curses / reduced flicker)
python nlm_tui_curses.py
```

## Prerequisites

| Requirement | Check command | Notes |
|---|---|---|
| Python 3.10+ | `python --version` | [python.org](https://www.python.org/downloads/) |
| One browser | - | Edge / Chrome / Brave / Firefox |
| Google account | - | Must have access to NotebookLM |

No extra package installation is required for the core CLI/TUI tools (`nlm-login`, `nlm-backup`, `nlm-upload`, `nlm-tui`, `nlm-tui-en`).

> On Linux, browser auto-detection checks `firefox`, `google-chrome`, `chromium`, and `brave-browser`.

## Step 1: Authentication (`nlm-login`)

```bash
# Use default browser preference (Windows: Edge first / Linux: Firefox first)
python nlm_login.py

# Choose a specific browser
python nlm_login.py --browser chrome
python nlm_login.py --browser brave
python nlm_login.py --browser firefox

# Explicit Firefox profile path
python nlm_login.py --browser firefox --firefox-profile ~/.mozilla/firefox/xxxx.default-release

# Check current auth status
python nlm_login.py --check

# Show detected browser options
python nlm_login.py --list-browsers
```

After NotebookLM home loads in the browser, return to terminal and press Enter. Cookies are saved automatically.

> If Firefox profile auto-detection fails, `nlm-login` can still fall back to a temporary profile. Use `--firefox-profile` for explicit control.

## Step 2: Setup

```bash
git clone https://github.com/superdoccimo/notebooklm-tui.git
cd notebooklm-tui
```

Run directly (no install):

```bash
python nlm_backup.py --list
```

Optional install as commands:

```bash
pip install .
# provides: nlm-backup, nlm-upload, nlm-login, nlm-tui, nlm-tui-en, nlm-tui-curses, nlm-canary
```


## Release Canary

Before publishing a release, run the live canary against a real notebook that contains representative Studio artifacts.

```bash
# Read-only: inventory whatever is present
nlm-canary --notebook-id <notebook-id> --profile inventory

# Release gate: require successful export of the current compatibility surface
nlm-canary --notebook-id <notebook-id> --profile release
```

The `release` profile requires successful export of Audio Overview, Video Overview, Slide Deck, Report, Data Table, Flashcards, Quiz, Interactive Mind Map, Infographic, and an Interactive Learning Overview-shaped report payload. The canary writes local backup files plus `canary-report.json`; it does not modify the target notebook.

For Short Video Overview specifically, prepare the release-canary notebook so its video artifact is a Short. The backup path is media-format agnostic, so the canary verifies that the actual video artifact can be exported even though the current list row does not expose a reliable human-readable short/explainer label.

To verify current write/read wrappers separately:

```bash
# Explicitly destructive only to a notebook created by this command.
# It creates one disposable notebook, adds a pasted-text source,
# verifies readback + backup, and deletes that same notebook in finally.
nlm-canary --write-smoke

# Optional URL-source path as part of the same disposable smoke
nlm-canary --write-smoke --smoke-url https://example.com
```

Exit code is `0` on pass, `2` on a completed canary with missing/failed coverage, `3` for authentication failure, and `1` for other errors. GitHub Release publication should remain a separate human-approved step after the live report passes.

## Usage: `nlm-backup` (Download)

```bash
# List notebooks
nlm-backup --list

# Select from list and download interactively
nlm-backup --list --download

# Backup one notebook by ID
nlm-backup <notebook-id>

# Backup all notebooks
nlm-backup --all

# Set output directory
nlm-backup --all -o ~/notebooklm-backup

# Specify cookie file explicitly
nlm-backup --list --cookies /path/to/cookies.json
```

> If you did not run `pip install .`, use `python nlm_backup.py` instead.

## Usage: `nlm-upload` (Upload)

```bash
# Create a new notebook and upload files
nlm-upload "My Research" paper.pdf notes.md image.png

# Upload all files from a folder
nlm-upload "Project Docs" ./my_folder/

# Add files to an existing notebook
nlm-upload --to <notebook-id> new_document.pdf

# Add web sources by URL
nlm-upload "Web Research" --url https://example.com --url https://example2.com

# Restore from backup folder (creates a new notebook)
nlm-upload --restore ./downloads/My_Notebook/

# Show supported file types
nlm-upload --types
```

> If you did not run `pip install .`, use `python nlm_upload.py` instead.

## Restore Semantics

Backup schema v2 stores restore-safe sidecars under `sources/_metadata/`, `notes/_metadata/`, and `mindmaps/_metadata/`. Signed/capability download URLs are deliberately **not** persisted in those sidecars.

`nlm-upload --restore` restores only representations it can identify without pretending that a different object is the original:

| Backup item | Restore behavior |
|---|---|
| Web / YouTube source with canonical URL | re-add the URL |
| Source with an original downloaded file | upload that original file |
| Pasted text / Markdown fallback | restore as a text source |
| Native Note | recreate as a native Gemini Notebook Note |
| Note-backed mind map JSON | recreate as a native JSON-backed Note/mind map |
| Image with only a rendered fallback | restore the rendered image and report it as degraded |
| PDF with only rendered page images | keep the page images local; **do not** upload each page as a separate image source |
| Extracted content whose original source representation is unavailable | optionally restore preserved text, but mark it `DEGRADED` |
| Studio artifacts (Audio/Video/Report/Quiz/etc.) | keep the exact downloaded/local evidence; **do not regenerate**, because regeneration would create new AI output rather than restore the original |

Every restore writes `restore-report-<new-notebook-id>.json` with `restored`, `degraded`, `preserved_only`, and `failed` outcomes. The terminal prints `COMPLETE WITH LIMITATIONS` whenever preserved/degraded data remains.

Legacy backups without sidecars are handled conservatively. The restore path reads only top-level files in `sources/`; it never recursively uploads old PDF page-image directories.

## Usage: `nlm-tui` / `nlm-tui-en` (Terminal UI)

```bash
# Start Japanese UI
nlm-tui

# Start English UI
nlm-tui-en

# Set output directory
nlm-tui -o ~/notebooklm-backup

# Specify cookie file
nlm-tui --cookies /path/to/cookies.json

# Write logs to file
nlm-tui --log ./nlm_tui.log
```

> If you did not run `pip install .`, use `python nlm_tui.py` instead.
> For English UI without install, use `python nlm_tui_en.py`.
> `nlm-tui` works on interactive terminals in Windows and Linux and is the recommended TUI on Windows/Python 3.14.
> In upload menu (`u`), you can pass folder paths and upload multiple entries separated by `;`.

## Usage: `nlm_tui_curses.py` (Flicker-Reduced TUI, Experimental)

This optional variant uses `curses` screen rendering to reduce terminal flicker compared to clear/redraw loops.

```bash
# Start curses UI
python nlm_tui_curses.py

# Set output directory
python nlm_tui_curses.py -o ~/notebooklm-backup

# Specify cookie file
python nlm_tui_curses.py --cookies /path/to/cookies.json

# Write logs to file
python nlm_tui_curses.py --log ./nlm_tui_curses.log
```

When installed with `pip install .`, this variant is also available as `nlm-tui-curses`.
Use `nlm-tui` / `nlm-tui-en` as the default choice on Windows/Python 3.14.

Flashcards and Quiz artifacts are exported in three files:

- `.md` for human-readable backup
- `.html` for the original NotebookLM artifact payload
- `.json` for parsed structured data

Windows notes:

- Some Python builds do not include `_curses` (for example, `python 3.14` in this environment raised `ModuleNotFoundError: No module named '_curses'`).
- If your environment allows package installation, install `windows-curses`.
- If package installation is restricted, run with a Python version/build that already supports curses. Example that worked here:

```bash
~/.pyenv/pyenv-win/versions/3.12.0/python.exe nlm_tui_curses.py
```

If `nlm_tui_curses.py` cannot run in your environment, or you are on Python 3.14 without `windows-curses`, use `python nlm_tui.py` or `python nlm_tui_en.py`.

### TUI Key Bindings

| Key | Action |
|---|---|
| `Up` / `Down` (`j` / `k`) | Move notebook cursor |
| `Space` | Select/unselect notebook |
| `Enter` | Open tree view (sources/artifacts/notes) |
| `b` | Backup selected notebooks (or current row if none selected) |
| `u` | Upload menu (new notebook or append to current notebook) |
| `x` | Retry only failed items from the last backup |
| `f` | Toggle backup targets (Sources/Artifacts/Notes/Mindmaps) |
| `a` | Select all / clear all |
| `r` | Refresh notebook list |
| `q` | Quit (or go back from details view) |

### Supported File Types

These are the file extensions accepted by `nlm-upload`. NotebookLM's officially documented source support currently includes PDF, DOCX, TXT, Markdown, CSV, PPTX, EPUB, images, audio/transcription files, web URLs, YouTube URLs, and Google Drive files. Best-effort entries may fail if NotebookLM rejects them.

| Category | Extensions |
|---|---|
| Documents | `.pdf` `.docx` `.pptx` `.epub` |
| Text / data pasted as text sources | `.txt` `.md` `.csv` `.tsv` `.json` `.xml` `.html` `.htm` |
| Audio / transcription containers | `.3g2` `.3gp` `.aac` `.aif` `.aifc` `.aiff` `.amr` `.au` `.avi` `.cda` `.m4a` `.mid` `.mp3` `.mp4` `.mpeg` `.ogg` `.opus` `.ra` `.ram` `.snd` `.wav` `.wma` |
| Images | `.avif` `.bmp` `.gif` `.heic` `.heif` `.ico` `.jp2` `.jpe` `.jpeg` `.jpg` `.png` `.tif` `.tiff` `.webp` |
| Best-effort legacy/media uploads | `.doc` `.ppt` `.xls` `.xlsx` `.flac` `.mov` `.mkv` `.webm` |

## Output Structure

```text
downloads/
└── <Notebook Title>/
    ├── metadata.json          # notebook metadata (id/title/updated time)
    ├── sources/               # user uploaded sources
    │   ├── _metadata/          # restore-safe source type/URL/file mapping
    │   ├── document.md        # extracted text / web content
    │   ├── research.docx      # original uploaded file when direct download is available
    │   ├── recording.m4a      # original media when available
    │   ├── paper.pdf          # original PDF when available
    │   └── report/            # fallback rendered PDF pages when original is unavailable
    │       ├── page1.png
    │       ├── page2.png
    │       └── ...
    ├── artifacts/             # generated by Gemini Notebook / NotebookLM
    │   ├── _raw/               # raw Studio payload snapshots for future re-parsing
    │   ├── audio_overview.m4a
    │   ├── slide_deck.pdf
    │   ├── report.md
    │   ├── flashcards.md
    │   ├── flashcards.html
    │   ├── flashcards.json
    │   ├── quiz.md
    │   ├── quiz.html
    │   ├── quiz.json
    │   └── ...
    ├── notes/                 # user-authored notes
    │   ├── _metadata/          # native Note restore mapping
    │   └── my_note.md
    └── mindmaps/
        ├── _metadata/          # note-backed mind-map restore mapping
        ├── my_map.json
        └── my_map.md
```

## What Gets Downloaded

### Sources (user uploaded)

| Type | Format |
|---|---|
| Uploaded file with direct-download URL | original file/extension when available |
| Text / Markdown | `.md` fallback |
| Website / URL / YouTube | `.md` extracted-content fallback |
| Image | original file when available; rendered image fallback |
| PDF | original `.pdf` when available; page-image fallback otherwise |

### Artifacts (generated by Gemini Notebook / NotebookLM)

Every Studio artifact also gets a raw JSON snapshot under `artifacts/_raw/`. This is a forward-compatibility safety net for newly introduced or changed internal schemas.

| Type | Format |
|---|---|
| Audio Overview | `.m4a` |
| Video Overview | `.mp4` |
| Slide Deck | `.pdf` + `.pptx` when available |
| Report | `.md` |
| Data Table | `.csv` |
| Flashcards | `.md` + `.html` + `.json` |
| Quiz | `.md` + `.html` + `.json` |
| Interactive Mind Map | `.md` + `.json` |
| Infographic | `.png` |

### Notes

| Type | Format |
|---|---|
| User notes | `.md` |

## Architecture

This project talks directly to Gemini Notebook / NotebookLM internal `batchexecute` endpoints. RPC/auth defaults to `https://notebook.google.com` and can be switched with `NOTEBOOKLM_BASE_URL`. File-upload session start stays on the live-observed consumer upload host `https://notebooklm.google.com` by default; `NOTEBOOKLM_UPLOAD_BASE_URL` can opt into `https://notebook.google.com` for account cohorts where that upload endpoint is known to work.

```text
nlm_login.py            <- auth helper (Chromium via CDP / Firefox via cookies DB)
notebooklm_client.py    <- API client (batchexecute RPC)
├── nlm_backup.py       <- backup tool
├── nlm_upload.py       <- upload/restore tool
├── nlm_tui.py          <- TUI notebook browser + batch backup (Japanese UI)
├── nlm_tui_en.py       <- TUI notebook browser + batch backup (English UI)
└── nlm_tui_curses.py   <- Curses-based TUI notebook browser + batch backup (experimental)
```

- Core tools keep zero third-party runtime dependencies (`urllib`, `http.cookiejar`, etc.)
- Browser support: Edge, Chrome, Brave, Firefox
- Transport: batchexecute RPC over HTTPS

## Troubleshooting

### `Authentication expired`

Your session cookies are expired. Re-run login:

```bash
python nlm_login.py
```

### Why did a PDF fall back to page images?

The backup now prefers the source row's direct original-file URL. If Gemini Notebook does not expose that URL, or the original-file download fails, the tool falls back to the rendered page images returned by source content. This keeps the backup useful without pretending an original binary was recovered when it was not.

### `ModuleNotFoundError: No module named '_curses'` on Windows

Your current Python build does not provide curses bindings.
For Windows/Python 3.14, the default recommendation is to use `nlm_tui.py` / `nlm_tui_en.py`.

Try one of:

```bash
pip install windows-curses
```

or run the curses TUI with a Python build/version where curses works (for example):

```bash
~/.pyenv/pyenv-win/versions/3.12.0/python.exe nlm_tui_curses.py
```

If neither is possible, or you cannot install `windows-curses`, use `python nlm_tui.py` / `python nlm_tui_en.py`.

## License

MIT
