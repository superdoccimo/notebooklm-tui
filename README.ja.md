# notebooklm-tui

NotebookLM のバックアップ＆リストア CLI/TUI ツール。

- **nlm-login** — ブラウザから認証クッキーを自動取得（Edge/Chrome/Brave/Firefox対応）
- **nlm-backup** — ソース・アーティファクト・ノートを一括ダウンロード
- **nlm-upload** — ファイルやURLを一括アップロード、バックアップからの復元
- **nlm-tui** — 日本語UIのTUIで選択・閲覧・一括バックアップ
- **nlm-tui-en** — 英語UIのTUIで選択・閲覧・一括バックアップ
- **nlm-tui-curses**（スクリプト: `nlm_tui_curses.py`）— 任意利用の curses ベースちらつき抑制TUI（実験的）

**コアCLIと標準TUIは外部パッケージ依存ゼロ** — Python 標準ライブラリのみで動作します。  
Windows / Python 3.14 では `nlm_tui.py` / `nlm_tui_en.py` の利用を推奨します。`nlm_tui_curses.py` は環境によって追加セットアップが必要です（後述）。

## Quick Start

```bash
# 1. このリポジトリをクローン
git clone https://github.com/superdoccimo/notebooklm-tui.git
cd notebooklm-tui

# 2. 認証（ブラウザでログインするだけ！）
python nlm_login.py

# 3. ノートブック一覧を確認
python nlm_backup.py --list

# 4. 全ノートブックをバックアップ！
python nlm_backup.py --all
```

アップロードも簡単です：

```bash
# ファイルを新しいノートブックにアップロード
python nlm_upload.py "My Research" paper.pdf notes.md

# バックアップから復元
python nlm_upload.py --restore ./downloads/My_Notebook/
```

TUIで操作する場合：

```bash
# Windows / Python 3.14 ではこちらを推奨
# 日本語UI
python nlm_tui.py

# 英語UI
python nlm_tui_en.py

# curses が使える環境向けのオプション
# curses UI（ちらつき抑制）
python nlm_tui_curses.py
```

## Prerequisites

