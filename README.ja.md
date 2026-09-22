# notebooklm-tui

**Gemini Notebook（旧 NotebookLM）にはワークスペース全体の公式バックアップがありません。このツールでノートブックデータをエクスポートできます。**

> このツールが役に立ったら、ぜひスターをお願いします。

Gemini Notebook（旧 NotebookLM）をバックアップし、サーバー側に忠実な書き戻し経路があるデータを意味を保ってリストアする CLI/TUI ツールです。

- ソースをバックアップし、direct downloadがあるアップロードファイルは原本を優先保存。ない場合は抽出テキスト/レンダリング内容へfallback
- 生成コンテンツをエクスポート（音声・動画・スライド＋PPTX・レポート・インフォグラフィック・データテーブル・マインドマップ）
- 新しいQuiz形式にも耐えるschema-tolerantなフラッシュカード・クイズ保存
- Studio artifactごとの生JSON snapshotを保存し、将来の形式追加でもpayloadを捨てない
- ノートをバックアップ
- URL・原本ファイル・Native Note・note-backed Mind Mapを意味を保って復元し、fallback画像を別sourceへ勝手に変換しない
- 外部Pythonパッケージ不要

> **2026年9月互換対応:** 現在の `notebook.google.com` を既定hostにし、notebook作成/取得、URL・text source追加、file登録を現行request wrapperへ更新しました。Quizはshort answer / multiple select / fill in the blank等の複数schemaを許容し、Interactive Mind MapはMarkdown + JSONで保存します。CLI/TUI 3種は同じartifact exporterを使い、全Studio artifactの生payloadも `artifacts/_raw/` に保存します。Interactive Learning Overviewはartifact rowにHTML/structured payloadが現れた場合に保全しますが、まだ実payloadを観測していない形式の完全再現は保証しません。

- **nlm-login** — ブラウザから認証クッキーを自動取得（Edge/Chrome/Brave/Firefox対応）
- **nlm-backup** — ソース・アーティファクト・ノートを一括ダウンロード
- **nlm-upload** — ファイルやURLを一括アップロード、backup sidecarに基づく意味保存リストア
- **nlm-tui** — 日本語UIのTUIで選択・閲覧・一括バックアップ
- **nlm-tui-en** — 英語UIのTUIで選択・閲覧・一括バックアップ
- **nlm-tui-curses** — 任意利用の curses ベースちらつき抑制TUI（実験的・Windowsでは追加セットアップが必要な場合あり）
- **nlm-canary** — Release前に実Notebookでbackup互換性を検証するlive canary

**コアCLIと標準TUIは外部パッケージ依存ゼロ** — Python 標準ライブラリのみで動作します。  
Windows / Python 3.14 では `nlm_tui.py` / `nlm_tui_en.py` の利用を推奨します。`nlm_tui_curses.py` は環境によって追加セットアップが必要です（後述）。

## 動画・記事

- 動画（日本語）: https://youtu.be/yhRBLfuogvs
- 記事: https://minokamo.tokyo/2026/02/18/9671/

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

# backupから意味を保てる範囲を復元
python nlm_upload.py --restore ./downloads/My_Notebook/

# sourceがreadyになるまでの最大待ち時間を調整
python nlm_upload.py --restore ./downloads/My_Notebook/ --wait-timeout 300
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
# → nlm-backup, nlm-upload, nlm-login, nlm-tui, nlm-tui-en, nlm-tui-curses, nlm-canary コマンドが使えるようになる
```


## Release前 Live Canary

Releaseを公開する前に、代表的なStudio artifactを入れた実Notebookに対してcanaryを実行します。

```bash
# 読み取り専用: 現在入っているものを棚卸し
nlm-canary --notebook-id <notebook-id> --profile inventory

