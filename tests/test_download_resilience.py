"""Offline regression tests for bounded, atomic media downloads."""

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from notebooklm_client import (
    DOWNLOAD_SOCKET_TIMEOUT_SECONDS,
    NotebookLMClient,
)


class _PartialThenTimeout(io.BytesIO):
    def read(self, size=-1):
        if self.tell() == 0:
            return super().read(size)
        raise TimeoutError("simulated stalled download")


class DownloadResilienceTests(unittest.TestCase):
    def setUp(self):
        self.client = NotebookLMClient.__new__(NotebookLMClient)
        self.client._opener = Mock()

    def test_complete_download_is_atomic_and_uses_socket_timeout(self):
        self.client._opener.open.return_value = io.BytesIO(b"complete video")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "video.mp4"
            target.write_bytes(b"old backup")

            self.assertTrue(self.client.download_url("https://example.com/video", target))
            self.assertEqual(target.read_bytes(), b"complete video")
            self.assertEqual(list(Path(directory).glob("*.part")), [])
            self.assertEqual(list(Path(directory).glob(".*.part")), [])
            self.assertEqual(
                self.client._opener.open.call_args.kwargs["timeout"],
                DOWNLOAD_SOCKET_TIMEOUT_SECONDS,
            )

    def test_partial_download_timeout_keeps_previous_file(self):
        self.client._opener.open.return_value = _PartialThenTimeout(b"partial video")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "video.mp4"
            target.write_bytes(b"verified earlier copy")

            self.assertFalse(self.client.download_url("https://example.com/video", target))
            self.assertEqual(target.read_bytes(), b"verified earlier copy")
            self.assertEqual(list(Path(directory).glob(".*.part")), [])

    def test_empty_download_does_not_create_zero_byte_file(self):
        self.client._opener.open.return_value = io.BytesIO(b"")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "video.mp4"

            self.assertFalse(self.client.download_url("https://example.com/empty", target))
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(directory).glob(".*.part")), [])

    def test_connection_timeout_preserves_existing_file(self):
        self.client._opener.open.side_effect = TimeoutError("connection stalled")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "video.mp4"
            target.write_bytes(b"good data")

            self.assertFalse(self.client.download_url("https://example.com/stall", target))
            self.assertEqual(target.read_bytes(), b"good data")
            self.assertEqual(list(Path(directory).glob(".*.part")), [])

    def test_total_deadline_aborts_and_cleans_partial_file(self):
        self.client._opener.open.return_value = io.BytesIO(b"video")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "video.mp4"
            with patch("notebooklm_client.time.monotonic", side_effect=[0, 601]):
                self.assertFalse(self.client.download_url("https://example.com/slow", target))
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(directory).glob(".*.part")), [])


if __name__ == "__main__":
    unittest.main()
