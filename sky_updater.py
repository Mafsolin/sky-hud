"""Safe sidecar updater for Sky HUD.

The running HUD starts this process and exits. The updater waits for the old
process, replaces only release-owned files, preserves the config and songs,
and rolls the files back if installation fails.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import zipfile

ALLOWED_FILES = {"SkyHUD.exe", "SkyHUD-Updater.exe", "README-PORTABLE.md"}
REQUIRED_FILES = {"SkyHUD.exe", "SkyHUD-Updater.exe"}
REPLACE_TIMEOUT = 5.0
MAX_PACKAGE_BYTES = 100 * 1024 * 1024
MAX_UNPACKED_BYTES = 200 * 1024 * 1024


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_digest(package, expected):
    value = str(expected or "").removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError("В релизе нет корректного SHA-256 пакета")
    if sha256(package).lower() != value.lower():
        raise ValueError("Не совпал SHA-256 пакета")
    return value.lower()


def safe_members(package: str | os.PathLike[str]) -> list[str]:
    if Path(package).stat().st_size > MAX_PACKAGE_BYTES:
        raise ValueError("Слишком большой пакет обновления")
    with zipfile.ZipFile(package) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)):
            raise ValueError("Повторяющиеся файлы в пакете обновления")
        for entry in entries:
            if entry.filename not in ALLOWED_FILES or entry.is_dir():
                raise ValueError("Запрещённый файл в пакете: %s" % entry.filename)
            if entry.flag_bits & 1 or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Зашифрованные файлы и ссылки запрещены")
            if not 0 < entry.file_size <= MAX_UNPACKED_BYTES:
                raise ValueError("Недопустимый размер файла в пакете")
        if sum(entry.file_size for entry in entries) > MAX_UNPACKED_BYTES:
            raise ValueError("Слишком большой распакованный пакет")
        missing = REQUIRED_FILES.difference(names)
        if missing:
            raise ValueError("В пакете нет: %s" % ", ".join(sorted(missing)))
    return names


def prepare_update(package, expected_sha256, work_root=None):
    """Verify before extracting the sidecar outside the installation directory."""
    verify_digest(package, expected_sha256)
    safe_members(package)
    work = Path(tempfile.mkdtemp(prefix="sky-hud-update-", dir=work_root))
    try:
        copied = work / "SkyHUD-Update.zip"
        shutil.copy2(package, copied)
        verify_digest(copied, expected_sha256)
        sidecar = work / "SkyHUD-Updater.exe"
        with zipfile.ZipFile(copied) as archive, archive.open(sidecar.name) as source, sidecar.open("xb") as target:
            shutil.copyfileobj(source, target)
        return str(work), str(copied), str(sidecar)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise


def wait_for_pid(pid: int, timeout: float = 30.0) -> bool:
    """Wait safely with pointer-sized Windows handles, never kill a process."""
    if not pid:
        return True
    if pid < 0:
        raise ValueError("Некорректный PID")
    if os.name == "nt":
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            error = kernel.GetLastError()
            if error == 87:
                return True
            raise OSError(error, "Не удалось дождаться завершения Sky HUD")
        try:
            return kernel.WaitForSingleObject(handle, max(0, int(timeout * 1000))) == 0
        finally:
            kernel.CloseHandle(handle)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def replace_with_retry(source, target):
    """The PyInstaller parent or antivirus can retain the EXE lock briefly."""
    deadline = time.monotonic() + REPLACE_TIMEOUT
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def install_package(package: str | os.PathLike[str], install_dir: str | os.PathLike[str],
                    restart: bool = True, expected_sha256=None) -> None:
    package = Path(package).resolve()
    install_dir = Path(install_dir).resolve()
    if expected_sha256 is not None:
        verify_digest(package, expected_sha256)
    members = safe_members(package)
    install_dir.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".sky-hud-stage-", dir=install_dir))
    backup_dir = None
    replaced = []
    existed = {name for name in members if (install_dir / name).exists()}
    try:
        # Check all CRCs before backing up or replacing files.
        with zipfile.ZipFile(package) as archive:
            for name in members:
                target = install_dir / name
                if target.is_symlink() or (target.exists() and not target.is_file()):
                    raise ValueError("Недопустимый целевой файл: %s" % name)
                with archive.open(name) as source, (stage / name).open("xb") as destination:
                    shutil.copyfileobj(source, destination)
        backup_root = install_dir / ".sky-hud-backups"
        backup_root.mkdir(exist_ok=True)
        backup_dir = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=backup_root))
        for name in members:
            if name in existed:
                shutil.copy2(install_dir / name, backup_dir / name)
        for name in members:
            replace_with_retry(stage / name, install_dir / name)
            replaced.append(name)
        if restart:
            process = subprocess.Popen([str(install_dir / "SkyHUD.exe")], cwd=install_dir,
                                       creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            time.sleep(0.8)
            if process.poll() is not None:
                raise RuntimeError("Новая версия завершилась сразу после запуска")
    except Exception as exc:
        recovery_errors = []
        for name in reversed(replaced):
            target = install_dir / name
            try:
                if name in existed:
                    # Keep the only backup even if recovery itself fails.
                    restored = stage / (name + ".restore")
                    shutil.copy2(backup_dir / name, restored)
                    replace_with_retry(restored, target)
                else:
                    target.unlink(missing_ok=True)
            except OSError as recovery_error:
                recovery_errors.append("%s: %s" % (name, recovery_error))
        if recovery_errors:
            raise RuntimeError("Ошибка отката: %s. Резервная копия: %s" %
                               ("; ".join(recovery_errors), backup_dir)) from exc
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    # Keep backups for manual recovery; never delete a user's input package.


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, default=0)
    parser.add_argument("--package", required=True)
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--no-restart", action="store_true")
    args = parser.parse_args()
    try:
        verify_digest(args.package, args.sha256)
        if not wait_for_pid(args.pid):
            raise RuntimeError("Старый Sky HUD не завершился вовремя")
        install_package(args.package, args.install_dir, restart=not args.no_restart,
                        expected_sha256=args.sha256)
        return 0
    except Exception as exc:
        try:
            Path(args.install_dir, "sky_hud_update_error.log").write_text(str(exc), encoding="utf-8")
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
