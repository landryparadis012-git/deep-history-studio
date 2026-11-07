import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import free_video


class FreeVideoSafetyTests(unittest.TestCase):
    def test_unspecified_archive_license_is_rejected(self):
        self.assertFalse(free_video._allowed_archive_license(""))
        self.assertFalse(free_video._allowed_archive_license("https://archive.org/about/terms.php"))

    def test_known_cc_license_is_accepted(self):
        self.assertTrue(free_video._allowed_archive_license(
            "https://creativecommons.org/licenses/by/4.0/"))

    def test_wikimedia_requires_license_metadata(self):
        self.assertFalse(free_video._allowed_commons_license("", ""))
        self.assertTrue(free_video._allowed_commons_license(
            "CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/"))

    def test_relevance_gate_rejects_unrelated_title(self):
        self.assertEqual(free_video._relevance("ancient rome", "modern sports car"), 0.0)
        self.assertEqual(free_video._relevance("ancient rome", "Ancient Rome forum"), 1.0)

    def test_download_rejects_unapproved_hosts_before_network(self):
        with self.assertRaises(ValueError):
            free_video.download_video(
                {"download_url": "https://example.com/clip.mp4"},
                "/tmp/should-not-be-created.mp4",
            )

    def test_search_cache_avoids_second_provider_call(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = [{"title": "Ancient Rome", "relevance": 1.0}]
            with patch.object(free_video, "search_wikimedia", return_value=fixture) as wiki, \
                 patch.object(free_video, "search_internet_archive", return_value=[]) as archive:
                first = free_video.search_free_videos("ancient rome", directory)
                second = free_video.search_free_videos("ancient rome", directory)
            self.assertEqual(first, second)
            wiki.assert_called_once()
            archive.assert_called_once()

    def test_total_provider_failure_is_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(free_video, "search_wikimedia", side_effect=RuntimeError("offline")) as wiki, \
                 patch.object(free_video, "search_internet_archive", side_effect=RuntimeError("offline")) as archive:
                free_video.search_free_videos("ancient rome", directory)
                free_video.search_free_videos("ancient rome", directory)
            self.assertEqual(wiki.call_count, 2)
            self.assertEqual(archive.call_count, 2)
            self.assertEqual(list(Path(directory).glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
