"""Safe sidecar updater for Sky HUD.

The running HUD starts this process and exits. The updater waits for the old
process, replaces only release-owned files, preserves the config and songs,
and rolls the files back if installation fails.
"""
from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import zipfile

ALLOWED_FILES = {"SkyHUD.exe", "SkyHUD-Updater.exe", "README-PORTABLE.md"}
REQUIRED_FILES = {"SkyHUD.exe", "SkyHUD-Updater.exe"}


def safe_members(package: str | os.PathLike[str]) -> list[str]:
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        for name in names:
            path = Path(name)
            if not name or name.endswith("/") or path.is_absolute() or ".." in path.parts:
                raise ValueError("Небезопасный путь в пакете обновления: %s" % name)
            if name not in ALLOWED_FILES:
                raise ValueError("Запрещённый файл в пакете обновления: %s" % name)
        missing = REQUIRED_FILES.difference(names)
        if missing:
            raise ValueError("В пакете нет: %s" % ", ".join(sorted(missing)))
    return names


def wait_for_pid(pid: int, timeout: float = 30.0) -> bool:
    """Wait until a Windows process disappears; return false on timeout."""
    if not pid:
        return True
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(0x100000, False, pid)
        if handle:
            try:
                result = ctypes.windll.kernel32.WaitForSingleObject(handle, int(timeout * 1000))
                return result == 0
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return True
        time.sleep(0.25)
    return False


def install_package(package: str | os.PathLike[str], install_dir: str | os.PathLike[str],
                   restart: bool = True) -> None:
    package = Path(package).resolve()
    install_dir = Path(install_dir).resolve()
    members = safe_members(package)
    install_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = Path(tempfile.mkdtemp(prefix="sky-hud-update-backup-"))
    try:
        for name in members:
            target = install_dir / name
            if target.exists():
                shutil.copy2(target, backup_dir / name)
        for name in members:
            target = install_dir / name
            staged = target.with_suffix(target.suffix + ".new")
            with zipfile.ZipFile(package) as archive, archive.open(name) as source, open(staged, "wb") as dest:
                shutil.copyfileobj(source, dest)
            os.replace(staged, target)
        if restart:
            subprocess.Popen([str(install_dir / "SkyHUD.exe")], cwd=install_dir,
                             creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except Exception:
        for name in members:
            target = install_dir / name
            backup = backup_dir / name
            try:
                if backup.exists():
                    os.replace(backup, target)
                elif target.exists() and name in REQUIRED_FILES:
                    target.unlink()
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(backup_dir, ignore_errors=True)
        try:
            package.unlink()
            package.parent.rmdir()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, default=0)
    parser.add_argument("--package", required=True)
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--no-restart", action="store_true")
    args = parser.parse_args()
    try:
        if not wait_for_pid(args.pid):
            raise RuntimeError("Старый Sky HUD не завершился вовремя")
        install_package(args.package, args.install_dir, restart=not args.no_restart)
        return 0
    except Exception as exc:
        try:
            Path(args.install_dir, "sky_hud_update_error.log").write_text(str(exc), encoding="utf-8")
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
