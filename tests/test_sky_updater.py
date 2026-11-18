"""Updater safety checks use only temporary release-owned files."""
import ctypes
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

import sky_updater as updater


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("SKY_HUD_TEST_TMPDIR"))
        self.base = Path(self.tmp.name).resolve()
        self.package = self.base / "update.zip"
        self.install = self.base / "install"
        self.install.mkdir()
        self.retry_timeout = patch.object(updater, "REPLACE_TIMEOUT", 0)
        self.retry_timeout.start()

    def tearDown(self):
        self.retry_timeout.stop()
        self.tmp.cleanup()

    def package_files(self, files=None):
        files = files or {"SkyHUD.exe": b"new-hud", "SkyHUD-Updater.exe": b"new-updater",
                          "README-PORTABLE.md": b"new-readme"}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.package, "w") as archive:
                for name, data in (files.items() if isinstance(files, dict) else files):
                    archive.writestr(name, data)
        return hashlib.sha256(self.package.read_bytes()).hexdigest()

    def test_duplicate_archive_entries_are_rejected(self):
        self.package_files([("SkyHUD.exe", b"one"), ("SkyHUD.exe", b"two"),
                            ("SkyHUD-Updater.exe", b"updater")])
        with self.assertRaises(ValueError):
            updater.safe_members(self.package)

    def test_data_and_path_traversal_entries_are_rejected(self):
        for name in ("../SkyHUD.exe", "songs/User Songs/mine.txt", "sky_hud_config.json", "C:/SkyHUD.exe"):
            with self.subTest(name=name):
                self.package_files({"SkyHUD.exe": b"hud", "SkyHUD-Updater.exe": b"updater", name: b"bad"})
                with self.assertRaises(ValueError):
                    updater.safe_members(self.package)

    def test_prepare_extracts_verified_sidecar_to_temporary_directory(self):
        digest = self.package_files()
        work, package, sidecar = updater.prepare_update(self.package, digest, work_root=self.base)
        self.assertEqual(Path(sidecar).read_bytes(), b"new-updater")
        self.assertEqual(Path(package).read_bytes(), self.package.read_bytes())
        self.assertNotEqual(Path(sidecar).parent, self.install)
        self.assertEqual(Path(work), Path(sidecar).parent)

    def test_missing_or_wrong_digest_prevents_prepare(self):
        self.package_files()
        for digest in (None, "", "md5:abc", "0" * 64):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                updater.prepare_update(self.package, digest, work_root=self.base)
        self.assertEqual(list(self.base.glob("sky-hud-update-*")), [])

    def test_install_verifies_hash_again_before_replacing_files(self):
        self.package_files()
        (self.install / "SkyHUD.exe").write_bytes(b"old")
        with self.assertRaises(ValueError):
            updater.install_package(self.package, self.install, restart=False, expected_sha256="0" * 64)
        self.assertEqual((self.install / "SkyHUD.exe").read_bytes(), b"old")

    def test_backup_failure_never_deletes_existing_files(self):
        self.package_files()
        (self.install / "SkyHUD.exe").write_bytes(b"old-hud")
        (self.install / "SkyHUD-Updater.exe").write_bytes(b"old-updater")
        real_copy = updater.shutil.copy2
        def fail_backup(source, target, *args, **kwargs):
            if Path(source) == self.install / "SkyHUD-Updater.exe":
                raise PermissionError("injected backup failure")
            return real_copy(source, target, *args, **kwargs)
        with patch.object(updater.shutil, "copy2", side_effect=fail_backup):
            with self.assertRaises(PermissionError):
                updater.install_package(self.package, self.install, restart=False)
        self.assertEqual((self.install / "SkyHUD.exe").read_bytes(), b"old-hud")
        self.assertEqual((self.install / "SkyHUD-Updater.exe").read_bytes(), b"old-updater")

    def test_mid_install_failure_restores_old_files_and_removes_new_files(self):
        self.package_files([("README-PORTABLE.md", b"new"), ("SkyHUD.exe", b"new"),
                            ("SkyHUD-Updater.exe", b"new")])
        (self.install / "SkyHUD.exe").write_bytes(b"old-hud")
        (self.install / "SkyHUD-Updater.exe").write_bytes(b"old-updater")
        real_replace = updater.os.replace
        failed = False
        def fail_once(source, target):
            nonlocal failed
            if Path(target) == self.install / "SkyHUD-Updater.exe" and not failed:
                failed = True
                raise PermissionError("injected install failure")
            return real_replace(source, target)
        with patch.object(updater.os, "replace", side_effect=fail_once):
            with self.assertRaises(PermissionError):
                updater.install_package(self.package, self.install, restart=False)
        self.assertEqual((self.install / "SkyHUD.exe").read_bytes(), b"old-hud")
        self.assertEqual((self.install / "SkyHUD-Updater.exe").read_bytes(), b"old-updater")
        self.assertFalse((self.install / "README-PORTABLE.md").exists())

    def test_failed_rollback_keeps_recoverable_backup(self):
        self.package_files()
        for name in updater.REQUIRED_FILES:
            (self.install / name).write_bytes(b"old-" + name.encode())
        real_replace = updater.os.replace
        def block_second_exe(source, target):
            if Path(target) == self.install / "SkyHUD-Updater.exe":
                raise PermissionError("injected locked file")
            return real_replace(source, target)
        with patch.object(updater.os, "replace", side_effect=block_second_exe):
            with self.assertRaises(Exception):
                updater.install_package(self.package, self.install, restart=False)
        backups = list(self.install.glob(".sky-hud-backups/*/SkyHUD.exe"))
        self.assertTrue(backups, "a failed transaction must retain its backup")
        self.assertEqual(backups[-1].read_bytes(), b"old-SkyHUD.exe")

    def test_transient_windows_exe_lock_is_retried(self):
        self.package_files()
        (self.install / "SkyHUD.exe").write_bytes(b"old")
        real_replace = updater.os.replace
        attempts = 0
        def temporary_lock(source, target):
            nonlocal attempts
            if Path(target) == self.install / "SkyHUD.exe":
                attempts += 1
                if attempts == 1:
                    raise PermissionError("bootloader still closing")
            return real_replace(source, target)
        with patch.object(updater.os, "replace", side_effect=temporary_lock), \
                patch.object(updater, "REPLACE_TIMEOUT", 1):
            updater.install_package(self.package, self.install, restart=False)
        self.assertGreaterEqual(attempts, 2)
        self.assertEqual((self.install / "SkyHUD.exe").read_bytes(), b"new-hud")

    def test_windows_process_handle_is_pointer_sized(self):
        if os.name != "nt":
            self.skipTest("Windows process API")
        updater.wait_for_pid(os.getpid(), timeout=0)
        self.assertIs(ctypes.windll.kernel32.OpenProcess.restype, ctypes.c_void_p)

    def test_successful_install_removes_the_transaction_journal(self):
        self.package_files()
        for name in updater.REQUIRED_FILES:
            (self.install / name).write_bytes(b"old-" + name.encode())
        updater.install_package(self.package, self.install, restart=False)
        self.assertFalse((self.install / updater.JOURNAL_NAME).exists())
        self.assertEqual((self.install / "SkyHUD.exe").read_bytes(), b"new-hud")

    def test_interrupted_transaction_is_rolled_back_from_its_journal(self):
        backup = self.install / ".sky-hud-backups" / "crash"
        backup.mkdir(parents=True)
        for name in updater.REQUIRED_FILES:
            (backup / name).write_bytes(b"old-" + name.encode())
            (self.install / name).write_bytes(b"mixed-" + name.encode())
        journal = self.install / updater.JOURNAL_NAME
        journal.write_text('{"backup": "%s", "files": {"SkyHUD.exe": true, "SkyHUD-Updater.exe": true}}'
                           % str(backup).replace("\\", "\\\\"), encoding="utf-8")
        self.assertEqual(updater.recover_incomplete(str(self.install)), [])
        self.assertFalse(journal.exists())
        for name in updater.REQUIRED_FILES:
            self.assertEqual((self.install / name).read_bytes(), b"old-" + name.encode())

    def test_journal_ignores_paths_outside_the_allowlist(self):
        journal = self.install / updater.JOURNAL_NAME
        journal.write_text('{"backup": "%s", "files": {"../victim.txt": false}}'
                           % str(self.install).replace("\\", "\\\\"), encoding="utf-8")
        (self.base / "victim.txt").write_bytes(b"keep")
        updater.recover_incomplete(str(self.install))
        self.assertEqual((self.base / "victim.txt").read_bytes(), b"keep")

    def test_failed_rollback_keeps_the_journal_for_the_next_recovery(self):
        self.package_files()
        for name in updater.REQUIRED_FILES:
            (self.install / name).write_bytes(b"old-" + name.encode())
        real_replace = updater.os.replace

        def break_install_and_restore(source, target):
            target = Path(target)
            if target == self.install / "SkyHUD-Updater.exe":
                raise PermissionError("injected install failure")
            if target == self.install / "SkyHUD.exe" and str(source).endswith(".restore"):
                raise PermissionError("injected restore failure")
            return real_replace(source, target)

        with patch.object(updater.os, "replace", side_effect=break_install_and_restore):
            with self.assertRaises(RuntimeError) as caught:
                updater.install_package(self.package, self.install, restart=False)
        self.assertIn("Ошибка отката", str(caught.exception))
        self.assertTrue((self.install / updater.JOURNAL_NAME).exists())
        backups = list(self.install.glob(".sky-hud-backups/*/SkyHUD.exe"))
        self.assertTrue(backups)


if __name__ == "__main__":
    unittest.main()
