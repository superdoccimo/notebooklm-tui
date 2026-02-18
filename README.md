# notebooklm-tui

[Japanese README](README.ja.md)

A zero-dependency NotebookLM backup and restore toolkit for CLI and terminal UI workflows.

- `nlm-login`: Get NotebookLM auth cookies from your browser (Edge/Chrome/Brave/Firefox)
- `nlm-backup`: Download sources, artifacts, and notes
- `nlm-upload`: Upload files/URLs and restore from backup folders
- `nlm-tui`: Japanese UI terminal TUI
- `nlm-tui-en`: English UI terminal TUI

All tools run on Python standard library only (no third-party packages required).

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

# Restore from a previous backup folder
python nlm_upload.py --restore ./downloads/My_Notebook/
```

TUI example:

```bash
# Interactive notebook browser + backup (Japanese UI)
python nlm_tui.py

# Interactive notebook browser + backup (English UI)
python nlm_tui_en.py
```

## Prerequisites

| Requirement | Check command | Notes |
|---|---|---|
| Python 3.10+ | `python --version` | [python.org](https://www.python.org/downloads/) |
| One browser | - | Edge / Chrome / Brave / Firefox |
| Google account | - | Must have access to NotebookLM |

No extra package installation is required.

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
# provides: nlm-backup, nlm-upload, nlm-login, nlm-tui, nlm-tui-en
```

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
> `nlm-tui` works on interactive terminals in Windows and Linux.
> In upload menu (`u`), you can pass folder paths and upload multiple entries separated by `;`.

### TUI Key Bindings

| Key | Action |
|---|---|
| `Up` / `Down` (`j` / `k`) | Move notebook cursor |
| `Space` | Select/unselect notebook |
| `Enter` | Open tree view (sources/artifacts/notes) |
| `b` | Backup selected notebooks (or current row if none selected) |
| `u` | Upload menu (new notebook or append to current notebook) |
| `x` | Retry only failed items from the last backup |
| `f` | Toggle backup targets (Sources/Artifacts/Notes) |
| `a` | Select all / clear all |
| `r` | Refresh notebook list |
| `q` | Quit (or go back from details view) |

### Supported File Types

| Category | Extensions |
|---|---|
| Document | `.pdf` `.txt` `.md` `.doc` `.docx` `.ppt` `.pptx` `.xls` `.xlsx` |
| Data | `.csv` `.tsv` `.json` `.xml` |
| Web | `.html` `.htm` |
| Audio | `.mp3` `.wav` `.m4a` `.ogg` `.flac` |
| Video | `.mp4` `.mov` `.avi` `.mkv` `.webm` |
| Image | `.png` `.jpg` `.jpeg` `.gif` `.bmp` `.webp` |

## Output Structure

```text
downloads/
└── <Notebook Title>/
    ├── metadata.json          # notebook metadata (id/title/updated time)
    ├── sources/               # user uploaded sources
    │   ├── document.md        # text
    │   ├── photo.png          # image
    │   └── report/            # pdf pages as images
    │       ├── page1.png
    │       ├── page2.png
    │       └── ...
    ├── artifacts/             # generated by NotebookLM
    │   ├── audio_overview.m4a
    │   ├── slide_deck.pdf
    │   ├── report.md
    │   └── ...
    └── notes/                 # user-authored notes
        └── my_note.md
```

## What Gets Downloaded

### Sources (user uploaded)

| Type | Format |
|---|---|
| Text / Markdown | `.md` |
| Website / URL | `.md` (extracted text) |
| Image | `.png` |
| PDF | page images (`.png` per page) |

### Artifacts (generated by NotebookLM)

| Type | Format |
|---|---|
| Audio Overview | `.m4a` |
| Video Overview | `.mp4` |
| Slide Deck | `.pdf` |
| Report | `.md` |
| Data Table | `.csv` |
| Flashcards | `.md` |
| Infographic | `.png` |

### Notes

| Type | Format |
|---|---|
| User notes | `.md` |

## Architecture

This project talks directly to NotebookLM internal `batchexecute` endpoints.

```text
nlm_login.py            <- auth helper (Chromium via CDP / Firefox via cookies DB)
notebooklm_client.py    <- API client (batchexecute RPC)
├── nlm_backup.py       <- backup tool
├── nlm_upload.py       <- upload/restore tool
├── nlm_tui.py          <- TUI notebook browser + batch backup (Japanese UI)
└── nlm_tui_en.py       <- TUI notebook browser + batch backup (English UI)
```

- Zero third-party runtime dependencies (`urllib`, `http.cookiejar`, etc.)
- Browser support: Edge, Chrome, Brave, Firefox
- Transport: batchexecute RPC over HTTPS

## Troubleshooting

### `Authentication expired`

Your session cookies are expired. Re-run login:

```bash
python nlm_login.py
```

### Why are PDFs downloaded as images?

This is expected. NotebookLM stores uploaded PDF content as rendered page images, so backups save PNG pages instead of the original PDF binary.

## License

MIT