# Release gate: 現在の主要互換対象が実際に保存できることを要求
nlm-canary --notebook-id <notebook-id> --profile release
```

`release` profileでは Audio Overview、Video Overview、Slide Deck、Report、Data Table、Flashcards、Quiz、Interactive Mind Map、Infographic、Interactive Learning Overview型のreport payloadについて、completed artifactのexport成功を確認します。対象Notebookは変更せず、ローカルbackupと `canary-report.json` だけを生成します。

Short Video Overviewを確認する場合は、release-canary用NotebookのVideo artifact自体をShortで作っておきます。現在のlist rowにはShort/Explainerを安定して識別できる人間向けlabelがないため、canaryはその実video artifactをmediaとして正常exportできることを確認します。

write/read wrapperのlive確認は別モードです。

```bash
# このコマンド自身が作ったdisposable notebookだけを書き込み・削除します。
# pasted-text追加 → readback → backup → finallyで同じNotebookを削除。
nlm-canary --write-smoke

# URL source経路も同時に確認
nlm-canary --write-smoke --smoke-url https://example.com
```

終了コードは、PASS=`0`、coverage不足/保存失敗=`2`、認証失敗=`3`、その他のエラー=`1` です。GitHub Release公開はlive reportがPASSした後の、人間承認が必要な別操作として残します。

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

## リストアの意味

backup schema v2では `sources/_metadata/`、`notes/_metadata/`、`mindmaps/_metadata/` にrestore用sidecarを保存します。期限付き・capability付きのdownload URLはsidecarへ保存しません。

実際にNotebookを作る前に、ローカルだけでrestore計画を確認できます。

```bash
# Google認証不要、Notebook作成なし、ネットワークへのwriteなし
nlm-upload --restore ./downloads/My_Notebook/ --dry-run
```

`restore-plan.json` を生成し、各項目が `restored` / `degraded` / `preserved_only` / preflight失敗のどれになるかを先に表示します。

`nlm-upload --restore` は、元と同じ意味で戻せるものと、戻せないものを区別します。

| Backup内容 | Restore動作 |
|---|---|
| canonical URLがあるWeb / YouTube source | URLとして再追加 |
| 原本ファイルを取得できたsource | その原本をupload |
| Pasted text / Markdown fallback | text sourceとして復元 |
| Native Note | Gemini NotebookのNative Noteとして再作成 |
| note-backed Mind Map JSON | JSON-backed Note / Mind Mapとして再作成 |
| 原本がなくrendered imageだけの画像 | rendered imageを戻し、`DEGRADED` と記録 |
| 原本PDFがなくページ画像だけ | ローカル保全のみ。**各ページを別々の画像sourceとしてuploadしない** |
| 元形式がなく抽出テキストだけ残ったsource | 内容はtext sourceとして戻せるが `DEGRADED` と記録 |
| Studio artifacts（Audio/Video/Report/Quiz等） | ダウンロード済み証拠をローカル保全。**再生成しない**。再生成は元artifactの復元ではなく新しいAI出力になるため |

restore後には `restore-report-<new-notebook-id>.json` を出力し、各項目を `restored` / `degraded` / `preserved_only` / `failed` で記録します。制約が残る場合、端末表示も `COMPLETE WITH LIMITATIONS` とし、完全復元したようには表示しません。

sidecarがない旧backupは保守的に扱います。`sources/` 直下のファイルだけを対象にし、旧PDFのページ画像directoryを再帰的にuploadすることはありません。

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

`pip install .` した場合、このバリアントは `nlm-tui-curses` コマンドとしても利用できます。
Windows / Python 3.14 では、まず `nlm-tui` / `nlm-tui-en` を使ってください。

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

以下は `nlm-upload` が受け付ける拡張子です。NotebookLM の公開ヘルプ上で確認できる公式 source 対応は、PDF、DOCX、TXT、Markdown、CSV、PPTX、EPUB、画像、音声/文字起こしファイル、Web URL、YouTube URL、Google Drive ファイルです。best-effort の項目は NotebookLM 側で拒否される場合があります。

| カテゴリ | 拡張子 |
|---------|--------|
| ドキュメント | `.pdf` `.docx` `.pptx` `.epub` |
| テキスト/データ（貼り付けソース） | `.txt` `.md` `.csv` `.tsv` `.json` `.xml` `.html` `.htm` |
| 音声/文字起こしコンテナ | `.3g2` `.3gp` `.aac` `.aif` `.aifc` `.aiff` `.amr` `.au` `.avi` `.cda` `.m4a` `.mid` `.mp3` `.mp4` `.mpeg` `.ogg` `.opus` `.ra` `.ram` `.snd` `.wav` `.wma` |
| 画像 | `.avif` `.bmp` `.gif` `.heic` `.heif` `.ico` `.jp2` `.jpe` `.jpeg` `.jpg` `.png` `.tif` `.tiff` `.webp` |
| 互換目的の best-effort | `.doc` `.ppt` `.xls` `.xlsx` `.flac` `.mov` `.mkv` `.webm` |

## Output Structure

```
downloads/
└── <Notebook Title>/
    ├── metadata.json          # ノートブック情報（ID、タイトル、更新日時）
    ├── sources/               # アップロードしたソース
    │   ├── _metadata/          # source type/URL/fileのrestore-safe mapping
    │   ├── document.md        # 抽出テキスト / Web内容
    │   ├── research.docx      # direct downloadがある場合はアップロード原本
    │   ├── recording.m4a      # 利用可能な場合は音声原本
    │   ├── paper.pdf          # 利用可能な場合はPDF原本
    │   └── report/            # 原本を取れないPDFのfallbackページ画像
    │       ├── page1.png
    │       ├── page2.png
    │       └── ...
    ├── artifacts/             # Gemini Notebook / NotebookLM が生成したもの
    │   ├── _raw/               # 将来の再解析用 Studio payload snapshot
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
    ├── notes/                 # ユーザーが作成したNative Note
    │   ├── _metadata/          # Native Note restore mapping
    │   └── my_note.md
    └── mindmaps/
        ├── _metadata/          # note-backed Mind Map restore mapping
        ├── my_map.json
        └── my_map.md
