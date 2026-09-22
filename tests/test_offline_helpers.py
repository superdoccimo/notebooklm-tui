import json
import tempfile
import unittest
from pathlib import Path

from nlm_backup import (
    _unique_path,
    mindmap_to_markdown,
    sanitize_filename,
    save_artifact_raw_snapshot,
)
from nlm_upload import collect_files
from notebooklm_client import NotebookLMClient


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
