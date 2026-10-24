import json
import tempfile
import unittest
from pathlib import Path

from nlm_backup import (
    _unique_path,
    mindmap_to_markdown,
    sanitize_filename,
    save_artifact,
    save_artifact_raw_snapshot,
    save_source,
)
from nlm_upload import (
    DOCUMENT_UPLOAD_EXTENSIONS,
    PASTED_TEXT_EXTENSIONS,
    UNVERIFIED_UPLOAD_EXTENSIONS,
    collect_files,
    upload_files,
)
from notebooklm_client import (
    ARTIFACT_STATUS,
    SOURCE_TYPES,
    MAX_UPLOAD_BYTES,
    NotebookLMClient,
    _is_youtube_url,
    _template_block,
    _trusted_upload_origin,
    NotebookLMError,
)


class BackupHelperTests(unittest.TestCase):
    def test_sanitize_filename_replaces_cross_platform_reserved_characters(self):
        self.assertEqual(sanitize_filename(' report:<Q1>? '), "report__Q1__")

    def test_unique_path_increments_without_overwriting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original = Path(temp_dir) / "backup.json"
            original.touch()
            (Path(temp_dir) / "backup_2.json").touch()

            self.assertEqual(_unique_path(original).name, "backup_3.json")

    def test_mindmap_to_markdown_preserves_tree_depth(self):
        tree = {
            "name": "Root",
            "children": [
                {"name": "First"},
                {"name": "Second", "children": [{"name": "Leaf"}]},
            ],
        }

        self.assertEqual(
            mindmap_to_markdown(tree),
            "- Root\n  - First\n  - Second\n    - Leaf",
        )


