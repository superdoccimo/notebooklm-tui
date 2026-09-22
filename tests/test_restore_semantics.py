import json
import tempfile
import unittest
from pathlib import Path

from nlm_backup import build_backup_metadata, save_artifact, save_source
from nlm_upload import build_restore_plan, restore_backup
from notebooklm_client import NotebookLMClient


class CurrentWireRowTests(unittest.TestCase):
    def setUp(self):
        self.client = NotebookLMClient.__new__(NotebookLMClient)
        self.calls = []

    def test_drive_backed_source_id_status_and_pdf_mime_are_decoded(self):
        meta = [None] * 20
        meta[4] = 14
        meta[9] = ["drive-id", 8, "application/pdf", ""]
        meta[19] = "application/pdf"

        row = [None] * 8
        row[0] = [None, True, ["source-drive-pdf"]]
        row[1] = "drive-paper.pdf"
        row[2] = meta
        row[3] = [None, 2]
        row[5] = "https://download.example/drive-paper.pdf"
        row[7] = [None, None, "application/pdf"]

        def rpc(rpc_id, params, source_path="/"):
            self.calls.append((rpc_id, params, source_path))
            return [[None, [row]]]

        self.client._batchexecute = rpc
        sources = self.client.list_sources("nb-1")

        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source["id"], "source-drive-pdf")
        self.assertEqual(source["raw_type_code"], 14)
        self.assertEqual(source["type_code"], 3)
        self.assertEqual(source["type"], "pdf")
        self.assertEqual(source["status"], "ready")
        self.assertEqual(source["status_code"], 2)

    def test_drive_sheet_mime_disambiguates_type_14(self):
        meta = [None] * 20
        meta[4] = 14
        meta[9] = [
            "sheet-id",
            8,
            "application/vnd.google-apps.spreadsheet",
            "",
        ]
        meta[19] = "application/vnd.google-apps.spreadsheet"

        row = [None] * 8
        row[0] = [None, True, ["source-sheet"]]
        row[1] = "Budget"
        row[2] = meta
        row[3] = [None, 2]

        self.client._batchexecute = lambda *args, **kwargs: [[None, [row]]]
        source = self.client.list_sources("nb-1")[0]

        self.assertEqual(source["type_code"], 7)
        self.assertEqual(source["type"], "google_spreadsheet")

    def test_create_note_uses_create_then_update_wire_shapes(self):
        def rpc(rpc_id, params, source_path="/"):
            self.calls.append((rpc_id, params, source_path))
            if rpc_id == "CYK0Xb":
                return ["note-1"]
            return None

        self.client._batchexecute = rpc
        note_id = self.client.create_note("nb-1", "Native Note", "Body")

        self.assertEqual(note_id, "note-1")
        self.assertEqual(
            self.calls[0],
            (
                "CYK0Xb",
                ["nb-1", "", [1], None, "Native Note"],
                "/notebook/nb-1",
            ),
        )
        self.assertEqual(
            self.calls[1],
            (
                "cYAfTb",
                ["nb-1", "note-1", [[["Body", "Native Note", [], 0]]]],
                "/notebook/nb-1",
            ),
        )

    def test_wait_for_source_ready_accepts_legacy_row_without_status_slot(self):
        self.client.list_sources = lambda notebook_id: [
            {
                "id": "legacy-source",
                "title": "Legacy",
                "type": "pasted_text",
                "status": "unknown",
                "status_code": None,
            }
        ]

        source = self.client.wait_for_source_ready(
            "nb-legacy",
            "legacy-source",
            timeout=0.1,
            interval=0.01,
        )

        self.assertEqual(source["id"], "legacy-source")

    def test_note_reader_accepts_current_outer_current_legacy_and_deleted_rows(self):
        mind_map_body = json.dumps(
            {"name": "Root", "children": [{"name": "Leaf"}]}
        )
        rows = [
            [None, ["note-current", "Current body", None, None, "Current Title"]],
            ["note-legacy", "Legacy body"],
            [None, ["map-1", mind_map_body, None, None, "Map Title"]],
            ["deleted", None, 2],
        ]

        self.client._batchexecute = lambda *args, **kwargs: [rows, [123456]]

        notes = self.client.list_notes("nb-1")
        mindmaps = self.client.list_mindmaps("nb-1")

        self.assertEqual(
            [(note["id"], note["title"], note["content"]) for note in notes],
            [
                ("note-current", "Current Title", "Current body"),
                ("note-legacy", "Untitled", "Legacy body"),
            ],
        )
        self.assertEqual(len(mindmaps), 1)
        self.assertEqual(mindmaps[0]["id"], "map-1")
        self.assertEqual(mindmaps[0]["title"], "Map Title")
        self.assertEqual(mindmaps[0]["data"]["children"][0]["name"], "Leaf")


