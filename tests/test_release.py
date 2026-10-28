"""Release download tests use in-memory HTTP responses, never live network."""
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import sky_hud as hud


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("SKY_HUD_TEST_TMPDIR"))
        self.base = Path(self.tmp.name)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("SkyHUD.exe", b"hud")
            archive.writestr("SkyHUD-Updater.exe", b"updater")
        self.data = stream.getvalue()
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.asset = {"name": "SkyHUD-Update.zip", "size": len(self.data),
                      "digest": "sha256:" + self.digest,
                      "browser_download_url": "https://github.com/Mafsolin/sky-hud/releases/download/v2.0.0/SkyHUD-Update.zip"}
        self.release = {"tag_name": "v2.0.0", "assets": [self.asset]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_cli_check_supports_english_windows_console_encoding(self):
        import subprocess
        import sys
        fixture = self.base / "check.json"
        fixture.write_text('{"songNotes":[{"time":0,"key":"1Key0"}]}', encoding="utf-8")
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        check = subprocess.run([sys.executable, str(Path(hud.__file__).resolve()), "--check", str(fixture)],
                               env=env, capture_output=True, timeout=10)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn(b"ALL PASS", check.stdout)

    def test_verified_download_contains_extracted_updater(self):
        with patch.object(hud.urllib.request, "urlopen", return_value=io.BytesIO(self.data)):
            work, package, sidecar, digest = hud.download_update(self.release, work_root=self.base)
        self.assertEqual(Path(sidecar).read_bytes(), b"updater")
        self.assertEqual(Path(package).read_bytes(), self.data)
        self.assertEqual(digest, self.digest)
        self.assertEqual(Path(work), Path(sidecar).parent)

    def test_download_without_trusted_hash_fails_closed(self):
        self.asset.pop("digest")
        with patch.object(hud.urllib.request, "urlopen") as request:
            with self.assertRaises(ValueError):
                hud.download_update(self.release, work_root=self.base)
        request.assert_not_called()

    def test_wrong_download_hash_leaves_no_temp_data(self):
        self.asset["digest"] = "sha256:" + "0" * 64
        with patch.object(hud.urllib.request, "urlopen", return_value=io.BytesIO(self.data)):
            with self.assertRaises(ValueError):
                hud.download_update(self.release, work_root=self.base)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_download_from_other_repository_is_rejected(self):
        self.asset["browser_download_url"] = "https://example.org/SkyHUD-Update.zip"
        with patch.object(hud.urllib.request, "urlopen") as request:
            with self.assertRaises(ValueError):
                hud.download_update(self.release, work_root=self.base)
        request.assert_not_called()

    def test_launch_uses_existing_sidecar_and_passes_mandatory_hash(self):
        sidecar = self.base / "SkyHUD-Updater.exe"
        sidecar.write_bytes(b"updater")
        package = self.base / "SkyHUD-Update.zip"
        package.write_bytes(self.data)
        with patch.object(hud.subprocess, "Popen") as spawn:
            hud.launch_update((str(self.base), str(package), str(sidecar), self.digest))
        argv = spawn.call_args.args[0]
        self.assertEqual(argv[0], str(sidecar))
        self.assertIn("--sha256", argv)
        self.assertEqual(argv[argv.index("--sha256") + 1], self.digest)


if __name__ == "__main__":
    unittest.main()