class WireContractTests(unittest.TestCase):
    def setUp(self):
        self.client = NotebookLMClient.__new__(NotebookLMClient)
        self.calls = []

        def rpc(rpc_id, params, source_path="/"):
            self.calls.append((rpc_id, params, source_path))
            return None

        self.client._batchexecute = rpc

    def test_current_source_type_codes(self):
        self.assertEqual(SOURCE_TYPES[5], "web_page")
        self.assertEqual(SOURCE_TYPES[9], "youtube")
        self.assertEqual(SOURCE_TYPES[11], "docx")
        self.assertEqual(SOURCE_TYPES[16], "csv")
        self.assertEqual(SOURCE_TYPES[17], "epub")

    def test_current_artifact_status_codes(self):
        self.assertEqual(ARTIFACT_STATUS[1], "pending")
        self.assertEqual(ARTIFACT_STATUS[2], "in_progress")
        self.assertEqual(ARTIFACT_STATUS[3], "completed")
        self.assertEqual(ARTIFACT_STATUS[6], "pending_review")

    def test_create_notebook_uses_nested_template_block(self):
        def rpc(rpc_id, params, source_path="/"):
            self.calls.append((rpc_id, params, source_path))
            return [None, None, "new-notebook"]

        self.client._batchexecute = rpc
        notebook_id = self.client.create_notebook("Current")

        self.assertEqual(notebook_id, "new-notebook")
        self.assertEqual(
            self.calls[0],
            ("CCqFvf", ["Current", None, None, _template_block()], "/"),
        )

    def test_get_notebook_uses_nested_template_block(self):
        self.client.list_sources("nb-1")
        self.assertEqual(
            self.calls[0],
            (
                "rLM1Ne",
                ["nb-1", None, _template_block(), None, 0],
                "/notebook/nb-1",
            ),
        )

    def test_text_source_uses_migrated_three_field_wrapper(self):
        def rpc(rpc_id, params, source_path="/"):
            self.calls.append((rpc_id, params, source_path))
            return [[[["source-1"]]]]

        self.client._batchexecute = rpc
        source_id = self.client.add_source_text("nb-1", "Title", "Body")

        self.assertEqual(source_id, "source-1")
        rpc_id, params, path = self.calls[0]
        self.assertEqual(rpc_id, "izAoDd")
        self.assertEqual(path, "/notebook/nb-1")
        self.assertEqual(params[1], "nb-1")
        self.assertEqual(params[2], _template_block())
        self.assertEqual(len(params), 3)
        self.assertEqual(params[0][0][1], ["Title", "Body"])
        self.assertEqual(params[0][0][3], 2)
        self.assertEqual(params[0][0][10], 1)

    def test_url_source_uses_current_web_shape(self):
        def rpc(rpc_id, params, source_path="/"):
            self.calls.append((rpc_id, params, source_path))
            return [[[["source-2"]]]]

        self.client._batchexecute = rpc
        self.client.add_source_url("nb-1", "https://example.com/page")

        _, params, _ = self.calls[0]
        spec = params[0][0]
        self.assertEqual(spec[2], ["https://example.com/page"])
        self.assertEqual(spec[10], 1)
        self.assertEqual(params[2], _template_block())

    def test_youtube_url_uses_youtube_source_slot(self):
        def rpc(rpc_id, params, source_path="/"):
            self.calls.append((rpc_id, params, source_path))
            return [[[["source-3"]]]]

        self.client._batchexecute = rpc
        self.client.add_source_url("nb-1", "https://youtu.be/abc123")

        _, params, _ = self.calls[0]
        spec = params[0][0]
        self.assertIsNone(spec[2])
        self.assertEqual(spec[7], ["https://youtu.be/abc123"])
        self.assertTrue(_is_youtube_url("https://www.youtube.com/watch?v=abc123"))
        self.assertFalse(_is_youtube_url("https://example.com/youtube"))

    def test_get_artifact_uses_single_id_parameter(self):
        self.client.get_artifact("artifact-1")
        self.assertEqual(self.calls[0], ("v9rmvd", ["artifact-1"], "/"))

    def test_resumable_upload_url_is_origin_and_path_bound(self):
        self.assertEqual(
            _trusted_upload_origin(
                "https://notebooklm.google.com/upload/_/?upload_id=session"
            ),
            "https://notebooklm.google.com",
        )
        self.assertEqual(
            _trusted_upload_origin(
                "https://notebook.google.com/upload/_/?upload_id=session"
            ),
            "https://notebook.google.com",
        )
        with self.assertRaises(NotebookLMError):
            _trusted_upload_origin("https://evil.example/upload/_/?upload_id=session")
        with self.assertRaises(NotebookLMError):
            _trusted_upload_origin("https://notebook.google.com/not-upload/?id=session")

    def test_source_row_exposes_original_download_metadata(self):
        meta = [None] * 8
        meta[4] = 11
        meta[7] = ["https://example.com/canonical"]
        row = [None] * 8
        row[0] = ["source-file"]
        row[1] = "report.docx"
        row[2] = meta
        row[5] = "https://download.example/report.docx"
        row[6] = "https://viewer.example/report.docx"
        row[7] = [None, None, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]

        def rpc(rpc_id, params, source_path="/"):
            self.calls.append((rpc_id, params, source_path))
            return [[None, [row]]]

        self.client._batchexecute = rpc
        sources = self.client.list_sources("nb-1")

        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source["type"], "docx")
        self.assertEqual(source["url"], "https://example.com/canonical")
        self.assertEqual(source["download_url"], "https://download.example/report.docx")
        self.assertEqual(source["content_mime"], row[7][2])
        self.assertEqual(source["_raw"], row)


