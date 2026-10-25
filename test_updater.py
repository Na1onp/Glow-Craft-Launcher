import hashlib
import io
import json
import plistlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import launcher_updater as updater


class UpdaterTests(unittest.TestCase):
    def test_version_and_unconfigured_feed(self):
        self.assertEqual(updater.version_tuple("1.2.10"), (1, 2, 10))
        with self.assertRaises(ValueError):
            updater.version_tuple("1.2-beta")
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(updater.check({}, Path(temp))["status"], "unconfigured")

    def test_check_validates_manifest_and_asset(self):
        asset = {"url": "https://example.org/release.zip", "sha256": "a" * 64, "size": 12}
        manifest = {"schema": 1, "version": "0.4.1", "platforms": {"macos-arm64": asset}}
        with patch.object(updater, "APP_VERSION", "0.4.0"), patch.object(updater, "platform_key", return_value="macos-arm64"), patch.object(updater, "_read_json", return_value=manifest):
            with tempfile.TemporaryDirectory() as temp:
                self.assertEqual(updater.check({"update_feed_url": "https://example.org/feed.json"}, Path(temp))["status"], "available")
        asset["sha256"] = "bad"
        with patch.object(updater, "APP_VERSION", "0.4.0"), patch.object(updater, "platform_key", return_value="macos-arm64"), patch.object(updater, "_read_json", return_value=manifest):
            with tempfile.TemporaryDirectory() as temp, self.assertRaises(ValueError):
                updater.check({"update_feed_url": "https://example.org/feed.json"}, Path(temp))

    def test_download_rejects_zip_traversal(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("Glow Craft Launcher.app/../../bad", "bad")
        payload = out.getvalue()
        release = {"status": "available", "version": "0.4.1", "asset": {
            "url": "https://example.org/release.zip", "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}}
        with tempfile.TemporaryDirectory() as temp, patch.object(updater.urllib.request, "urlopen", return_value=io.BytesIO(payload)):
            with self.assertRaisesRegex(ValueError, "不安全"):
                updater.download_and_stage(release, Path(temp))
            self.assertFalse((Path(temp) / "bad").exists())

    def test_download_rejects_wrong_digest(self):
        payload = b"not a zip"
        release = {"status": "available", "version": "0.4.1", "asset": {
            "url": "https://example.org/release.zip", "sha256": "0" * 64, "size": len(payload)}}
        progress = []
        with tempfile.TemporaryDirectory() as temp, patch.object(updater.urllib.request, "urlopen", return_value=io.BytesIO(payload)):
            with self.assertRaisesRegex(ValueError, "校验失败"):
                updater.download_and_stage(release, Path(temp), progress.append)
        self.assertEqual(progress[-1], {"phase": "download", "received": len(payload), "total": len(payload)})


if __name__ == "__main__":
    unittest.main()
