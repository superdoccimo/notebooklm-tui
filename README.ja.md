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

> **2026年10月 Gemini Notebook 互換対応:** `notebook.google.com`（および `notebooklm.google.com` 互換）、現行request wrapper、**Interactive Learning Overview**（埋め込みスライド・クイズ・フラッシュカード・マインドマップ・関連アーティファクトIDを含む `.md` + `.html` + `.json` 出力）、**Slide Deck / Video Overview 共同制作（Co-Creation）メタデータ**（`pending_review` 状態の Review outline notes / Review producer notes を `.review.md` + `.review.json` として保存）、**Video Overviews**（`explainer` / `short` / `cinematic` 識別とメタデータ）、**新しい生成ファイル形式（`type_code=10`）**（`PDF`, `DOCX`, `MD`, `TXT`, `XLSX`, `PPTX`, `CSV`, `JSON`, `PNG`, `JPG`, `GIF`, `SVG` の識別・保存および抽出テキストのバイナリ偽装防止）、拡張 **Quiz / Flashcards**、および `artifacts/_raw/`・`artifacts/_metadata/` における署名付きURL・認証情報の自動マスクに対応しています。

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

以下は `nlm-upload` の入力ポリシーです。`.txt` / `.md` / `.csv` は pasted text へ変換せず、Gemini Notebook のネイティブファイルsourceとして送ります。

| カテゴリ | 拡張子 / 動作 |
|---------|--------|
| ネイティブ文書・テキスト・データ | `.pdf` `.docx` `.pptx` `.epub` `.txt` `.md` `.markdown` `.csv` |
| 音声/文字起こしコンテナ | `.3g2` `.3gp` `.aac` `.aif` `.aifc` `.aiff` `.amr` `.au` `.avi` `.cda` `.m4a` `.mid` `.mp3` `.mp4` `.mpeg` `.ogg` `.opus` `.ra` `.ram` `.snd` `.wav` `.wma` |
| 画像 | `.avif` `.bmp` `.gif` `.heic` `.heif` `.ico` `.jp2` `.jpe` `.jpeg` `.jpg` `.png` `.tif` `.tiff` `.webp` |
| 明示的に貼り付けテキストへ変換 | `.tsv` `.json` `.xml` `.html` `.htm` |
| 標準では拒否。変換してから再試行 | `.doc` `.ppt` `.xls` `.xlsx` `.flac` `.mov` `.mkv` `.webm` |

空ファイルと200 MiBを超えるファイルは、upload RPCを送る前にローカルで拒否します。

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

すべてのStudio artifactについて、`artifacts/_metadata/` にrestore-safeなメタデータsidecarを、`artifacts/_raw/` に署名付きURLや認証トークンをマスクした生JSON snapshotを保存します。

| Type | Format | バックアップ・復元区分 |
|------|--------|------------------------|
| Audio Overview | `.m4a` | バックアップ可能、元形式への復元不可 |
| Video Overview (`explainer` / `short` / `cinematic`) | `.mp4` + 形式/言語/演出メタデータ | バックアップ可能、元形式への復元不可 |
| Slide Deck | `.pdf` + 利用可能な場合は `.pptx` + 各ページ `.png` | バックアップ可能、元形式への復元不可 |
| 共同制作レビュー状態 (`pending_review` の Slide Deck / Video) | `.review.md` + `.review.json`（outline / producer notes、プロンプト、修正指示） | 一部データのみ保存可能（`Proceed` 前の構成確認情報） |
| Interactive Learning Overview | `.md` + `.html` + `.json`（セクション、埋め込みスライド/クイズ/カード、関連artifact ID） | バックアップ可能、元形式への復元不可 |
| Report | `.md` | バックアップ可能、元形式への復元不可 |
| Data Table | `.csv` | バックアップ可能、元形式への復元不可 |
| Flashcards | `.md` + `.html` + `.json` | バックアップ可能、元形式への復元不可 |
| Quiz（単一選択・複数選択・短答式・穴埋め式） | `.md` + `.html` + `.json` | バックアップ可能、元形式への復元不可 |
| Interactive Mind Map | `.md` + `.json` | バックアップ可能、元形式への復元不可 |
| Infographic | `.png` | バックアップ可能、元形式への復元不可 |
| 生成ファイル (`type_code=10`: `PDF`, `DOCX`, `MD`, `TXT`, `XLSX`, `PPTX`, `CSV`, `JSON`, `PNG`, `JPG`, `GIF`, `SVG`) | 実体取得時は元拡張子で保存。バイナリ実体不在時は `.extracted.txt` に分離 | バックアップ可能、元形式への復元不可（抽出テキストを元バイナリと偽らない） |
| 未知のStudio成果物 (`unknown_*`, `fantasy_map`) | 認証情報を除いた `artifacts/_raw/` JSON + `official_support: false` | raw JSONのみ保存可能 |

### 2026年10月時点 バックアップ・復元対応区分表（6区分）