class BackupMetadataTests(unittest.TestCase):
    def test_backup_metadata_declares_schema_v2_restore_boundaries(self):
        meta = build_backup_metadata(
            "nb-1",
            "Title",
            {"id": "nb-1", "title": "Title", "source_count": 3},
        )

        self.assertEqual(meta["backup_schema_version"], 2)
        self.assertEqual(meta["source_count"], 3)
        self.assertEqual(
            meta["restore_semantics"]["studio_artifacts"],
            "local backup only; not recreated from local files",
        )


class BackupEvidenceTests(unittest.TestCase):
    def test_source_sidecar_does_not_persist_signed_download_url(self):
        class FakeClient:
            def download_url(self, url, dest):
                Path(dest).write_bytes(b"original")
                return True

        source = {
            "id": "src-1",
            "title": "paper.pdf",
            "type": "pdf",
            "type_code": 3,
            "raw_type_code": 14,
            "status": "ready",
            "status_code": 2,
            "url": None,
            "download_url": "https://secret.example/file?token=DO_NOT_SAVE",
            "content_mime": "application/pdf",
            "drive_mime": "application/pdf",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            result = save_source(FakeClient(), source, Path(temp_dir))
            sidecar = json.loads(
                result["metadata_path"].read_text(encoding="utf-8")
            )
            serialized = json.dumps(sidecar)

        self.assertTrue(result["saved"])
        self.assertEqual(sidecar["backup_mode"], "original")
        self.assertNotIn("download_url", sidecar)
        self.assertNotIn("DO_NOT_SAVE", serialized)

    def test_pending_source_waits_with_notebook_context(self):
        class FakeClient:
            def __init__(self):
                self.waited = []

            def wait_for_source_ready(self, notebook_id, source_id, timeout):
                self.waited.append((notebook_id, source_id, timeout))
                return {
                    "id": source_id,
                    "title": "Pending",
                    "type": "pasted_text",
                    "type_code": 4,
                    "status": "ready",
                    "status_code": 2,
                }

            def get_source_content(self, source_id):
                return {
                    "title": "Pending",
                    "content": "ready body",
                    "source_type": "pasted_text",
                }

        client = FakeClient()
        source = {
            "id": "src-pending",
            "notebook_id": "nb-pending",
            "title": "Pending",
            "type": "pasted_text",
            "status": "pending",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            result = save_source(
                client,
                source,
                Path(temp_dir),
                wait_timeout=17,
            )

        self.assertTrue(result["saved"])
        self.assertEqual(client.waited, [("nb-pending", "src-pending", 17)])

    def test_guided_view_raw_fallback_is_json_not_bin(self):
        client = NotebookLMClient.__new__(NotebookLMClient)
        artifact = {
            "id": "guided-1",
            "title": "Guided View",
            "type": "guided_view",
            "type_code": 11,
            "variant": None,
            "status": "completed",
            "status_code": 3,
            "_raw": ["guided-1", "Guided View", 11, None, 3],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            result = save_artifact(client, artifact, Path(temp_dir))
            saved = json.loads(result["dest"].read_text(encoding="utf-8"))

        self.assertTrue(result["saved"])
        self.assertEqual(result["dest"].suffix, ".json")
        self.assertEqual(saved["type_code"], 11)


class RestoreDryRunTests(unittest.TestCase):
    def _write_json(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_dry_run_matches_v2_semantic_categories_without_remote_client(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_json(
                root / "metadata.json",
                {
                    "id": "old-nb",
                    "title": "Plan Me",
                    "backup_schema_version": 2,
                },
            )
            sources = root / "sources"
            (sources / "doc.docx").parent.mkdir(parents=True, exist_ok=True)
            (sources / "doc.docx").write_bytes(b"doc")
            (sources / "paper").mkdir()
            (sources / "paper" / "page1.png").write_bytes(b"page")
            self._write_json(
                sources / "_metadata" / "url.json",
                {
                    "title": "Web",
                    "type": "web_page",
                    "url": "https://example.com",
                    "backup_mode": "text",
                    "files": [],
                },
            )
            self._write_json(
                sources / "_metadata" / "doc.json",
                {
                    "title": "doc.docx",
                    "type": "docx",
                    "backup_mode": "original",
                    "files": ["doc.docx"],
                },
            )
            self._write_json(
                sources / "_metadata" / "pdf.json",
                {
                    "title": "paper.pdf",
                    "type": "pdf",
                    "backup_mode": "rendered-pages",
                    "files": ["paper/page1.png"],
                },
            )
            notes = root / "notes"
            notes.mkdir()
            (notes / "Note.md").write_text("note", encoding="utf-8")
            self._write_json(
                notes / "_metadata" / "note.json",
                {"title": "Note", "file": "Note.md"},
            )
            artifacts = root / "artifacts"
            artifacts.mkdir()
            (artifacts / "report.md").write_text("report", encoding="utf-8")

            plan = build_restore_plan(root)

        actions = {row["title"]: (row["status"], row["action"]) for row in plan["sources"]}
        self.assertEqual(actions["Web"], ("restored", "readd_url"))
        self.assertEqual(actions["doc.docx"], ("restored", "upload_original_file"))
        self.assertEqual(actions["paper.pdf"], ("preserved_only", "keep_local"))
        self.assertEqual(plan["notes"][0]["action"], "create_native_note")
        self.assertTrue(plan["artifacts"]["preserved"])
        self.assertEqual(plan["summary"]["expected_state"], "COMPLETE WITH LIMITATIONS")
        self.assertEqual(plan["summary"]["would_fail_preflight"], 0)

    def test_dry_run_reports_missing_note_file_before_any_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_json(
                root / "metadata.json",
                {"title": "Broken", "backup_schema_version": 2},
            )
            self._write_json(
                root / "notes" / "_metadata" / "note.json",
                {"title": "Missing", "file": "missing.md"},
            )

            plan = build_restore_plan(root)

        self.assertEqual(plan["notes"][0]["status"], "failed")
        self.assertEqual(plan["summary"]["would_fail_preflight"], 1)
        self.assertEqual(plan["summary"]["expected_state"], "PARTIAL")

    def test_dry_run_legacy_nested_directory_is_preserved_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_json(root / "metadata.json", {"title": "Legacy"})
            nested = root / "sources" / "paper"
            nested.mkdir(parents=True)
            (nested / "page1.png").write_bytes(b"page")

            plan = build_restore_plan(root)

        self.assertEqual(plan["sources"][0]["status"], "preserved_only")
        self.assertEqual(plan["sources"][0]["action"], "keep_local")


class FakeRestoreClient:
    def __init__(self):
        self.uploaded_files = []
        self.added_urls = []
        self.added_text = []
        self.created_notes = []
        self.waited = []

    def create_notebook(self, title):
        return "restored-nb"

    def add_source_url(self, notebook_id, url):
        self.added_urls.append(url)
        return f"url-{len(self.added_urls)}"

    def upload_file(self, notebook_id, path):
        self.uploaded_files.append(Path(path).name)
        return f"file-{len(self.uploaded_files)}"

    def add_source_text(self, notebook_id, title, content):
        self.added_text.append((title, content))
        return f"text-{len(self.added_text)}"

    def wait_for_source_ready(self, notebook_id, source_id, timeout):
        self.waited.append((notebook_id, source_id, timeout))
        return {"id": source_id, "status": "ready"}

    def create_note(self, notebook_id, title, content=""):
        self.created_notes.append((title, content))
        return f"note-{len(self.created_notes)}"


class RestoreSemanticsTests(unittest.TestCase):
    def _write_json(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_schema_v2_restore_preserves_semantics_and_does_not_upload_pdf_pages(self):
        client = FakeRestoreClient()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_json(
                root / "metadata.json",
                {
                    "id": "old-nb",
                    "title": "Backup",
                    "backup_schema_version": 2,
                },
            )

            sources = root / "sources"
            (sources / "paper").mkdir(parents=True)
            (sources / "paper" / "page1.png").write_bytes(b"page")
            (sources / "doc.docx").write_bytes(b"docx")
            self._write_json(
                sources / "_metadata" / "url.json",
                {
                    "schema_version": 1,
                    "id": "s-url",
                    "title": "Website",
                    "type": "web_page",
                    "url": "https://example.com/article",
                    "backup_mode": "text",
                    "saved": True,
                    "files": [],
                },
            )
            self._write_json(
                sources / "_metadata" / "file.json",
                {
                    "schema_version": 1,
                    "id": "s-file",
                    "title": "doc.docx",
                    "type": "docx",
                    "backup_mode": "original",
                    "saved": True,
                    "files": ["doc.docx"],
                },
            )
            self._write_json(
                sources / "_metadata" / "pdf.json",
                {
                    "schema_version": 1,
                    "id": "s-pdf",
                    "title": "paper.pdf",
                    "type": "pdf",
                    "backup_mode": "rendered-pages",
                    "saved": True,
                    "files": ["paper/page1.png"],
                },
            )

            notes = root / "notes"
            (notes / "Note.md").parent.mkdir(parents=True, exist_ok=True)
            (notes / "Note.md").write_text("native note body", encoding="utf-8")
            self._write_json(
                notes / "_metadata" / "note.json",
                {
                    "schema_version": 1,
                    "id": "note-old",
                    "title": "Native Note",
                    "file": "Note.md",
                },
            )

            mindmaps = root / "mindmaps"
            map_payload = {
                "name": "Root",
                "children": [{"name": "Leaf"}],
            }
            self._write_json(mindmaps / "Map.json", map_payload)
            self._write_json(
                mindmaps / "_metadata" / "map.json",
                {
                    "schema_version": 1,
                    "id": "map-old",
                    "title": "Map",
                    "json_file": "Map.json",
                    "markdown_file": "Map.md",
                },
            )

            (root / "artifacts").mkdir()
            (root / "artifacts" / "report.md").write_text(
                "preserved studio artifact",
                encoding="utf-8",
            )

            ok = restore_backup(client, root, wait_timeout=9)
            report = json.loads(
                (root / "restore-report-restored-nb.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertTrue(ok)
        self.assertEqual(client.added_urls, ["https://example.com/article"])
        self.assertEqual(client.uploaded_files, ["doc.docx"])
        self.assertFalse(any(name == "page1.png" for name in client.uploaded_files))
        self.assertEqual(len(client.created_notes), 2)
        self.assertIn(("Native Note", "native note body"), client.created_notes)
        self.assertIn(("Map", json.dumps(map_payload, ensure_ascii=False)), client.created_notes)
        pdf_row = next(row for row in report["sources"] if row["type"] == "pdf")
        self.assertEqual(pdf_row["status"], "preserved_only")
        self.assertEqual(report["artifacts"]["recreated"], 0)

    def test_legacy_restore_does_not_recurse_into_pdf_page_directories(self):
        client = FakeRestoreClient()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_json(
                root / "metadata.json",
                {"id": "old", "title": "Legacy"},
            )
            sources = root / "sources"
            sources.mkdir()
            (sources / "website.md").write_text("legacy text", encoding="utf-8")
            (sources / "paper").mkdir()
            (sources / "paper" / "page1.png").write_bytes(b"page")

            ok = restore_backup(client, root, wait_timeout=5)
            report = json.loads(
                (root / "restore-report-restored-nb.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertTrue(ok)
        self.assertEqual(client.uploaded_files, [])
        self.assertEqual(client.added_text, [("website.md", "legacy text")])
        nested = next(
            row for row in report["sources"]
            if row.get("title") == "paper"
        )
        self.assertEqual(nested["status"], "preserved_only")


if __name__ == "__main__":
    unittest.main()
