import tempfile
import unittest
from pathlib import Path

from nlm_canary import (
    RELEASE_REQUIRED_ARTIFACTS,
    _artifact_labels,
    _evaluate_artifact_coverage,
    run_read_only_canary,
    run_write_smoke,
)


class CanaryHelperTests(unittest.TestCase):
    def test_artifact_labels_include_variant(self):
        artifact = {
            "type": "flashcards",
            "variant": "quiz",
        }
        self.assertEqual(_artifact_labels(artifact), {"flashcards", "quiz"})

    def test_interactive_learning_overview_requires_interactive_payload(self):
        plain = {"type": "report", "variant": None}
        interactive = {
            "type": "report",
            "variant": None,
            "app_html": "<html><app-root></app-root></html>",
        }

        self.assertEqual(_artifact_labels(plain), {"report"})
        self.assertEqual(
            _artifact_labels(interactive),
            {"report", "interactive_learning_overview"},
        )

    def test_release_profile_has_expected_compatibility_surface(self):
        self.assertIn("interactive_learning_overview", RELEASE_REQUIRED_ARTIFACTS)
        self.assertIn("interactive_mind_map", RELEASE_REQUIRED_ARTIFACTS)
        self.assertIn("quiz", RELEASE_REQUIRED_ARTIFACTS)
        self.assertIn("video_overview", RELEASE_REQUIRED_ARTIFACTS)

    def test_coverage_requires_successful_completed_export(self):
        artifacts = [
            {
                "id": "quiz-1",
                "type": "flashcards",
                "variant": "quiz",
                "status": "completed",
            },
            {
                "id": "video-1",
                "type": "video_overview",
                "variant": None,
                "status": "completed",
            },
        ]
        results = [{"saved": True}, {"saved": False}]

        coverage = _evaluate_artifact_coverage(
            artifacts,
            results,
            {"quiz", "video_overview"},
        )

        self.assertFalse(coverage["passed"])
        self.assertEqual(coverage["missing_required"], ["video_overview"])
        self.assertEqual(
            coverage["failed_completed_exports"][0]["id"],
            "video-1",
        )


class FakeReadOnlyClient:
    def list_notebooks(self):
        return [{"id": "nb-1", "title": "Canary", "source_count": 1}]

    def list_sources(self, notebook_id):
        self.assert_id(notebook_id)
        return [
            {
                "id": "src-1",
                "title": "note.txt",
                "type": "pasted_text",
                "download_url": None,
            }
        ]

    def get_source_content(self, source_id):
        if source_id != "src-1":
            raise AssertionError(source_id)
        return {
            "title": "note.txt",
            "content": "canary source",
            "source_type": "pasted_text",
        }

    def list_artifacts(self, notebook_id):
        self.assert_id(notebook_id)
        return []

    def list_notes(self, notebook_id):
        self.assert_id(notebook_id)
        return [{"id": "note-1", "title": "Note", "content": "body"}]

    def list_mindmaps(self, notebook_id):
        self.assert_id(notebook_id)
        return []

    @staticmethod
    def assert_id(notebook_id):
        if notebook_id != "nb-1":
            raise AssertionError(notebook_id)


class CanaryReadOnlyTests(unittest.TestCase):
    def test_inventory_profile_is_read_only_and_passes_when_exports_succeed(self):
        client = FakeReadOnlyClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            report, report_path = run_read_only_canary(
                client,
                "nb-1",
                Path(temp_dir),
                "inventory",
            )

            self.assertTrue(report["passed"])
            self.assertTrue(report_path.exists())
            self.assertEqual(report["sources"][0]["mode"], "text")
            self.assertEqual(report["notes"], {"saved": 1, "total": 1})


class FakeWriteClient:
    def __init__(self):
        self.deleted = []
        self.created = []

    def create_notebook(self, title):
        self.created.append(title)
        return "temp-nb"

    def add_source_text(self, notebook_id, title, text):
        if notebook_id != "temp-nb":
            raise AssertionError(notebook_id)
        return "text-src"

    def add_source_url(self, notebook_id, url):
        if notebook_id != "temp-nb":
            raise AssertionError(notebook_id)
        return "url-src"

    def list_sources(self, notebook_id):
        if notebook_id != "temp-nb":
            raise AssertionError(notebook_id)
        return [
            {
                "id": "text-src",
                "title": "canary.txt",
                "type": "pasted_text",
                "download_url": None,
            },
            {
                "id": "url-src",
                "title": "Example",
                "type": "web_page",
                "download_url": None,
            },
        ]

    def get_source_content(self, source_id):
        return {
            "title": source_id,
            "content": f"content for {source_id}",
            "source_type": "pasted_text",
        }

    def delete_notebook(self, notebook_id):
        self.deleted.append(notebook_id)
        return notebook_id == "temp-nb"


class CanaryWriteSmokeTests(unittest.TestCase):
    def test_write_smoke_deletes_only_created_notebook(self):
        client = FakeWriteClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            report, report_path = run_write_smoke(
                client,
                Path(temp_dir),
                "https://example.com",
            )

            self.assertTrue(report["passed"])
            self.assertTrue(report["cleanup_deleted_disposable_notebook"])
            self.assertEqual(client.deleted, ["temp-nb"])
            self.assertTrue(report_path.exists())
            self.assertEqual(
                {row["id"] for row in report["source_backups"]},
                {"text-src", "url-src"},
            )


if __name__ == "__main__":
    unittest.main()
