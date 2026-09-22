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
from nlm_upload import collect_files
from notebooklm_client import (
    ARTIFACT_STATUS,
    SOURCE_TYPES,
    NotebookLMClient,
    _is_youtube_url,
    _template_block,
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


if __name__ == "__main__":
    unittest.main()