| 必要なもの | 確認コマンド | 備考 |
|-----------|------------|------|
| Python 3.10+ | `python --version` | [python.org](https://www.python.org/downloads/) |
| Edge / Chrome / Brave / Firefox | - | いずれか1つ |
| Google アカウント | - | NotebookLM を使用中のアカウント |

コアCLI/TUI（`nlm-login`, `nlm-backup`, `nlm-upload`, `nlm-tui`, `nlm-tui-en`）は追加のパッケージインストール不要です。

> Linux では `firefox` / `google-chrome` / `chromium` / `brave-browser` を自動検出します。

## Step 1: 認証（nlm-login）

```bash
# 既定ブラウザ候補でログイン（Windows: Edge優先 / Linux: Firefox優先）
python nlm_login.py

# Chrome を使う場合
python nlm_login.py --browser chrome

# Brave を使う場合
python nlm_login.py --browser brave

# Firefox を使う場合
python nlm_login.py --browser firefox

# Firefox プロファイルを明示する場合（自動検出できないとき）
python nlm_login.py --browser firefox --firefox-profile ~/.mozilla/firefox/xxxx.default-release

# 認証状態の確認
python nlm_login.py --check

# 利用可能なブラウザ一覧
python nlm_login.py --list-browsers
```

実行するとブラウザが開きます。Google アカウントでログインし、NotebookLM のホーム画面が表示されたらターミナルに戻って Enter を押してください。クッキーが自動的に保存されます。

> Firefox で `profile: (未検出)` の場合でも実行時に一時プロファイルでフォールバックします。必要なら `--firefox-profile` で既存プロファイルを明示指定できます。

> **Note:** 認証は数日〜数週間で期限切れになります。`Authentication expired` エラーが出たら `python nlm_login.py` を再実行してください。

## Step 2: ツールのセットアップ

```bash
git clone https://github.com/superdoccimo/notebooklm-tui.git
cd notebooklm-tui
```

**そのまま実行する場合（インストール不要）：**

```bash
python nlm_backup.py --list
```

**コマンドとしてインストールする場合（オプション）：**

```bash
pip install .
# → nlm-backup, nlm-upload, nlm-login, nlm-tui, nlm-tui-en コマンドが使えるようになる
```

## Usage: nlm-backup (ダウンロード)

```bash
# ノートブック一覧を表示
nlm-backup --list

# 一覧から選んでダウンロード（対話式）
nlm-backup --list --download

# ノートブックIDを指定してバックアップ
nlm-backup <notebook-id>

# 全ノートブックを一括バックアップ
nlm-backup --all

# 出力先を指定
nlm-backup --all -o ~/notebooklm-backup

# クッキーファイルを明示的に指定
nlm-backup --list --cookies /path/to/cookies.json
```

> `pip install .` していない場合は `nlm-backup` の代わりに `python nlm_backup.py` を使ってください。

## Usage: nlm-upload (アップロード)

```bash
# 新しいノートブックを作成してファイルをアップロード
nlm-upload "My Research" paper.pdf notes.md image.png

# フォルダ内のファイルをまとめてアップロード
nlm-upload "Project Docs" ./my_folder/

# 既存のノートブックにファイルを追加
nlm-upload --to <notebook-id> new_document.pdf

# URLをソースとして追加
nlm-upload "Web Research" --url https://example.com --url https://example2.com

# バックアップから復元（新しいノートブックが作成される）
nlm-upload --restore ./downloads/My_Notebook/

# 対応ファイル形式を確認
nlm-upload --types
```

> `pip install .` していない場合は `nlm-upload` の代わりに `python nlm_upload.py` を使ってください。

## Usage: nlm-tui / nlm-tui-en (ターミナルUI)

```bash
# 日本語UIを起動
nlm-tui

# 英語UIを起動
nlm-tui-en

# 出力先を指定
nlm-tui -o ~/notebooklm-backup

# クッキーファイルを指定
nlm-tui --cookies /path/to/cookies.json

# ログファイルを指定
nlm-tui --log ./nlm_tui.log
```

> `pip install .` していない場合は `nlm-tui` の代わりに `python nlm_tui.py` を使ってください。
> 英語UIは `python nlm_tui_en.py`（または `nlm-tui-en`）を使ってください。
> `nlm-tui` は Windows / Linux の対話式ターミナルで動作します（標準ライブラリのみ）。Windows / Python 3.14 では標準推奨です。
> `u` キーのアップロードメニューでは、フォルダパスを指定して空ノートブックへ一括投入できます（複数は `;` 区切り）。

## Usage: nlm_tui_curses.py（ちらつき抑制TUI・実験的）

`curses` 描画で画面更新を行い、`clear/redraw` 方式よりちらつきを抑えるための任意バリアントです。

```bash
# curses UIを起動
python nlm_tui_curses.py

# 出力先を指定
python nlm_tui_curses.py -o ~/notebooklm-backup

# クッキーファイルを指定
python nlm_tui_curses.py --cookies /path/to/cookies.json

# ログファイルを指定
python nlm_tui_curses.py --log ./nlm_tui_curses.log
```

`nlm_tui_curses.py` は現状スクリプト実行専用で、`pip install .` しても `nlm-tui-curses` コマンドは追加されません。
Windows / Python 3.14 では、まず `python nlm_tui.py` / `python nlm_tui_en.py` を使ってください。

Flashcards / Quiz は次の 3 ファイルで保存されます。

- `.md`: 人が読むためのバックアップ
- `.html`: NotebookLM が返した元の生成物
- `.json`: 解析済みの構造化データ

Windows での注意点:

- Python ビルドによっては `_curses` が含まれず、`ModuleNotFoundError: No module named '_curses'` が発生します。
- パッケージ導入が可能なら `windows-curses` を導入してください。
- パッケージ導入が難しい場合は、curses が使える Python ビルド/バージョンで実行してください（この環境で確認できた例）:

```bash
~/.pyenv/pyenv-win/versions/3.12.0/python.exe nlm_tui_curses.py
```

実行できない場合、または Python 3.14 で `windows-curses` を導入できない場合は、`python nlm_tui.py` / `python nlm_tui_en.py` を利用してください。

### キー操作

| キー | 動作 |
|------|------|
| `↑` / `↓` (`j` / `k`) | ノートブックを移動 |
| `Space` | ノートブックを選択/解除 |
| `Enter` | ソース/アーティファクト/ノートのツリー表示 |
| `b` | 選択ノートブックを一括バックアップ（未選択時は現在行） |
| `u` | アップロードメニュー（新規作成して投入 / 現在ノートブックへ追加） |
| `x` | 直近バックアップの失敗項目のみ再試行 |
| `f` | バックアップ対象フィルタ（Sources/Artifacts/Notes/Mindmaps） |
| `a` | 全選択/全解除 |
| `r` | 一覧を再読み込み |
| `q` | 終了（詳細画面では戻る） |

### 対応ファイル形式

| カテゴリ | 拡張子 |
|---------|--------|
| ドキュメント | `.pdf` `.txt` `.md` `.doc` `.docx` `.ppt` `.pptx` `.xls` `.xlsx` |
| データ | `.csv` `.tsv` `.json` `.xml` |
| ウェブ | `.html` `.htm` |
| 音声 | `.mp3` `.wav` `.m4a` `.ogg` `.flac` |
| 動画 | `.mp4` `.mov` `.avi` `.mkv` `.webm` |
| 画像 | `.png` `.jpg` `.jpeg` `.gif` `.bmp` `.webp` |

## Output Structure

```
downloads/
└── <Notebook Title>/
    ├── metadata.json          # ノートブック情報（ID、タイトル、更新日時）
    ├── sources/               # アップロードしたソース
    │   ├── document.md        # テキスト
    │   ├── photo.png          # 画像
    │   └── report/            # PDF (ページごとの画像)
    │       ├── page1.png
    │       ├── page2.png
    │       └── ...
    ├── artifacts/             # NotebookLM が生成したもの
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
    └── notes/                 # ユーザーが作成したノート
        └── my_note.md
```

## What Gets Downloaded

### Sources (自分がアップロードしたもの)
| Type | Format |
|------|--------|
| Text / Markdown | `.md` |
| Website / URL | `.md` (extracted text) |
| Image | `.png` |
| PDF | Page images (`.png` per page) |

### Artifacts (NotebookLM が生成したもの)
| Type | Format |
|------|--------|
| Audio Overview | `.m4a` |
| Video Overview | `.mp4` |
| Slide Deck | `.pdf` |
| Report | `.md` |
| Data Table | `.csv` |
| Flashcards | `.md` + `.html` + `.json` |
| Quiz | `.md` + `.html` + `.json` |
| Infographic | `.png` |

### Notes
| Type | Format |
|------|--------|
| User notes | `.md` |

## Architecture

このツールは Google NotebookLM の内部 `batchexecute` API を直接操作します。

```
nlm_login.py            ← 認証ツール（Chromium系: CDP / Firefox: cookies.sqlite）
notebooklm_client.py    ← API クライアント（batchexecute RPC）
├── nlm_backup.py       ← バックアップツール
├── nlm_upload.py       ← アップロード/リストアツール
├── nlm_tui.py          ← 日本語UIのTUIブラウズ/選択バックアップツール
├── nlm_tui_en.py       ← 英語UIのTUIブラウズ/選択バックアップツール
└── nlm_tui_curses.py   ← curses ベースのTUIブラウズ/選択バックアップツール（実験的）
```

- **コア機能は外部パッケージ依存ゼロ**: `requests`, `httpx` 等は不要。`urllib` と `http.cookiejar` のみ使用
- **認証**: Chromium系は CDP、Firefox はプロファイルDBからクッキーを取得
- **ブラウザ対応**: Edge, Chrome, Brave, Firefox（Windowsは Edge 優先、Linux は Firefox 優先）
- **プロトコル**: batchexecute RPC over HTTPS

## Troubleshooting

### `Authentication expired` エラー

認証の有効期限が切れています。再ログインしてください：

```bash
python nlm_login.py
```

### PDF が画像としてダウンロードされる

これは仕様です。NotebookLM はアップロードされた PDF をページごとにレンダリングして画像として保管しています。そのため、元の PDF ファイルではなく、各ページの PNG 画像として取得されます。

### Windows で `ModuleNotFoundError: No module named '_curses'` が出る

現在の Python ビルドに curses バインディングが含まれていません。Windows / Python 3.14 では `nlm_tui.py` / `nlm_tui_en.py` の利用を標準推奨としています。

次のいずれかを試してください:

```bash
pip install windows-curses
```

または、curses が動作する Python ビルド/バージョンで実行:

```bash
~/.pyenv/pyenv-win/versions/3.12.0/python.exe nlm_tui_curses.py
```

上記が難しい場合、または `windows-curses` を導入できない場合は `python nlm_tui.py` / `python nlm_tui_en.py` を使用してください。

## License

MIT