class ArtifactCompatibilityTests(unittest.TestCase):
    def setUp(self):
        # Authentication/network setup is intentionally bypassed for pure helpers.
        self.client = NotebookLMClient.__new__(NotebookLMClient)

    def test_quiz_markdown_supports_multiple_select(self):
        markdown = self.client._render_quiz_markdown(
            "Multi",
            [
                {
                    "question": "Choose two",
                    "questionType": "multiple_select",
                    "answerOptions": [
                        {"text": "A", "isCorrect": True},
                        {"text": "B", "isCorrect": False},
                        {"text": "C", "isCorrect": True},
                    ],
                }
            ],
            "quiz.html",
            "quiz.json",
        )

        self.assertIn("Format: multiple_select", markdown)
        self.assertIn("Correct answers: A, C", markdown)
        self.assertIn("- [x] A", markdown)
        self.assertIn("- [x] C", markdown)

    def test_quiz_markdown_supports_short_answer_without_options(self):
        markdown = self.client._render_quiz_markdown(
            "Short",
            [
                {
                    "prompt": "Name the protocol",
                    "format": "short_answer",
                    "acceptedAnswers": ["HTTPS", "TLS over HTTP"],
                    "explanation": "Either accepted form is preserved.",
                }
            ],
            "quiz.html",
            "quiz.json",
        )

        self.assertIn("Name the protocol", markdown)
        self.assertIn("Format: short_answer", markdown)
        self.assertIn("Correct answers: HTTPS, TLS over HTTP", markdown)
        self.assertIn("Explanation: Either accepted form is preserved.", markdown)

    def test_quiz_markdown_supports_fill_in_the_blank_answer_field(self):
        markdown = self.client._render_quiz_markdown(
            "Blank",
            [
                {
                    "stem": "The sky is ____.",
                    "kind": "fill_in_the_blank",
                    "answer": "blue",
                }
            ],
            "quiz.html",
            "quiz.json",
        )

        self.assertIn("The sky is ____.", markdown)
        self.assertIn("Format: fill_in_the_blank", markdown)
        self.assertIn("Correct answer: blue", markdown)

    def test_report_html_is_preserved_as_app_artifact(self):
        payload = (
            '<html><app-root data-app-data="'
            '{&quot;sections&quot;:[{&quot;title&quot;:&quot;Overview&quot;}]}'
            '"></app-root></html>'
        )
        art = [None] * 8
        art[7] = [payload]

        info = self.client._extract_artifact_download(art, 2)

        self.assertEqual(info["app_html"], payload)
        self.assertEqual(info["app_data"]["sections"][0]["title"], "Overview")

    def test_interactive_mind_map_variant_and_tree_are_exportable(self):
        tree = {
            "name": "Root",
            "children": [{"name": "Branch", "children": [{"name": "Leaf"}]}],
        }
        art = [None] * 10
        art[0] = "map-1"
        art[1] = "Interactive Map"
        art[2] = 4
        art[4] = 3
        art[9] = [None, [4], None, json.dumps(tree)]

        record = self.client._build_artifact_record(art)

        self.assertEqual(record["variant"], "interactive_mind_map")
        self.assertEqual(record["structured_content"], tree)

        with tempfile.TemporaryDirectory() as temp_dir:
            dest = Path(temp_dir) / "interactive.md"
            self.assertTrue(self.client.download_artifact(record, dest))
            markdown = dest.read_text(encoding="utf-8")
            saved_tree = json.loads(dest.with_suffix(".json").read_text(encoding="utf-8"))

        self.assertIn("- Root", markdown)
        self.assertIn("  - Branch", markdown)
        self.assertIn("    - Leaf", markdown)
        self.assertEqual(saved_tree, tree)

    def test_shared_artifact_saver_writes_main_and_raw_snapshot(self):
        class FakeClient:
            def download_artifact(self, artifact, dest):
                Path(dest).write_text("artifact body", encoding="utf-8")
                return True

            def download_artifact_pptx(self, artifact, dest):
                return False

            def download_artifact_pages(self, artifact, dest):
                return []

        artifact = {
            "id": "artifact-2",
            "title": "Shared Export",
            "type": "report",
            "type_code": 2,
            "variant": None,
            "status": "completed",
            "status_code": 3,
            "_raw": ["raw-row"],
            "content": "artifact body",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir)
            result = save_artifact(FakeClient(), artifact, out_dir)
            self.assertTrue(result["saved"])
            self.assertEqual(result["dest"].read_text(encoding="utf-8"), "artifact body")
            raw = json.loads(result["raw_snapshot"].read_text(encoding="utf-8"))

        self.assertEqual(raw["raw"], ["raw-row"])
        self.assertEqual(raw["id"], "artifact-2")

    def test_raw_artifact_snapshot_preserves_unknown_payload(self):
        artifact = {
            "id": "artifact-1",
            "title": "Future format",
            "type": "unknown_42",
            "type_code": 42,
            "variant": None,
            "status": "completed",
            "status_code": 3,
            "_raw": ["opaque", {"future": True}],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            dest = save_artifact_raw_snapshot(
                artifact,
                Path(temp_dir),
                "future_format",
            )
            saved = json.loads(dest.read_text(encoding="utf-8"))

        self.assertEqual(saved["type_code"], 42)
        self.assertEqual(saved["raw"], ["opaque", {"future": True}])


class SourceBackupTests(unittest.TestCase):
    def test_original_file_is_preferred_over_extracted_text(self):
        class FakeClient:
            def __init__(self):
                self.content_called = False

            def download_url(self, url, dest):
                Path(dest).write_bytes(b"original-docx")
                return True

            def get_source_content(self, source_id):
                self.content_called = True
                raise AssertionError("content fallback should not run")

        source = {
            "id": "src-1",
            "title": "research.docx",
            "type": "docx",
            "content_mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "download_url": "https://download.example/research.docx",
        }
        client = FakeClient()

        with tempfile.TemporaryDirectory() as temp_dir:
            result = save_source(client, source, Path(temp_dir))
            payload = result["paths"][0].read_bytes()

        self.assertTrue(result["saved"])
        self.assertEqual(result["mode"], "original")
        self.assertEqual(payload, b"original-docx")
        self.assertFalse(client.content_called)

    def test_failed_original_download_falls_back_to_source_content(self):
        class FakeClient:
            def download_url(self, url, dest):
                return False

            def get_source_content(self, source_id):
                return {
                    "title": "Website",
                    "content": "fallback text",
                    "source_type": "web_page",
                }

        source = {
            "id": "src-web",
            "title": "Website",
            "type": "web_page",
            "download_url": "https://download.example/temporary",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            result = save_source(FakeClient(), source, Path(temp_dir))
            payload = result["paths"][0].read_text(encoding="utf-8")

        self.assertTrue(result["saved"])
        self.assertEqual(result["mode"], "text")
        self.assertEqual(payload, "fallback text")


class UploadRoutingTests(unittest.TestCase):
    class FakeClient:
        def __init__(self):
            self.uploaded = []
            self.pasted = []

        def upload_file(self, notebook_id, path):
            self.uploaded.append((notebook_id, Path(path).name))
            return f"file-{len(self.uploaded)}"

        def add_source_text(self, notebook_id, title, text):
            self.pasted.append((notebook_id, title, text))
            return f"text-{len(self.pasted)}"

    def test_native_text_and_csv_formats_stay_native_files(self):
        self.assertTrue({".txt", ".md", ".csv"} <= DOCUMENT_UPLOAD_EXTENSIONS)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            files = []
            for name in ("notes.txt", "readme.md", "data.csv"):
                path = root / name
                path.write_text("body", encoding="utf-8")
                files.append(path)

            client = self.FakeClient()
            ok, fail = upload_files(client, "nb-1", files)

        self.assertEqual((ok, fail), (3, 0))
        self.assertEqual(
            [name for _nb, name in client.uploaded],
            ["notes.txt", "readme.md", "data.csv"],
        )
        self.assertEqual(client.pasted, [])

    def test_html_json_tsv_are_explicit_pasted_text_fallbacks(self):
        self.assertTrue({".html", ".json", ".tsv"} <= PASTED_TEXT_EXTENSIONS)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            files = []
            for name in ("page.html", "data.json", "table.tsv"):
                path = root / name
                path.write_text("payload", encoding="utf-8")
                files.append(path)

            client = self.FakeClient()
            ok, fail = upload_files(client, "nb-1", files)

        self.assertEqual((ok, fail), (3, 0))
        self.assertEqual(client.uploaded, [])
        self.assertEqual(
            [title for _nb, title, _text in client.pasted],
            ["page.html", "data.json", "table.tsv"],
        )

    def test_unverified_legacy_formats_are_not_sent_to_server(self):
        self.assertIn(".xlsx", UNVERIFIED_UPLOAD_EXTENSIONS)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "book.xlsx"
            path.write_bytes(b"xlsx")
            client = self.FakeClient()
            ok, fail = upload_files(client, "nb-1", [path])

        self.assertEqual((ok, fail), (0, 1))
        self.assertEqual(client.uploaded, [])
        self.assertEqual(client.pasted, [])

    def test_upload_file_rejects_empty_file_before_network(self):
        client = NotebookLMClient.__new__(NotebookLMClient)
        client._batchexecute = lambda *args, **kwargs: self.fail("network/RPC should not run")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "empty.pdf"
            path.write_bytes(b"")
            with self.assertRaises(NotebookLMError):
                client.upload_file("nb-1", path)

    def test_upload_file_rejects_over_200_mib_before_network(self):
        client = NotebookLMClient.__new__(NotebookLMClient)
        client._batchexecute = lambda *args, **kwargs: self.fail("network/RPC should not run")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "too-large.pdf"
            with path.open("wb") as handle:
                handle.truncate(MAX_UPLOAD_BYTES + 1)
            with self.assertRaises(NotebookLMError):
                client.upload_file("nb-1", path)


class UploadHelperTests(unittest.TestCase):
    def test_collect_files_is_recursive_sorted_and_skips_hidden_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "nested").mkdir()
            (root / "z.txt").write_text("z", encoding="utf-8")
            (root / "nested" / "a.md").write_text("a", encoding="utf-8")
            (root / ".cookies.json").write_text("secret", encoding="utf-8")

            collected = collect_files([str(root)])

            self.assertEqual(
                [path.relative_to(root).as_posix() for path in collected],
                ["nested/a.md", "z.txt"],
            )


class October2026GeminiNotebookTests(unittest.TestCase):
    def setUp(self):
        self.client = NotebookLMClient.__new__(NotebookLMClient)

    def test_all_three_tui_modules_import_and_route_uploads_consistently(self):
        import nlm_tui
        import nlm_tui_curses
        import nlm_tui_en

        class FakeTuiClient:
            def __init__(self):
                self.uploaded = []
                self.pasted = []

            def upload_file(self, notebook_id, path):
                self.uploaded.append((notebook_id, Path(path).name))
                return f"up-{len(self.uploaded)}"

            def add_source_text(self, notebook_id, title, text):
                self.pasted.append((notebook_id, title, text))
                return f"txt-{len(self.pasted)}"

            def add_source_url(self, notebook_id, url):
                return "url-1"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            f_md = root / "doc.md"
            f_md.write_text("# Doc", encoding="utf-8")
            f_json = root / "data.json"
            f_json.write_text('{"k":1}', encoding="utf-8")
            f_bad_json = root / "bad.json"
            f_bad_json.write_bytes(b"\xff\xfe\x00\x00")
            f_xlsx = root / "sheet.xlsx"
            f_xlsx.write_bytes(b"xlsx")

            for mod in (nlm_tui, nlm_tui_en, nlm_tui_curses):
                client = FakeTuiClient()
                summary = mod._upload_to_notebook(
                    client=client,
                    notebook_id="nb-1",
                    files=[f_md, f_json, f_bad_json, f_xlsx],
                    urls=[],
                    on_progress=lambda r, m: None,
                )
                self.assertEqual(summary["files_ok"], 2)
                self.assertEqual(summary["files_fail"], 2)
                self.assertEqual([n for _, n in client.uploaded], ["doc.md"])
                self.assertEqual([n for _, n, _ in client.pasted], ["data.json"])

    def test_interactive_learning_overview_html_and_structured_exports_meaningful_markdown(self):
        app_payload = {
            "summary": "Interactive study guide for network security.",
            "relatedArtifacts": [{"id": "art-slide-1"}, {"artifactId": "art-quiz-9"}],
            "sections": [
                {
                    "title": "Core Concepts",
                    "content": "TLS encrypts transport traffic.",
                    "keyPoints": ["Handshake", "Session keys"],
                    "slides": [{"title": "TLS Handshake Slide", "content": "ClientHello -> ServerHello"}],
                }
            ],
            "quiz": [
                {
                    "question": "Which protocol replaces SSL?",
                    "questionType": "short_answer",
                    "acceptedAnswers": ["TLS"],
                    "explanation": "TLS 1.2/1.3 replaced SSL.",
                }
            ],
            "flashcards": [
                {"front": "mTLS", "back": "Mutual TLS authentication", "hint": "Both sides present certs"}
            ],
            "mindMap": {"name": "Security", "children": [{"name": "TLS"}]},
            "citations": [{"title": "RFC 8446", "quote": "TLS 1.3 specification"}],
        }
        escaped = json.dumps(app_payload).replace('"', "&quot;")
        html_doc = f'<html><body><app-root data-app-data="{escaped}"></app-root></body></html>'

        art = [None] * 8
        art[0] = "ilo-1"
        art[1] = "Security Study Overview"
        art[2] = 2
        art[4] = 3
        art[7] = [html_doc]

        record = self.client._build_artifact_record(art)
        self.assertEqual(record["variant"], "interactive_learning_overview")
        self.assertEqual(record["referenced_artifact_ids"], ["art-slide-1", "art-quiz-9"])

        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir)
            result = save_artifact(self.client, record, out_dir)
            self.assertTrue(result["saved"])
            self.assertEqual(result["backup_mode"], "interactive_bundle")
            md_text = result["dest"].read_text(encoding="utf-8")
            html_saved = result["dest"].with_suffix(".html").read_text(encoding="utf-8")
            json_saved = json.loads(result["dest"].with_suffix(".json").read_text(encoding="utf-8"))
            meta_saved = json.loads(result["metadata_path"].read_text(encoding="utf-8"))

        self.assertIn("- Artifact subtype: interactive_learning_overview", md_text)
        self.assertIn("`art-slide-1`", md_text)
        self.assertIn("`art-quiz-9`", md_text)
        self.assertIn("### 1. Core Concepts", md_text)
        self.assertIn("TLS Handshake Slide", md_text)
        self.assertIn("Which protocol replaces SSL?", md_text)
        self.assertIn("Mutual TLS authentication", md_text)
        self.assertIn("## Embedded Mind Map", md_text)
        self.assertIn("RFC 8446", md_text)
        self.assertEqual(html_saved, html_doc)
        self.assertEqual(json_saved["referenced_artifact_ids"], ["art-slide-1", "art-quiz-9"])
        self.assertEqual(meta_saved["restore_category"], "backup_only_no_server_restore")

    def test_co_creation_pending_review_exports_outline_and_producer_notes(self):
        # Slide deck in pending_review (status_code == 6)
        slide_art = [None] * 17
        slide_art[0] = "slide-rev-1"
        slide_art[1] = "Architecture Deck"
        slide_art[2] = 8
        slide_art[4] = 6  # pending_review
        slide_art[16] = [
            None,
            {
                "prompt": "Focus on zero-trust architecture",
                "userRevisions": ["Add a slide on device posture"],
                "outlineNotes": [
                    {"title": "Intro to Zero Trust", "notes": "Never trust, always verify", "bullets": ["Identity", "Device"]}
                ],
            },
        ]
        slide_record = self.client._build_artifact_record(slide_art)
        self.assertEqual(slide_record["status"], "pending_review")
        self.assertIn("outline_notes", slide_record["co_creation_notes"])

        # Video overview in pending_review (status_code == 6) with Short format & producer notes
        video_art = [None] * 9
        video_art[0] = "vid-rev-1"
        video_art[1] = "60s Quick Summary"
        video_art[2] = 3
        video_art[4] = 6  # pending_review
        video_art[8] = [
            None,
            2,  # short video style
            {
                "durationSeconds": 58,
                "aspectRatio": "9:16",
                "language": "ja",
                "producerNotes": [
                    {"scene": "Opening Hook", "direction": "Fast-paced vertical intro"}
                ],
            },
        ]
        video_record = self.client._build_artifact_record(video_art)
        self.assertEqual(video_record["status"], "pending_review")
        self.assertEqual(video_record["video_format"], "short")
        self.assertIn("producer_notes", video_record["co_creation_notes"])

        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir)
            s_res = save_artifact(self.client, slide_record, out_dir)
            v_res = save_artifact(self.client, video_record, out_dir)

            self.assertFalse(s_res["saved"])  # Not completed yet
            self.assertTrue(s_res["review_saved"])
            self.assertEqual(s_res["backup_mode"], "co_creation_review")
            s_md = s_res["review_dest"].read_text(encoding="utf-8")
            self.assertIn("Review Outline Notes (Slide Deck)", s_md)
            self.assertIn("Intro to Zero Trust", s_md)
            self.assertIn("Add a slide on device posture", s_md)

            self.assertTrue(v_res["review_saved"])
            v_md = v_res["review_dest"].read_text(encoding="utf-8")
            self.assertIn("- Video format: short", v_md)
            self.assertIn("Review Producer Notes (Video Overview)", v_md)
            self.assertIn("Fast-paced vertical intro", v_md)

    def test_generated_file_formats_identification_download_and_no_binary_spoofing(self):
        target_cases = [
            ("report.pdf", "application/pdf", ".pdf", True),
            ("spec.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx", True),
            ("readme.md", "text/markdown", ".md", False),
            ("notes.txt", "text/plain", ".txt", False),
            ("metrics.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx", True),
            ("slides.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx", True),
            ("table.csv", "text/csv", ".csv", False),
            ("config.json", "application/json", ".json", False),
            ("chart.png", "image/png", ".png", True),
            ("photo.jpg", "image/jpeg", ".jpg", True),
            ("anim.gif", "image/gif", ".gif", True),
            ("diagram.svg", "image/svg+xml", ".svg", False),
        ]

        self.client.download_url = lambda url, dest: (Path(dest).write_bytes(b"BINARY_OK") or True)

        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir)
            for idx, (fname, mime, expected_ext, is_binary) in enumerate(target_cases, 1):
                art = [
                    f"file-{idx}",
                    fname,
                    10,
                    None,
                    3,
                    {"fileName": fname, "mimeType": mime, "downloadUrl": f"https://notebook.google.com/dl/{fname}"},
                ]
                record = self.client._build_artifact_record(art)
                self.assertEqual(record["file_extension"], expected_ext)
                res = save_artifact(self.client, record, out_dir)
                self.assertTrue(res["saved"])
                self.assertEqual(res["dest"].suffix, expected_ext)

            # Anti-spoofing check: binary .xlsx without download_url must NOT write extracted text to .xlsx
            spoof_art = [
                "file-spoof",
                "financials.xlsx",
                10,
                None,
                3,
                {"fileName": "financials.xlsx", "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text": "Col1,Col2"},
            ]
            spoof_record = self.client._build_artifact_record(spoof_art)
            self.client.get_artifact = lambda aid: None
            spoof_res = save_artifact(self.client, spoof_record, out_dir)
            self.assertFalse(spoof_res["saved"])
            self.assertFalse(spoof_res["dest"].exists())
            self.assertTrue(spoof_res["extracted_text_dest"].exists())
            self.assertEqual(spoof_res["extracted_text_dest"].name, "financials.extracted.txt")
            meta = json.loads(spoof_res["metadata_path"].read_text(encoding="utf-8"))
            self.assertEqual(meta["restore_category"], "partial_data_only")
            self.assertIn("refused to fake extracted text", meta["warning"])

    def test_raw_snapshot_redacts_signed_urls_and_credentials_and_marks_unknown_artifacts(self):
        art = {
            "id": "unk-1",
            "title": "Mystery Studio Output",
            "type": "unknown_99",
            "type_code": 99,
            "variant": None,
            "status": "completed",
            "status_code": 3,
            "_raw": [
                "unk-1",
                "https://lh3.googleusercontent.com/notebooklm/AG60hQpSignedCapabilityToken12345",
                "https://notebook.google.com/api?token=secret123&ok=1",
                "SID=mysecretcookie; SAPISID=hashsecret",
                {"csrf_token": "at_secret"},
            ],
        }

        class DummyClient:
            def download_artifact(self, a, d):
                return False

        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir)
            res = save_artifact(DummyClient(), art, out_dir)
            raw_text = res["raw_snapshot"].read_text(encoding="utf-8")
            meta = json.loads(res["metadata_path"].read_text(encoding="utf-8"))

        self.assertNotIn("AG60hQpSignedCapabilityToken12345", raw_text)
        self.assertIn("https://lh3.googleusercontent.com/[REDACTED_SIGNED_URL]", raw_text)
        self.assertNotIn("secret123", raw_text)
        self.assertNotIn("mysecretcookie", raw_text)
        self.assertNotIn("at_secret", raw_text)
        self.assertFalse(meta["official_support"])
        self.assertEqual(meta["restore_category"], "raw_json_only")

    def test_real_local_backup_html_files_parse_and_render(self):
        real_dir = Path(__file__).resolve().parent.parent / "downloads" / "NotebookLM TUI Setup and Authentication Guide" / "artifacts"
        if not real_dir.is_dir():
            self.skipTest("Local real backup directory not present")
        html_files = sorted(real_dir.glob("*.html"))
        self.assertGreaterEqual(len(html_files), 2)
        for html_path in html_files:
            html_content = html_path.read_text(encoding="utf-8")
            app_data = self.client._extract_app_artifact_data(html_content)
            self.assertIsInstance(app_data, dict)
            art = {"title": html_path.stem, "type": "flashcards", "variant": None}
            md = self.client._render_app_artifact_markdown(
                art,
                app_data,
                html_path.name,
                html_path.with_suffix(".json").name,
            )
            self.assertTrue(md.startswith(f"# {html_path.stem}"))
            self.assertNotIn("could not be parsed", md)


if __name__ == "__main__":
    unittest.main()