```

## What Gets Downloaded

### Sources (自分がアップロードしたもの)
| Type | Format |
|------|--------|
| direct download URLがあるアップロードファイル | 利用可能なら元の拡張子の原本 |
| Text / Markdown | fallback時は `.md` |
| Website / URL / YouTube | fallback時は抽出内容を `.md` |
| Image | 利用可能なら原本、なければレンダリング画像 |
| PDF | 利用可能なら元の `.pdf`、なければページ画像 |

### Artifacts (Gemini Notebook / NotebookLM が生成したもの)

すべてのStudio artifactについて `artifacts/_raw/` に生JSON snapshotも保存します。内部schemaが追加・変更されたときに、後からparserを更新して再解析するための保全データです。
| Type | Format |
|------|--------|
| Audio Overview | `.m4a` |
| Video Overview | `.mp4` |
| Slide Deck | `.pdf` + 利用可能な場合は `.pptx` |
| Report | `.md` |
| Data Table | `.csv` |
| Flashcards | `.md` + `.html` + `.json` |
| Quiz | `.md` + `.html` + `.json` |
| Interactive Mind Map | `.md` + `.json` |
| Infographic | `.png` |

### Notes
| Type | Format |
|------|--------|
| User notes | `.md` |

## Architecture

このツールは Gemini Notebook / NotebookLM の内部 `batchexecute` API を直接操作します。RPC/認証の既定hostは `https://notebook.google.com` で、`NOTEBOOKLM_BASE_URL` で切り替えられます。一方、ファイルuploadのsession開始は実運用で確認されている `https://notebooklm.google.com` を既定に残し、rebrand側のupload endpointが使えるaccount cohortでは `NOTEBOOKLM_UPLOAD_BASE_URL=https://notebook.google.com` で切り替えられます。

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

### PDF がページ画像へfallbackする

現在はsource rowにdirect download URLがある場合、元のPDFを優先して保存します。Gemini Notebook側がそのURLを返さない場合、または原本ダウンロードに失敗した場合だけ、source contentから取得できるページ画像へfallbackします。原本を取得できていないのに取得済みと扱うことはしません。

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
