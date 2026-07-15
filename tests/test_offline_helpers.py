import tempfile
import unittest
from pathlib import Path

from nlm_backup import _unique_path, mindmap_to_markdown, sanitize_filename
from nlm_upload import collect_files


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