「ローカルに保存できること」と「Google側に元どおり復元できること」を厳密に分け、以下の6区分で管理・表示します。

| 区分 | 定義 | 対象データ |
|------|------|------------|
| **1. 完全にバックアップ・復元可能** (`fully_supported`) | ローカル保存でき、Gemini Notebook 側へ元の意味を保ったまま復元可能 | Web / YouTube URLソース、原本ファイルを取得できたソース（`PDF`, `DOCX`, `PPTX`, `EPUB`, `TXT`, `MD`, `CSV`, 画像, 音声）、貼り付けテキスト/Markdownソース、Native Note、note-backed Mind Map |
| **2. バックアップ可能、元形式への復元不可** (`backup_only_no_server_restore`) | 意味のある形式（バイナリ原本または `.md`+`.html`+`.json`）で保存可能だが、**再生成は別出力になるため復元時に再生成しない** | 完成済み Studio 成果物（Audio Overview、Video Overview `explainer`/`short`/`cinematic`、Slide Deck `.pdf`/`.pptx`、Interactive Learning Overview、Report、Quiz、Flashcards、Interactive Mind Map、Data Table、Infographic、実体取得済み生成ファイル）、ページ画像のみ取得できたPDF |
| **3. 一部データのみ保存可能** (`partial_data_only`) | 原本バイナリや完成前成果物のうち、抽出テキスト・レンダリング画像・レビュー構成案のみ保存可能 | 原本バイナリが非公開で抽出テキスト/レンダリング画像のみ取得できたソース（復元時は `DEGRADED` と明記）、`pending_review` 状態の Slide Deck / Video Overview（`.review.md` + `.review.json`）、バイナリ実体が取得できず抽出テキストのみ分離保存した生成ファイル（`.extracted.txt`） |
| **4. raw JSONのみ保存可能** (`raw_json_only`) | 認証情報を除いた生JSON構造のみを `artifacts/_raw/` に保全し、正式未対応（`official_support: false`）として記録 | 未知の `unknown_*` Studio アーティファクト、`fantasy_map`、レビュー情報を持たない `pending`/`in_progress` 状態のアーティファクト |
| **5. 利用環境で未確認** (`unverified_in_env`) | 自動テスト・フィクスチャで検証済みだが、アカウント・プラン（AI Ultra等）・地域・Cookie期限により実機レスポンス未採取 | 2026年10月19日公開の共同制作（Review outline notes / Review producer notes）の実機RPC行、Cinematic Video Overview、クラウドコンピュータコード実行による生成ファイル（`type_code=10`）の実機取得 |
| **6. 非対応** (`unsupported`) | ノートブックの永続データとしてエクスポート不可、またはツールのスコープ外 | リアルタイム音声対話セッション自体、共同制作の `Proceed` 実行やレビュー指示送信の自動操作RPC、ノートブックへインポートされていない Gemini アプリ側のチャット履歴 |

## 変更履歴 (Changelog)

### v1.2.0-dev (2026-10)
- **P0 修正**: `nlm_tui.py` / `nlm_tui_en.py` / `nlm_tui_curses.py` が `TEXT_EXTENSIONS` のimportエラーで起動不能になっていた問題を修正し、TUIからのファイルアップロード判定を `nlm_upload.py` と統一。
- **P0 修正**: `nlm_login.py` の Firefox 起動URL・SQLite 抽出条件・CDP タブ検出を `notebook.google.com` / `notebooklm.google.com` 両対応に統一。
- **P0 セキュリティ**: `artifacts/_raw/` および `artifacts/_metadata/` 保存時に `googleusercontent.com` 等の署名付きURL・クエリトークン・Cookie文字列を自動マスクする `sanitize_for_backup` を追加。
- **P1 Interactive Learning Overview**: `type_code=2`（HTML/構造化データ付きレポート）および `type_code=11`（`guided_view`）を `.md` + `.html` + `.json` として意味のある構造（セクション、埋め込みスライド・クイズ・フラッシュカード・マインドマップ、引用、関連アーティファクトID）で保存。
- **P1 共同制作（Co-Creation）メタデータ**: Slide Deck の Review outline notes および Video Overview の Review producer notes・プロンプト・修正指示を抽出し、`pending_review`（`status_code=6`）でも `.review.md` + `.review.json` として保存。
- **P2 生成ファイル形式（`type_code=10`）**: `PDF`, `DOCX`, `MD`, `TXT`, `XLSX`, `PPTX`, `CSV`, `JSON`, `PNG`, `JPG`, `GIF`, `SVG` の識別・保存に対応し、バイナリ実体未取得時に抽出テキストを元ファイルと偽装しないガードを実装。
- **P2 Quiz / Flashcards / Video Overviews**: Video Overview の `explainer` / `short` / `cinematic` 識別、クイズ・カードの画像URL・学習トピック・採点フィードバック表示、`nlm-canary` と `build_restore_plan` の拡張。

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
