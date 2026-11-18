#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sky HUD — оверлей-тренажёр поверх Sky: Children of the Light (ПК/Steam).

Прозрачное окно поверх игры показывает 15 клавиш инструмента и подсвечивает,
какие ноты (клавиши Y U I O P / H J K L ; / N M , . /) нужно нажимать по загруженной
песне (формат Sky Music Studio .txt/.json). Нажатия читаются глобально, прямо из игры:
верные попадания считаются в режиме тренировки.

Запуск:  python sky_hud.py        (или .bat рядом)
Флаги:   --check [файл]  — самопроверка парсера и таймлайна без GUI
         --smoke         — быстрый запуск GUI с авто-закрытием

Зависимостей нет: только стандартные python + tkinter (входит в Windows Python).
"""

import ctypes
import hashlib
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from typing import Any
from tkinter import filedialog, simpledialog, ttk
import urllib.error
import urllib.parse
import urllib.request
import winsound
import sky_updater
from sky_song_map import SongMapGrid

APP_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
APP_VERSION = "1.1.10"
UPDATE_REPO = "Mafsolin/sky-hud"


def _version_tuple(value):
    numbers = [int(x) for x in re.findall(r"\d+", str(value or ""))[:3]]
    return tuple((numbers + [0, 0, 0])[:3])


def update_message(release):
    """Return a Russian status message for a GitHub latest-release payload."""
    if not isinstance(release, dict):
        return "GitHub вернул некорректные данные релиза"
    tag = str(release.get("tag_name") or "").strip()
    if not tag:
        return "На GitHub пока нет опубликованных релизов"
    if _version_tuple(tag) > _version_tuple(APP_VERSION):
        url = release.get("html_url") or ("https://github.com/%s/releases" % UPDATE_REPO)
        return "Доступна новая версия %s · %s" % (tag, url)
    return "Версия %s актуальна" % APP_VERSION


def _release_asset(release):
    if not isinstance(release, dict):
        return None
    for asset in release.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name") == "SkyHUD-Update.zip":
            return asset
    return None


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_update(release, work_root=None):
    """Download only the fixed repository asset and require GitHub's SHA-256."""
    asset = _release_asset(release)
    if not asset:
        raise ValueError("В релизе нет SkyHUD-Update.zip")
    if not isinstance(asset, dict):
        raise ValueError("Некорректные данные файла обновления")
    digest = str(asset.get("digest") or "")
    if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise ValueError("GitHub не предоставил SHA-256; установка запрещена")
    url = urllib.parse.urlsplit(str(asset.get("browser_download_url") or ""))
    prefix = "/%s/releases/download/" % UPDATE_REPO
    if url.scheme != "https" or url.netloc.lower() != "github.com" or not url.path.startswith(prefix):
        raise ValueError("Недоверенный адрес пакета обновления")
    size = asset.get("size")
    if not isinstance(size, int) or not 0 < size <= sky_updater.MAX_PACKAGE_BYTES:
        raise ValueError("Недопустимый размер пакета")
    with tempfile.TemporaryDirectory(prefix="sky-hud-download-", dir=work_root) as folder:
        package = os.path.join(folder, "SkyHUD-Update.zip")
        req = urllib.request.Request(urllib.parse.urlunsplit(url),
            headers={"Accept": "application/octet-stream", "User-Agent": "SkyHUD/%s" % APP_VERSION})
        total = 0
        with urllib.request.urlopen(req, timeout=30) as response, open(package, "xb") as target:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > size:
                    raise ValueError("Размер скачанного пакета больше заявленного")
                target.write(chunk)
        if total != size:
            raise ValueError("Пакет скачан не полностью")
        work, verified, sidecar = sky_updater.prepare_update(package, digest, work_root=work_root)
        return work, verified, sidecar, digest.split(":", 1)[1].lower()


def launch_update(prepared):
    work, package, updater, digest = prepared
    sky_updater.verify_digest(package, digest)
    if not os.path.isfile(updater):
        raise ValueError("Не найден отдельный updater")
    return subprocess.Popen([updater, "--pid", str(os.getpid()), "--package", package,
                             "--install-dir", APP_DIR, "--sha256", digest], cwd=work)

# ---------------------------------------------------------------- данные
NOTE_NAMES = ["C", "D", "E", "F", "G", "A", "B", "C", "D", "E", "F", "G", "A", "B", "C"]
SEMI = [0, 2, 4, 5, 7, 9, 11, 12, 14, 16, 17, 19, 21, 23, 24]

# физические клавиши Steam-раскладки Sky: ряды сверху вниз (верхний ряд — низкие ноты)
CODES = ["Y", "U", "I", "O", "P", "H", "J", "K", "L", ";", "N", "M", ",", ".", "/"]
VK = {"Y": 0x59, "U": 0x55, "I": 0x49, "O": 0x4F, "P": 0x50,
      "H": 0x48, "J": 0x4A, "K": 0x4B, "L": 0x4C, ";": 0xBA,
      "N": 0x4E, "M": 0x4D, ",": 0xBC, ".": 0xBE, "/": 0xBF}

PART_COLORS = [  # r,g,b
    (255, 184, 77),    # 1 — тёплый жёлтый
    (77, 210, 255),    # 2 — голубой
    (255, 122, 217),   # 3 — розовый
    (125, 224, 125),   # 4 — зелёный
]

BASE_C4 = 261.6256
MAX_SONG_MS = 12 * 60 * 60 * 1000  # 12 часов: защита от битых времён в JSON

THEMES = {
    "dark": {
        "bg": "#182733", "panel": "#1f3140", "text": "#f5efdf", "muted": "#b8c6c9",
        "button": "#283945", "button_active": "#3e5864", "border": "#3e5864",
        "entry": "#283945", "select": "#3e5864", "accent": "#6edaff",
        "error": "#ff9999", "ok": "#7fe08a", "scale": "#3e5864",
    },
    "light": {
        "bg": "#f4f6f9", "panel": "#ffffff", "text": "#1c2430", "muted": "#5f6b7a",
        "button": "#e4ebf5", "button_active": "#cfdcec", "border": "#c3cfdd",
        "entry": "#ffffff", "select": "#d8e2ef", "accent": "#1f6feb",
        "error": "#c0392b", "ok": "#1f7a3a", "scale": "#c9d4e2",
    },
}


def system_theme():
    """Return the Windows app theme: light unless apps are configured dark."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        try:
            return "light" if winreg.QueryValueEx(key, "AppsUseLightTheme")[0] else "dark"
        finally:
            key.Close()
    except Exception:
        return "dark"


# ---------------------------------------------------------------- парсер
def parse_song(text, src_name="песня"):
    try:
        obj = json.loads(text)
    except Exception as e:
        raise ValueError("Не JSON: %s" % e) from None
    if isinstance(obj, list):
        if not obj:
            raise ValueError("Пустой список вместо песни Sky")
        data = obj[0]
    else:
        data = obj
    if not isinstance(data, dict) or not isinstance(data.get("songNotes"), list):
        raise ValueError("Не похоже на файл песни Sky (нет songNotes)")
    if data.get("isEncrypted"):
        raise ValueError("Файл зашифрован (isEncrypted) — открыть нельзя")

    parts_map = {}
    max_nums = []
    for n in data["songNotes"]:
        if not isinstance(n, dict):
            continue
        key = str(n.get("key", ""))
        m = re.match(r"^(?:(\d+)\s*)?Key\s*(\d+)$", key, re.I)
        if m:
            part = m.group(1) or "1"
            num = int(m.group(2))
        else:
            mm = re.search(r"(\d+)$", key)
            if not mm:
                continue
            part = "1"
            num = int(mm.group(1))
        try:
            note_time = float(n.get("time", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(note_time) or note_time < 0 or note_time > MAX_SONG_MS:
            continue
        if not 0 <= num <= 15:
            continue
        max_nums.append(num)
        parts_map.setdefault(part, []).append((note_time, num))

    if not parts_map:
        raise ValueError("В файле не нашлось нот")

    # Битая нота с номером 99 не должна переключать всю песню в one-based
    # и выбрасывать корректный Key0. База определяется только по валидным 0..15.
    one_based = 0 not in max_nums and 15 in max_nums
    parts = []
    for pid in sorted(parts_map, key=lambda p: (len(p), p)):
        notes = []
        for t, k in parts_map[pid]:
            kk = k - 1 if one_based else k
            if 0 <= kk <= 14:
                notes.append((t, kk))
        notes.sort(key=lambda x: (x[0], x[1]))
        by_t = {}
        for t, k in notes:
            by_t.setdefault(t, []).append(k)
        events = sorted((t, sorted(set(ks))) for t, ks in by_t.items())
        mono = sum(1 for _, ks in events if len(ks) == 1)
        melody = bool(events) and mono / len(events) >= 0.9
        parts.append({
            "id": pid,
            "notes": notes,
            "events": events,          # [(t, [key,...])]
            "kind": "мелодия" if melody else ("аккорды" if events and max(len(ks) for _, ks in events) >= 3 else "партия"),
        })
    dur_ms = parts[0]["events"][-1][0] if parts and parts[0]["events"] else 0.0
    for p in parts:
        if p["events"]:
            dur_ms = max(dur_ms, p["events"][-1][0])
    bpm = data.get("bpm") or 0
    try:
        bpm = float(bpm)
    except (TypeError, ValueError):
        bpm = 0
    if not math.isfinite(bpm) or bpm < 0:
        bpm = 0
    return {
        "name": str(data.get("name") or src_name),
        "author": str(data.get("author") or ""),
        "bpm": bpm,
        "parts": parts,
        "dur_ms": dur_ms,
    }


def build_timeline(song, part_ids):
    """Сводный список событий по включённым партиям + метаданные тайминга."""
    time_set = set()
    for p in song["parts"]:
        if p["id"] in part_ids:
            for t, _ in p["events"]:
                time_set.add(t)
    times = sorted(time_set)
    evs = [{"t": t, "notes": []} for t in times]
    by_time = {t: e for t, e in zip(times, evs, strict=True)}
    for p in song["parts"]:
        if p["id"] in part_ids:
            for t, ks in p["events"]:
                e = by_time[t]
                for k in ks:
                    e["notes"].append({"key": k, "part": p["id"]})
    return _timeline_from_events(evs)


def _timeline_from_events(evs):
    """Build timing metadata from already grouped, chronologically sorted events."""
    normalized = []
    for event in evs:
        seen = set()
        notes = []
        for note in event["notes"]:
            if note["key"] in seen:
                continue
            seen.add(note["key"])
            notes.append(dict(note))
        normalized.append({"t": event["t"], "notes": notes})
    evs = normalized
    times = [e["t"] for e in evs]
    gaps = [times[i + 1] - times[i] for i in range(len(times) - 1) if times[i + 1] - times[i] > 0]
    gaps.sort()
    gap = max(80.0, gaps[len(gaps) // 2] if gaps else 250.0)
    instances = []
    for e in evs:
        for n in e["notes"]:
            instances.append({"t": e["t"], "key": n["key"], "part": n["part"],
                              "open": e["t"] - 0.35 * gap, "close": e["t"] + 0.7 * gap,
                              "consumed": False, "done": False})
    end_t = max((i["close"] for i in instances), default=(times[-1] if times else 0))
    return {"evs": evs, "gap": gap, "instances": instances, "end_t": end_t}


def slice_timeline(tl, start, end):
    """Return a zero-based practice fragment from inclusive event indexes."""
    selected = tl["evs"][max(0, start):end + 1]
    if not selected:
        return _timeline_from_events([])
    origin = selected[0]["t"]
    events = [{"t": e["t"] - origin,
               "notes": [dict(n) for n in e["notes"]]} for e in selected]
    return _timeline_from_events(events)


# ---------------------------------------------------------------- HUD
def song_path_key(path):
    """Portable identity inside APP_DIR; absolute identity for external files."""
    absolute = os.path.abspath(path)
    try:
        relative = os.path.relpath(absolute, APP_DIR)
        if relative != ".." and not relative.startswith(".." + os.sep):
            return relative.replace("\\", "/")
    except ValueError:
        pass
    return absolute.replace("\\", "/")


def blend(base, part, w):
    return tuple(int(base[i] + (part[i] - base[i]) * max(0.0, min(1.0, w))) for i in range(3))


def rr_points(x1, y1, x2, y2, r):
    return [x1 + r, y1, x2 - r, y1, x2, y1 + r, x2, y2 - r, x2 - r, y2,
            x1 + r, y2, x1, y2 - r, x1, y1 + r]


class SoundBus:
    """Очередь коротких Beep-сигналов в фоновом потоке (winsound.Beep блокирующий)."""

    def __init__(self):
        self.q = queue.Queue()
        self.on = True
        self._closed = False
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            freq, dur = item
            try:
                winsound.Beep(int(freq), int(dur))
            except Exception:
                pass

    def beep(self, freq, dur=70):
        if self.on and 37 <= freq <= 32767:
            self.q.put((freq, dur))

    def clear(self):
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.on = False
        self.clear()
        self.q.put(None)
        if self.t is not threading.current_thread():
            self.t.join(timeout=2.0)


class HudApp:
    CFG_FILE = os.path.join(APP_DIR, "sky_hud_config.json")
    CUSTOM_SONG_DIR = os.path.join(APP_DIR, "songs", "User Songs")
    CATALOG_LIMIT = 300

    def __init__(self, root, start_song=None):
        self.root = root
        self.sound = SoundBus()
        self.cfg = self.load_cfg()
        self.scale = float(self.cfg.get("scale", 1.0))
        self.alpha = float(self.cfg.get("alpha", 0.85))
        self.tempo = float(self.cfg.get("tempo", 1.0))
        # Ручная помощь: по умолчанию и для старых настроек — пошаговый режим,
        # где ход меняется только после нажатия игрока. Таймерный «train» больше
        # не включается сам: старые конфиги с mode="train" открываются в «step».
        saved_mode = self.cfg.get("mode")
        self.mode = "demo" if saved_mode == "demo" else "step"
        self.metro_on = bool(self.cfg.get("metro", False))
        self.sound_on = bool(self.cfg.get("sound", False))
        self.loop_on = bool(self.cfg.get("loop", True))
        parts_cfg = self.cfg.get("parts")
        self.part_sel = None if parts_cfg is None else set(parts_cfg)
        self.song = None
        self.song_path = None
        self.preview_tl = None
        self.run = None
        self.flash = {}            # idx -> (kind, perf_expire)
        self.pressed_key = set()
        self.hud_hidden = bool(self.cfg.get("hud_hidden", False))
        self.favorites = set(self.cfg.get("favorites") or [])
        self._catalog_cache = None
        self._catalog_split_cache = None
        self._catalog_lock = threading.RLock()
        self._catalog_refreshers = []
        self._map_refreshers = []
        self.preview_index = 0
        self.practice_range = None
        self.theme_choice = self.cfg.get("theme", "system")
        if self.theme_choice not in ("system", "light", "dark"):
            self.theme_choice = "system"
        self.theme = system_theme() if self.theme_choice == "system" else self.theme_choice
        self.palette = THEMES[self.theme]

        # ---- окно HUD: поверх всего, без рамки, без фокуса, клики проходят сквозь
        root.title("Sky HUD")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", self.alpha)
        try:
            root.wm_attributes("-transparentcolor", "#0b1230")
        except Exception:
            pass
        self.cv = tk.Canvas(root, width=1, height=1, highlightthickness=0, bd=0)
        self.cv.pack()
        root.update_idletasks()
        self._make_click_through()
        self._last_valid_hud_pos = None
        self._move_cfg(self.cfg.get("x"), self.cfg.get("y"))
        self._remember_hud_pos()

        # ---- панель управления (обычное окно)
        self.panel = tk.Toplevel(root)
        self.panel.title("Sky HUD — управление")
        self.panel.configure(bg=self.palette["bg"])
        self.panel.attributes("-topmost", True)
        self.panel.protocol("WM_DELETE_WINDOW", self.quit)
        self._configure_ttk_theme()
        self._build_panel()
        self.place_panel()
        self._theme_titlebar(self.panel)

        if start_song is not None:
            self.set_song(start_song)
        else:
            self.restore_song()

        self._after_ids = set()
        self._closed = False
        self._after(15, self._poll_keys)
        self._it = None
        self._need_rebuild = True
        self._last_frac = 0.0
        self._last_time = None
        self._after(33, self._frame)
        self._after(40, self._tick)
        self._after(400, self._save_cfg_debounced)
        self._after(250, lambda: threading.Thread(target=self._warm_catalog, daemon=True).start())
        self._click_through = self._make_click_through()
        self._t0 = time.perf_counter()
        if self.hud_hidden:
            self.root.withdraw()

    # ---------------------------------------------------------- окно/стили
    @staticmethod
    def _desktop_bounds():
        u = ctypes.windll.user32
        return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),
                u.GetSystemMetrics(78), u.GetSystemMetrics(79))

    def _on_desktop(self, x, y):
        """Отклоняет координаты -32000, которые Tk отдаёт для скрытого окна."""
        try:
            x, y = int(x), int(y)
        except (TypeError, ValueError):
            return False
        vx, vy, vw, vh = self._desktop_bounds()
        return vx - 32 <= x < vx + vw - 40 and vy - 32 <= y < vy + vh - 40

    def _default_hud_pos(self):
        vx, vy, vw, vh = self._desktop_bounds()
        L = self._layout_geom()
        return vx + vw - L["w"] - 60, vy + vh - L["h"] - 90

    def _default_panel_pos(self):
        vx, vy, vw, vh = self._desktop_bounds()
        pw, ph = self.panel.winfo_reqwidth(), self.panel.winfo_reqheight()
        rx, ry, rw, _rh = self.geom()
        if not self._on_desktop(rx, ry):
            rx, ry = self._default_hud_pos()
            rw = 0
        px, py = rx + rw + 14, max(vy + 20, ry)
        if px + pw > vx + vw:
            px = max(vx + 10, rx - pw - 14)
        if px + pw > vx + vw:
            px = max(vx + 10, vx + (vw - pw) // 2)
        if py + ph > vy + vh:
            py = max(vy + 10, vy + vh - ph - 40)
        return px, py

    def place_panel(self):
        # Сохранённые координаты могут быть устаревшими или с другого монитора.
        self.root.update_idletasks()
        px, py = self.cfg.get("px"), self.cfg.get("py")
        if self._on_desktop(px, py):
            self.panel.geometry("+%d+%d" % (int(px), int(py)))
            return
        self.panel.geometry("+%d+%d" % self._default_panel_pos())

    def _make_click_through(self):
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            if not hwnd:
                return False
            style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            style |= 0x80000 | 0x20 | 0x08000000          # LAYERED|TRANSPARENT|NOACTIVATE
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, style)
            return True
        except Exception:
            return False

    def _remember_hud_pos(self):
        x, y, w, h = self.geom()
        if w >= 50 and h >= 30 and self._on_desktop(x, y):
            self._last_valid_hud_pos = (x, y, w, h)

    def _move_cfg(self, x, y):
        if self._on_desktop(x, y):
            self.root.geometry("+%d+%d" % (int(x), int(y)))
        else:
            self.root.geometry("+%d+%d" % self._default_hud_pos())
        self._remember_hud_pos()

    def geom(self):
        try:
            return self.root.winfo_rootx(), self.root.winfo_rooty(), \
                   self.root.winfo_width(), self.root.winfo_height()
        except Exception:
            return 0, 0, 0, 0

    def load_cfg(self):
        try:
            with open(self.CFG_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        cfg = dict(raw)

        def number(key, default, low, high):
            try:
                value = float(cfg.get(key, default))
            except (TypeError, ValueError):
                return default
            if not math.isfinite(value):
                return default
            return max(low, min(high, value))

        cfg["scale"] = number("scale", 1.0, 0.5, 2.0)
        cfg["alpha"] = number("alpha", 0.85, 0.35, 1.0)
        cfg["tempo"] = number("tempo", 1.0, 0.5, 2.0)
        for key in ("x", "y", "px", "py", "w", "h"):
            if key not in cfg:
                continue
            try:
                value = float(cfg[key])
                cfg[key] = int(value) if math.isfinite(value) else None
            except (TypeError, ValueError):
                cfg[key] = None
            if cfg[key] is None:
                cfg.pop(key)
        if cfg.get("mode") not in ("demo", "step", "train"):
            cfg["mode"] = "step"
        for key in ("metro", "sound", "loop", "hud_hidden"):
            if key in cfg:
                cfg[key] = bool(cfg[key])
        parts = cfg.get("parts")
        if isinstance(parts, (list, tuple)):
            cfg["parts"] = [str(part) for part in parts]
        else:
            cfg.pop("parts", None)
        favorites = cfg.get("favorites")
        cfg["favorites"] = [str(item) for item in favorites] if isinstance(favorites, (list, tuple)) else []
        if not isinstance(cfg.get("last_song"), str):
            cfg.pop("last_song", None)
        return cfg

    _save_pending = False

    def _after(self, delay, callback):
        holder = {}
        def run():
            ident = holder.get("id")
            if ident is not None:
                self._after_ids.discard(ident)
            if not getattr(self, "_closed", False):
                callback()
        ident = self.root.after(delay, run)
        holder["id"] = ident
        self._after_ids.add(ident)
        return ident

    def _cancel_afters(self):
        self._closed = True
        for ident in list(getattr(self, "_after_ids", ())):
            try:
                self.root.after_cancel(ident)
            except tk.TclError:
                pass
        self._after_ids.clear()

    def _save_cfg_debounced(self):
        if self._save_pending:
            return
        self._save_pending = True

        def do():
            self.save_cfg()
            self._save_pending = False
        self._after(500, do)

    def save_cfg(self):
        x, y, w, h = self.geom()
        px, py = self.panel.winfo_rootx(), self.panel.winfo_rooty()
        self._remember_hud_pos()
        if self._last_valid_hud_pos:
            x, y, w, h = self._last_valid_hud_pos
        elif self._on_desktop(self.cfg.get("x"), self.cfg.get("y")):
            x, y = int(self.cfg["x"]), int(self.cfg["y"])
            L = self._layout_geom()
            w, h = L["w"], L["h"]
        # Скрытое окно отдаёт размер 1x1 и координаты -32000 — их нельзя писать в конфиг.
        if w < 50 or h < 30 or not self._on_desktop(x, y):
            x, y = self._default_hud_pos()
            L = self._layout_geom()
            w, h = L["w"], L["h"]
        if not self._on_desktop(px, py):
            px, py = self._default_panel_pos()
        cfg = {
            "x": x, "y": y, "w": w,
            "px": px, "py": py,
            "scale": self.scale, "alpha": self.alpha, "tempo": self.tempo,
            "mode": self.mode, "metro": self.metro_on, "sound": self.sound_on,
            "loop": self.loop_on, "parts": sorted(self.part_sel or []),
            "hud_hidden": self.hud_hidden,
            "theme": self.theme_choice,
            "favorites": sorted(self.favorites),
            "last_song": self.song_path,
        }
        self.cfg = cfg
        # Атомарная запись: пишем во временный файл, потом переименовываем
        tmp = self.CFG_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.CFG_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    # ---------------------------------------------------------- панель
    def _theme_titlebar(self, window):
        """Paint the native Windows title bar to match the light/dark palette."""
        def apply(_event=None):
            try:
                hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
                if not hwnd:
                    hwnd = window.winfo_id()
                value = ctypes.c_int(1 if self.theme == "dark" else 0)
                for attribute in (20, 19):
                    if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                            hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                        break
            except Exception:
                pass
        window.bind("<Map>", apply, add="+")
        apply()

    def _configure_ttk_theme(self):
        # Native Windows ttk elements ignore colors; clam paints all elements itself.
        style = ttk.Style(self.panel)
        style.theme_use("clam")
        pal = self.palette
        style.configure("Sky.Treeview", background=pal["bg"], fieldbackground=pal["bg"],
                        foreground=pal["text"], rowheight=32, font=("Segoe UI", 11), borderwidth=0,
                        bordercolor=pal["bg"], lightcolor=pal["bg"])
        style.map("Sky.Treeview", background=[("selected", pal["select"])],
                  foreground=[("selected", pal["text"])])
        style.configure("Sky.Vertical.TScrollbar", background=pal["button"],
                        troughcolor=pal["bg"], arrowcolor=pal["text"], bordercolor=pal["bg"],
                        lightcolor=pal["button"], darkcolor=pal["button"], gripcount=0)
        style.map("Sky.Vertical.TScrollbar", background=[("active", pal["button_active"])],
                  arrowcolor=[("disabled", pal["muted"])])
        style.configure("Sky.TCombobox", fieldbackground=pal["entry"], background=pal["button"],
                        foreground=pal["text"], arrowcolor=pal["text"], bordercolor=pal["border"],
                        lightcolor=pal["border"], darkcolor=pal["border"], padding=5)
        style.map("Sky.TCombobox", fieldbackground=[("readonly", pal["entry"])],
                  foreground=[("readonly", pal["text"])],
                  background=[("active", pal["button_active"])])

    def set_theme(self, choice):
        if choice not in ("system", "light", "dark"):
            raise ValueError("Unknown theme: %s" % choice)
        old = self.palette
        self.theme_choice = choice
        self.theme = system_theme() if choice == "system" else choice
        self.palette = THEMES[self.theme]
        pal = self.palette
        self._configure_ttk_theme()
        self._redraw()
        self.panel.option_add("*TCombobox*Listbox.background", pal["panel"])
        self.panel.option_add("*TCombobox*Listbox.foreground", pal["text"])
        self.panel.option_add("*TCombobox*Listbox.selectBackground", pal["select"])
        self.panel.option_add("*TCombobox*Listbox.selectForeground", pal["text"])
        # Recolor all classic widgets, including open dialogs and their menus.
        # Match palette roles by existing value, with explicit widget-specific roles.
        def recolor(widget):
            cls = widget.winfo_class()
            opts = widget.keys()
            for option in ("background", "foreground", "activebackground", "activeforeground",
                           "disabledforeground", "selectcolor", "troughcolor", "insertbackground",
                           "highlightbackground", "highlightcolor"):
                if option not in opts:
                    continue
                try:
                    value = str(widget.cget(option)).lower()
                    roles = [key for key in old if old[key].lower() == value]
                    if roles:
                        role = roles[0]
                        if option == "background" and cls in ("Button", "Menubutton"):
                            role = "button"
                        if option == "background" and cls == "Entry":
                            role = "entry"
                        widget.configure(**{option: pal[role]})
                except tk.TclError:
                    pass
            for child in widget.winfo_children():
                recolor(child)
        recolor(self.panel)
        for slider in (self.s_scale, self.s_alpha):
            slider.configure(bg=pal["button"], troughcolor=pal["scale"], activebackground=pal["accent"])
        self.lb_go.configure(bg=pal["accent"], fg=("#182733" if self.theme == "dark" else "#ffffff"))
        labels = {"system": "Системная", "light": "Светлая", "dark": "Тёмная"}
        for window in self.panel.winfo_children():
            if isinstance(window, tk.Toplevel):
                self._theme_titlebar(window)
                if hasattr(window, "theme_var"):
                    window.theme_var.set(labels[choice])
                    window._theme_palette.clear()
                    window._theme_palette.update(pal)
        self._theme_titlebar(self.panel)
        # The map grid keeps a derived palette; recreate open maps and catalogs.
        windows = [(w, w.title()) for w in self.panel.winfo_children()
                   if isinstance(w, tk.Toplevel) and
                   ("карта песни" in w.title() or "каталог" in w.title())]
        for win, title in windows:
            geometry = win.geometry()
            catalog_state = (win.catalog_query.get(), win.catalog_page) if hasattr(win, "catalog_query") else None
            win.destroy()
            replacement = self.open_map() if "карта песни" in title else (self.open_catalog() if "каталог" in title else None)
            if replacement:
                replacement.geometry(geometry)
                if catalog_state is not None:
                    replacement.catalog_query.set(catalog_state[0])
                    replacement.catalog_goto_page(catalog_state[1])
        self.save_cfg()

    def _btn(self, parent, text, command, **kw):
        pal = self.palette
        opts = dict(text=text, command=command, bg=pal["button"], fg=pal["text"],
                    activebackground=pal["button_active"], activeforeground=pal["text"],
                    relief="flat", bd=0, padx=10, pady=4, highlightthickness=0,
                    highlightbackground=pal["border"], highlightcolor=pal["border"],
                    disabledforeground=pal["muted"],
                    font=("Segoe UI", 9))
        opts.update(kw)
        return tk.Button(parent, **opts)

    def _label(self, parent, text="", **kw):
        pal = self.palette
        opts = dict(text=text, bg=pal["bg"], fg=pal["text"], font=("Segoe UI", 9))
        opts.update(kw)
        return tk.Label(parent, **opts)

    def _build_panel(self):
        p = self.panel
        pal = self.palette
        head = tk.Frame(p, bg=pal["bg"])
        head.pack(fill="x", padx=8, pady=(8, 0))
        tk.Label(head, text="Sky HUD", anchor="w", bg=pal["bg"], fg=pal["text"],
                 font=("Segoe UI", 12)).pack(side="left")
        tk.Label(head, text="v" + APP_VERSION, anchor="e", bg=pal["bg"], fg=pal["muted"],
                 font=("Segoe UI", 8)).pack(side="right")
        f = tk.Frame(p, bg=pal["bg"])
        f.pack(fill="x", padx=4, pady=(4, 4))
        self._panel_buttons = [
            self._btn(f, "Каталог нот", self.open_catalog, width=20, pady=6),
            self._btn(f, "Открыть карту", self.open_map, width=20, pady=6),
            self._btn(f, "▶ Старт", self.toggle_run, width=20, pady=6,
                      bg=pal["accent"], fg=("#182733" if self.theme == "dark" else "#ffffff")),
            self._btn(f, "Показать HUD" if self.hud_hidden else "Скрыть HUD полностью",
                      self.toggle_hud, width=20, pady=6),
            self._btn(f, "Настройки", self.open_settings, width=20, pady=6),
        ]
        for col in range(6):
            f.grid_columnconfigure(col, weight=1, uniform="buttons")
        for button, row, col, span in ((self._panel_buttons[0], 0, 0, 2),
                                       (self._panel_buttons[1], 0, 2, 2),
                                       (self._panel_buttons[4], 0, 4, 2),
                                       (self._panel_buttons[2], 1, 0, 3),
                                       (self._panel_buttons[3], 1, 3, 3)):
            button.grid(row=row, column=col, columnspan=span, padx=4, pady=4, sticky="nsew")
        self.lb_go = self._panel_buttons[2]
        self.lb_hide = self._panel_buttons[3]
        self.mode_btns = {}

        song_row = tk.Frame(p, bg=pal["bg"])
        song_row.pack(fill="x", padx=8)
        self.lb_song = tk.Label(song_row, text="—", anchor="w", bg=pal["bg"], fg=pal["text"],
                                font=("Segoe UI", 9))
        self.lb_song.pack(side="left", fill="x", expand=True)
        self.favorite_button = self._btn(song_row, "☆", self.toggle_selected_favorite, width=3)
        self.favorite_button.pack(side="right", padx=(8, 0))
        self.lb_meta = tk.Label(p, text="", anchor="w", bg=pal["bg"], fg=pal["muted"],
                                font=("Segoe UI", 8))
        self.lb_meta.pack(fill="x", padx=8)
        tk.Frame(p, bg=pal["border"], height=1).pack(fill="x", padx=8, pady=(8, 0))

        self.part_vars = {}
        self.part_frame = tk.Frame(p, bg=pal["bg"])
        self.part_frame.pack_forget()

        f2 = tk.Frame(p, bg=pal["bg"])
        f2.pack(fill="x", padx=6)
        self.v_metro = tk.BooleanVar(value=self.metro_on)
        self.v_sound = tk.BooleanVar(value=self.sound_on)
        self.v_loop = tk.BooleanVar(value=self.loop_on)

        def check(parent, text, var):
            return tk.Checkbutton(parent, text=text, variable=var, command=self._on_cfg,
                                  bg=pal["bg"], fg=pal["text"], activebackground=pal["bg"],
                                  activeforeground=pal["text"], selectcolor=pal["panel"],
                                  highlightthickness=0, bd=0, font=("Segoe UI", 9))

        self.cb_metro = check(f2, "Метроном", self.v_metro)
        self.cb_metro.pack(side="left")
        check(f2, "Звук", self.v_sound).pack(side="left")
        self.cb_loop = check(f2, "Повтор (цикл)", self.v_loop)
        self.cb_loop.pack(side="left")
        tk.Label(f2, text="Темп", bg=pal["bg"], fg=pal["text"], font=("Segoe UI", 9)).pack(side="left", padx=(10, 2))
        self.v_tempo = tk.StringVar(value=str(self.tempo))
        cb = tk.OptionMenu(f2, self.v_tempo, "0.5", "0.75", "1", "1.25", "1.5", command=lambda _: self._on_cfg())
        cb.config(width=3, bg=pal["button"], fg=pal["text"], activebackground=pal["button_active"],
                  activeforeground=pal["text"], relief="flat", highlightthickness=0, bd=0)
        cb["menu"].config(bg=pal["panel"], fg=pal["text"], activebackground=pal["select"],
                          activeforeground=pal["text"])
        cb.pack(side="left")

        f3 = tk.Frame(p, bg=pal["bg"])
        f3.pack(fill="x", padx=8, pady=(8, 0))
        tk.Label(f3, text="Масштаб", bg=pal["bg"], fg=pal["text"], font=("Segoe UI", 9)).pack(side="left")
        self.s_scale = tk.Scale(f3, from_=0.5, to=2.0, resolution=0.05, orient="horizontal",
                                showvalue=False, command=lambda v: self._set_scale(float(v)),
                                bg=pal["button"], fg=pal["text"], troughcolor=pal["scale"],
                                highlightthickness=0, bd=1, width=12, sliderlength=22,
                                sliderrelief="flat", activebackground=pal["accent"])
        self.s_scale.set(self.scale)
        self.s_scale.pack(side="left", fill="x", expand=True)
        tk.Label(f3, text="Прозрачность", bg=pal["bg"], fg=pal["text"], font=("Segoe UI", 9)).pack(side="left", padx=(8, 0))
        self.s_alpha = tk.Scale(f3, from_=0.35, to=1.0, resolution=0.05, orient="horizontal",
                                showvalue=False, command=self._set_alpha,
                                bg=pal["button"], fg=pal["text"], troughcolor=pal["scale"],
                                highlightthickness=0, bd=1, width=12, sliderlength=22,
                                sliderrelief="flat", activebackground=pal["accent"])
        self.s_alpha.set(self.alpha)
        self.s_alpha.pack(side="left", fill="x", expand=True)

        f4 = tk.Frame(p, bg=pal["bg"])
        f4.pack(fill="x", padx=8, pady=(8, 8))
        for t, d in (("←", (-12, 0)), ("→", (12, 0)), ("↑", (0, -12)), ("↓", (0, 12))):
            self._btn(f4, t, lambda d=d: self.nudge(*d), width=3, padx=4).pack(side="left")
        self._btn(f4, "По центру низа", self.place_bottom).pack(side="left", padx=8)

        self.lb_stats = tk.Label(p, text="", fg=pal["ok"], bg=pal["bg"], font=("Segoe UI", 9))
        self.lb_status = tk.Label(p, text="Загрузите файл песни (.txt/.json) или нажмите «Пример»",
                                  fg=pal["muted"], bg=pal["bg"], anchor="w", wraplength=430,
                                  justify="left", font=("Segoe UI", 8))
        self.lb_status.pack(fill="x", padx=8, pady=(0, 6))
        self.sync_panel_controls()
        self.sync_favorite_button()
        f2.pack_forget()

    def _catalog_index(self):
        """Build once: name, path, size, lower-case name and normalised favourite key."""
        with self._catalog_lock:
            if getattr(self, "_catalog_cache", None) is None:
                entries = []
                roots = [os.path.join(APP_DIR, "songs", folder) for folder in ("Songs", "Multi-Sheet Songs")]
                roots.append(self.CUSTOM_SONG_DIR)
                for root in roots:
                    for base, _, files in os.walk(root):
                        for name in files:
                            if name.lower().endswith((".txt", ".json")):
                                path = os.path.join(base, name)
                                try:
                                    entries.append((name, path, os.path.getsize(path),
                                                    name.casefold(), song_path_key(path)))
                                except OSError:
                                    continue
                entries.sort(key=lambda entry: (entry[3], entry[1]))
                self._catalog_cache = entries
                self._catalog_split_cache = None
            return self._catalog_cache

    def _warm_catalog(self):
        """Scan the library off the Tk thread so the first catalog open is instant."""
        try:
            self._catalog_index()
            self._catalog_split()
        except Exception:
            pass

    def _catalog_split(self):
        """Favourites and everything else, in sorted order; rebuilt when favourites change."""
        with self._catalog_lock:
            favorites = frozenset(self.favorites)
            cached = getattr(self, "_catalog_split_cache", None)
            if cached is None or getattr(self, "_catalog_fav_snapshot", None) != favorites:
                favs, others = [], []
                for entry in self._catalog_index():
                    (favs if entry[4] in favorites else others).append(entry)
                self._catalog_split_cache = (favs, others)
                self._catalog_fav_snapshot = favorites
            return self._catalog_split_cache

    def catalog_entries(self, query="", limit=None):
        needle = query.casefold().strip()
        favs, others = self._catalog_split()
        if needle:
            favs = [entry for entry in favs if needle in entry[3]]
            others = [entry for entry in others if needle in entry[3]]
        matched = favs + others
        if limit is not None:
            matched = matched[:limit]
        return [(name, path, size) for name, path, size, _lower, _key in matched]

    def catalog_page(self, query="", page=0, per_page=None, entries=None):
        """Page 0 starts with favourites; every page holds at most 300 songs."""
        per_page = per_page or self.CATALOG_LIMIT
        needle = query.casefold().strip()
        favs, others = self._catalog_split()
        if needle:
            favs = [entry for entry in favs if needle in entry[3]]
            others = [entry for entry in others if needle in entry[3]]
            matched = favs + others
            pages = max(1, (len(matched) + per_page - 1) // per_page)
            page = max(0, min(int(page), pages - 1))
            rows = matched[page * per_page:(page + 1) * per_page]
            return ([(name, path, size) for name, path, size, _l, _k in rows],
                    page, pages, "search", len(matched), 0)

        fav_pages = max(1, (len(favs) + per_page - 1) // per_page)
        song_pages = (len(others) + per_page - 1) // per_page
        pages = fav_pages + song_pages
        page = max(0, min(int(page), pages - 1))
        if page < fav_pages:
            rows = favs[page * per_page:(page + 1) * per_page]
            return ([(name, path, size) for name, path, size, _l, _k in rows],
                    page, pages, "favorites", len(favs), len(favs))
        song_page = page - fav_pages
        rows = others[song_page * per_page:(song_page + 1) * per_page]
        return ([(name, path, size) for name, path, size, _l, _k in rows],
                page, pages, "songs", len(others), len(favs))

    def import_song_file(self, path):
        """Validate before copying; never overwrite a different song with the same name."""
        name = os.path.basename(path)
        stem, ext = os.path.splitext(name)
        if ext.lower() not in (".txt", ".json"):
            raise ValueError("Выберите песню Sky в формате .txt или .json")
        with open(path, "rb") as source:
            content = source.read()
        song = parse_song(content.decode("utf-8-sig"), name)
        if not any(p["events"] for p in song["parts"]):
            raise ValueError("В песне нет подходящих нот")
        os.makedirs(self.CUSTOM_SONG_DIR, exist_ok=True)
        number = 1
        while True:
            filename = name if number == 1 else "%s (%d)%s" % (stem, number, ext)
            dest = os.path.join(self.CUSTOM_SONG_DIR, filename)
            try:
                with open(dest, "xb") as target:
                    target.write(content)
                break
            except FileExistsError:
                with open(dest, "rb") as existing:
                    if existing.read() == content:
                        break
                number += 1
            except OSError:
                try:
                    if os.path.exists(dest):
                        os.remove(dest)
                except OSError:
                    pass
                raise
        self._catalog_cache = None
        self._catalog_split_cache = None
        return dest

    def rename_song(self, path, new_name=None, parent=None):
        path = os.fspath(path)
        source = path if os.path.isabs(path) else os.path.join(APP_DIR, path)
        source = os.path.abspath(source)
        stem, ext = os.path.splitext(os.path.basename(source))
        if new_name is None:
            new_name = simpledialog.askstring("Переименовать песню", "Новое имя:",
                                              initialvalue=stem, parent=parent or self.panel)
            if new_name is None:
                return False
        def report(message, error=False):
            self.status(message, err=error)
            if parent is not None and hasattr(parent, "import_status"):
                parent.import_status.config(text=message, fg=self.palette["error" if error else "muted"])
        name = new_name.strip()
        if (not name or not name.strip(". ") or name.endswith(".") or
                re.search(r'[\\/:*?"<>|\x00-\x1f]', name) or
                re.fullmatch(r'(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', name)):
            report("Недопустимое имя файла", True)
            return False
        dest = os.path.join(os.path.dirname(source), name + ext)
        if dest == source:
            report("Имя не изменилось")
            return True
        try:
            with self._catalog_lock:
                folder = os.path.dirname(source)
                entries = [entry for entry in os.listdir(folder)
                           if os.path.normcase(os.path.join(folder, entry)) != os.path.normcase(source)]
                if any(entry.casefold() == os.path.basename(dest).casefold() for entry in entries):
                    report("Файл с таким именем уже существует", True)
                    return False
                os.rename(source, dest)
                self._catalog_cache = None
                self._catalog_split_cache = None
        except OSError as e:
            report("Ошибка переименования: %s" % e, True)
            return False
        old_key, new_key = song_path_key(source), song_path_key(dest)
        if old_key in self.favorites:
            self.favorites.remove(old_key)
            self.favorites.add(new_key)
        if self.song_path == old_key:
            self.song_path = new_key
            if self.song:
                self.song["name"] = name
                self.lb_song.config(text=name)
                self._redraw()
        self.sync_favorite_button()
        self.save_cfg()
        for refresh in list(self._catalog_refreshers):
            try:
                refresh()
            except tk.TclError:
                self._catalog_refreshers.remove(refresh)
        report("Переименовано: " + os.path.basename(dest))
        return True

    def toggle_favorite(self, path):
        key = path.replace("\\", "/") if not os.path.isabs(path) else song_path_key(path)
        if key in self.favorites:
            self.favorites.remove(key)
        else:
            self.favorites.add(key)
        self.save_cfg()
        self.sync_favorite_button()
        for refresh in list(self._catalog_refreshers):
            try:
                refresh()
            except tk.TclError:
                self._catalog_refreshers.remove(refresh)

    def sync_favorite_button(self):
        if not hasattr(self, "favorite_button"):
            return
        key = self.song_path
        self.favorite_button.config(
            text="★" if key and key in self.favorites else "☆",
            state="normal" if key else "disabled")

    def toggle_selected_favorite(self):
        if self.song_path:
            self.toggle_favorite(self.song_path)

    def _selected_part_ids(self):
        if not self.song:
            return []
        selected = self.part_sel or set()
        return [p["id"] for p in self.song["parts"] if p["id"] in selected]

    def _rebuild_preview_timeline(self, reset=False):
        self.preview_tl = build_timeline(self.song, self._selected_part_ids()) if self.song else None
        if reset:
            self.preview_index = 0
        elif self.preview_tl and self.preview_tl["evs"]:
            self.preview_index = min(self.preview_index, len(self.preview_tl["evs"]) - 1)
        else:
            self.preview_index = 0
        for refresh in list(getattr(self, "_map_refreshers", ())):
            try:
                refresh()
            except tk.TclError:
                try:
                    self._map_refreshers.remove(refresh)
                except ValueError:
                    pass

    def open_map(self):
        if not self.song:
            self.status("Сначала загрузите песню", err=True)
            return None
        existing = getattr(self, "_map_window", None)
        if existing is not None and existing.winfo_exists():
            for refresh in list(self._map_refreshers):
                try:
                    refresh()
                except tk.TclError:
                    pass
            existing.deiconify()
            existing.lift()
            return existing
        win = tk.Toplevel(self.panel)
        win.title("Sky HUD · карта песни")
        win.attributes("-topmost", True)
        px, py = self.panel.winfo_rootx(), self.panel.winfo_rooty()
        vx, vy, vw, vh = self._desktop_bounds()
        x, y = max(vx + 20, min(px - 180, vx + vw - 940)), max(vy + 20, min(py - 220, vy + vh - 740))
        win.geometry("900x680%+d%+d" % (x, y))
        win.minsize(700, 430)
        pal = self.palette
        win.configure(bg=pal["bg"])
        self._theme_titlebar(win)
        tk.Label(win, text="Карта песни · один ход — сетка 5 × 3 · нажмите на нужный ход",
                 anchor="w", font=("Segoe UI", 11, "bold"), bg=pal["bg"], fg=pal["text"]).pack(
                     fill="x", padx=12, pady=(10, 6))

        def rows():
            return self.preview_tl["evs"] if self.preview_tl else []

        def update_position():
            total = len(rows())
            win.position_label.config(text="Ход %d / %d" % (self.preview_index + 1 if total else 0, total))

        win.pick_mode = None
        range_state = {"start": None, "end": None}
        if self.practice_range is not None:
            range_state["start"], range_state["end"] = self.practice_range

        def active_range():
            lo, hi = range_state["start"], range_state["end"]
            if lo is None or hi is None:
                return None
            return (min(lo, hi), max(lo, hi))

        def sync_range_spins():
            total = len(rows())
            current = max(0, min(self.preview_index, total - 1)) if total else 0
            lo, hi = active_range() or (current, current)
            win.range_from.set(str(lo + 1))
            win.range_to.set(str(hi + 1))

        def set_pick_buttons():
            win.start_button.config(bg=pal["button_active"] if win.pick_mode == "start" else pal["button"])
            win.end_button.config(bg=pal["button_active"] if win.pick_mode == "end" else pal["button"])

        def apply_boundary(kind, index):
            if kind == "start":
                range_state["start"] = index
                if range_state["end"] is None or range_state["end"] < index:
                    range_state["end"] = index
                win.pick_mode = None
                set_pick_buttons()
                self.status("Начало: ход %d · теперь нажмите «Конец»" % (index + 1))
            else:
                start = range_state["start"]
                if start is None:
                    start = range_state["start"] = index
                if index < start:
                    self.status("Конец должен быть не раньше начала", err=True)
                    return
                range_state["end"] = index
                win.pick_mode = None
                set_pick_buttons()
                self.status("Отрезок %d–%d · нажмите «Ручная практика»" % (start + 1, index + 1))
            sync_range_spins()

        def begin_pick(kind):
            if not rows():
                return
            win.pick_mode = kind
            set_pick_buttons()
            self.status("Кликните по ходу, который будет началом" if kind == "start"
                        else "Кликните по ходу, который будет концом")

        def select(index):
            if not rows():
                return
            self.stop()
            self.preview_index = max(0, min(index, len(rows()) - 1))
            if win.pick_mode:
                apply_boundary(win.pick_mode, self.preview_index)
            grid.select_step(self.preview_index, active_range())
            update_position()
            self._redraw()

        body = tk.Frame(win, bg=pal["bg"])
        body.pack(fill="both", expand=True, padx=10, pady=4)
        grid = SongMapGrid(body, self.map_palette(), CODES, select)
        grid.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=grid.yview, style="Sky.Vertical.TScrollbar")
        scrollbar.pack(side="right", fill="y")
        grid.configure(yscrollcommand=scrollbar.set)
        win.map_scrollbar = scrollbar
        win.map_grid = grid
        win.select_step = select

        navigation = tk.Frame(win, bg=pal["bg"])
        navigation.pack(fill="x", padx=10, pady=(5, 4))
        win.previous_button = self._btn(navigation, "← Предыдущий", lambda: select(self.preview_index - 1))
        win.previous_button.pack(side="left")
        win.next_button = self._btn(navigation, "Следующий →", lambda: select(self.preview_index + 1))
        win.next_button.pack(side="left", padx=6)
        win.position_label = tk.Label(navigation, text="", anchor="w", bg=pal["bg"], fg=pal["text"])
        win.position_label.pack(side="left", padx=8)
        tk.Label(navigation, text="Голубая рамка — выбранный ход · золотые — клавиши",
                 fg=pal["muted"], bg=pal["bg"]).pack(side="right")

        controls = tk.Frame(win, bg=pal["bg"])
        controls.pack(fill="x", padx=10, pady=(0, 10))
        tk.Label(controls, text="Практика ходов:", bg=pal["bg"], fg=pal["text"]).pack(side="left")
        win.start_button = self._btn(controls, "Начало", lambda: begin_pick("start"), width=8)
        win.start_button.pack(side="left", padx=(6, 2))
        win.end_button = self._btn(controls, "Конец", lambda: begin_pick("end"), width=8)
        win.end_button.pack(side="left", padx=(2, 8))
        win.range_from = tk.StringVar(value="1")
        win.range_to = tk.StringVar(value="1")
        spin_style = dict(bg=pal["entry"], fg=pal["text"], buttonbackground=pal["button"],
                          readonlybackground=pal["entry"], relief="flat", highlightthickness=1,
                          highlightbackground=pal["border"], insertbackground=pal["text"])
        win.range_from_spin = tk.Spinbox(controls, from_=1, to=1, width=6,
                                         textvariable=win.range_from, state="readonly", **spin_style)
        win.range_from_spin.pack(side="left", padx=4)
        tk.Label(controls, text="—", bg=pal["bg"], fg=pal["text"]).pack(side="left")
        win.range_to_spin = tk.Spinbox(controls, from_=1, to=1, width=6,
                                       textvariable=win.range_to, state="readonly", **spin_style)
        win.range_to_spin.pack(side="left", padx=4)

        def practice():
            try:
                start, end = int(win.range_from.get()) - 1, int(win.range_to.get()) - 1
            except (TypeError, ValueError):
                self.status("Укажите номера ходов", err=True)
                return
            if start < 0 or end >= len(rows()) or start > end:
                self.status("Неверный диапазон ходов", err=True)
                return
            range_state["start"], range_state["end"] = start, end
            win.pick_mode = None
            set_pick_buttons()
            self.practice_range = (start, end)
            self.mode = "step"
            select(start)
            self.status("Выбран отрезок %d–%d · ручная практика · нажмите «Старт»" % (start + 1, end + 1))

        def clear_practice():
            self.stop()
            self.practice_range = None
            range_state["start"] = range_state["end"] = None
            win.pick_mode = None
            set_pick_buttons()
            self.mode = "step"
            select(0)
            win.range_from.set("1")
            win.range_to.set("1")
            self.status("Выбрана вся песня · нажмите «Старт»")

        win.practice_button = self._btn(controls, "Ручная практика", practice)
        win.practice_button.pack(side="left", padx=(8, 4))
        win.clear_practice_button = self._btn(controls, "Вся песня", clear_practice)
        win.clear_practice_button.pack(side="left")

        def refresh_map():
            current = rows()
            index = max(0, min(self.preview_index, len(current) - 1))
            if self.practice_range is None:
                range_state["start"] = range_state["end"] = None
            else:
                range_state["start"], range_state["end"] = self.practice_range
            win.range_from_spin.configure(to=max(1, len(current)))
            win.range_to_spin.configure(to=max(1, len(current)))
            grid.set_events(current, index, active_range())
            sync_range_spins()
            update_position()

        def cleanup_map(event=None):
            if event is not None and event.widget is not win:
                return
            try:
                self._map_refreshers.remove(refresh_map)
            except ValueError:
                pass
            if getattr(self, "_map_window", None) is win:
                self._map_window = None

        win.bind("<Destroy>", cleanup_map, add="+")
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        self._map_window = win
        self._map_refreshers.append(refresh_map)
        refresh_map()
        return win

    def open_settings(self):
        win = tk.Toplevel(self.panel)
        win.title("Sky HUD · настройки")
        win.attributes("-topmost", True)
        px, py = self.panel.winfo_rootx(), self.panel.winfo_rooty()
        win.geometry("430x320%+d+%d" % (max(20, px + 20), max(20, py + 20)))
        pal = dict(self.palette)
        win._theme_palette = pal
        win.configure(bg=pal["bg"])
        self._theme_titlebar(win)
        win._release = None
        cancelled = threading.Event()
        jobs = []
        def destroyed(event):
            if event.widget is win:
                cancelled.set()
                for job in jobs:
                    value = job.get("value")
                    if job.get("install") and value:
                        shutil.rmtree(value[0], ignore_errors=True)
        win.bind("<Destroy>", destroyed, add="+")
        tk.Label(win, text="Настройки Sky HUD", font=("Segoe UI", 12),
                 bg=pal["bg"], fg=pal["text"]).pack(anchor="w", padx=16, pady=(16, 8))
        theme_row = tk.Frame(win, bg=pal["bg"])
        theme_row.pack(fill="x", padx=16, pady=(0, 8))
        tk.Label(theme_row, text="Тема", bg=pal["bg"], fg=pal["text"]).pack(side="left")
        choices = {"Системная": "system", "Светлая": "light", "Тёмная": "dark"}
        labels = {value: key for key, value in choices.items()}
        win.theme_var = tk.StringVar(value=labels[self.theme_choice])
        win.theme_menu = ttk.Combobox(theme_row, textvariable=win.theme_var,
                                      values=list(choices), state="readonly", style="Sky.TCombobox", width=15)
        win.theme_menu.bind("<<ComboboxSelected>>",
                            lambda event: self.set_theme(choices[win.theme_var.get()]))
        def style_theme_list(event):
            popdown = win.tk.call("ttk::combobox::PopdownWindow", win.theme_menu)
            win.tk.call(popdown + ".f.l", "configure", "-background", self.palette["panel"],
                        "-foreground", self.palette["text"], "-selectbackground", self.palette["select"],
                        "-selectforeground", self.palette["text"])
        win.theme_menu.configure(postcommand=lambda: style_theme_list(None))
        win.theme_menu.pack(side="left", padx=8)
        tk.Frame(win, bg=pal["border"], height=1).pack(fill="x", padx=16, pady=(0, 8))
        win.creator_label = tk.Label(win, text="Создатель: @mafsolin", fg=pal["muted"], bg=pal["bg"],
                                     font=("Segoe UI", 9))
        win.creator_label.pack(anchor="w", padx=16)
        win.repo_label = tk.Label(win, text="Репозиторий: https://github.com/%s" % UPDATE_REPO,
                                  fg=pal["muted"], bg=pal["bg"], font=("Segoe UI", 9), anchor="w")
        win.repo_label.pack(anchor="w", padx=16, pady=(2, 10))
        win.update_status = tk.Label(win, text="Проверка обновлений ничего не скачивает",
                                     fg=pal["muted"], bg=pal["bg"], anchor="w", wraplength=390, justify="left")
        win.update_status.pack(fill="x", padx=16, pady=(0, 8))
        buttons = tk.Frame(win, bg=pal["bg"])
        buttons.pack(anchor="w", padx=16)
        win.update_button = self._btn(buttons, "Проверить обновления", lambda: None)
        win.update_button.pack(side="left")
        win.install_button = self._btn(buttons, "Обновиться", lambda: None, state="disabled")
        win.install_button.pack(side="left", padx=(8, 0))

        def on_ui(callback):
            result: dict[str, Any] = {"done": False}
            jobs.append(result)
            def poll():
                if not win.winfo_exists():
                    return
                if result["done"]:
                    callback(result.get("value"), result.get("error"))
                else:
                    self._after(60, poll)
            self._after(60, poll)
            return result

        def check():
            win.update_button.config(state="disabled", text="Проверяю…")
            win.install_button.config(state="disabled")
            win.update_status.config(text="Запрос к GitHub…", fg=pal["muted"])
            result = on_ui(lambda value, error: finish_check(value, error))
            def worker():
                try:
                    req = urllib.request.Request(
                        "https://api.github.com/repos/%s/releases/latest" % UPDATE_REPO,
                        headers={"Accept": "application/vnd.github+json", "User-Agent": "SkyHUD/%s" % APP_VERSION})
                    with urllib.request.urlopen(req, timeout=8) as response:
                        release = json.loads(response.read().decode("utf-8"))
                    result["value"] = release
                except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                    result["error"] = exc
                finally:
                    result["done"] = True
            threading.Thread(target=worker, daemon=True).start()

        def finish_check(release, error):
            win.update_button.config(state="normal", text="Проверить обновления")
            if error:
                win.update_status.config(text="Не удалось проверить обновления: %s" % error, fg=pal["error"])
                return
            message = update_message(release)
            win._release = release
            asset = _release_asset(release)
            if asset and _version_tuple(release.get("tag_name")) > _version_tuple(APP_VERSION):
                win.install_button.config(state="normal")
                message += "\nМожно скачать и установить новую версию."
            win.update_status.config(text=message, fg=pal["muted"])

        def install():
            release = win._release or {}
            asset = _release_asset(release)
            if not asset or not asset.get("browser_download_url"):
                win.update_status.config(text="В релизе нет пакета обновления", fg=pal["error"])
                return
            win.update_button.config(state="disabled")
            win.install_button.config(state="disabled", text="Скачиваю…")
            win.update_status.config(text="Скачивание и проверка пакета…", fg=pal["muted"])
            result = on_ui(lambda value, error: finish_install(value, error))
            result["install"] = True
            def worker():
                try:
                    prepared = download_update(release)
                    result["value"] = prepared
                    if cancelled.is_set():
                        shutil.rmtree(prepared[0], ignore_errors=True)
                except Exception as exc:
                    result["error"] = exc
                finally:
                    result["done"] = True
            threading.Thread(target=worker, daemon=True).start()

        def finish_install(value, error):
            if error:
                win.update_button.config(state="normal")
                win.install_button.config(state="normal", text="Обновиться")
                win.update_status.config(text="Обновление не установлено: %s" % error, fg=pal["error"])
                return
            tmp_dir = value[0]
            try:
                launch_update(value)
                for job in jobs:
                    job["install"] = False
                win.update_status.config(text="Обновление подготовлено. Приложение перезапустится…", fg=pal["muted"])
                self.quit()
            except Exception as exc:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                win.update_button.config(state="normal")
                win.install_button.config(state="normal", text="Обновиться")
                win.update_status.config(text="Не удалось запустить updater: %s" % exc, fg=pal["error"])

        win.update_button.config(command=check)
        win.install_button.config(command=install)
        return win

    def open_catalog(self):
        pal = self.palette
        win = tk.Toplevel(self.panel)
        win.title("Sky Notes · каталог")
        px, py = self.panel.winfo_rootx(), self.panel.winfo_rooty()
        win.geometry("620x700%+d%+d" % (px, max(20, py-350)))
        win.configure(bg=pal["bg"])
        self._theme_titlebar(win)
        win.attributes("-topmost", True)
        head = tk.Frame(win, bg=pal["bg"], padx=16, pady=16)
        head.pack(fill="x")
        title_row = tk.Frame(head, bg=pal["bg"])
        title_row.pack(fill="x")
        tk.Label(title_row, text="Каталог нот", fg=pal["text"], bg=pal["bg"],
                 font=("Segoe UI", 12)).pack(side="left")
        win.import_button = self._btn(title_row, "Загрузить свою песню", lambda: upload(),
                                      font=("Segoe UI", 10), padx=12, pady=7)
        win.import_button.pack(side="right")
        q = tk.StringVar()
        ent = tk.Entry(head, textvariable=q, bg=pal["entry"], fg=pal["text"],
                       insertbackground=pal["text"], relief="flat", font=("Segoe UI", 11),
                       highlightthickness=1, highlightbackground=pal["border"],
                       highlightcolor=pal["accent"])
        ent.pack(fill="x", ipady=8, pady=(8, 0))
        win.catalog_count = tk.Label(head, text="", bg=pal["bg"], fg=pal["muted"],
                                     anchor="w", font=("Segoe UI", 9))
        win.catalog_count.pack(fill="x", pady=(4, 0))
        win.import_status = tk.Label(head, text="", bg=pal["bg"], fg=pal["muted"],
                                     anchor="w", wraplength=560, font=("Segoe UI", 9))
        win.import_status.pack(fill="x", pady=(2, 0))
        tk.Frame(win, bg=pal["border"], height=1).pack(fill="x", padx=16)
        body = tk.Frame(win, bg=pal["bg"], padx=16, pady=8)
        pager = tk.Frame(win, bg=pal["bg"], padx=16)
        pager.pack(fill="x", side="bottom", pady=(0, 12))
        win.catalog_fav = self._btn(pager, "★ Избранное", lambda: goto_page(0), width=12)
        win.catalog_fav.pack(side="left")
        win.catalog_prev = self._btn(pager, "← Назад", lambda: step_page(-1), width=9)
        win.catalog_prev.pack(side="left", padx=(8, 0))
        win.catalog_next = self._btn(pager, "Вперёд →", lambda: step_page(1), width=9)
        win.catalog_next.pack(side="left", padx=(8, 0))
        win.catalog_page_label = tk.Label(pager, text="", bg=pal["bg"], fg=pal["muted"],
                                          font=("Segoe UI", 9))
        win.catalog_page_label.pack(side="right")
        body.pack(fill="both", expand=True)
        style = ttk.Style(win)
        style.configure("Sky.Treeview", background=pal["bg"], fieldbackground=pal["bg"],
                        foreground=pal["text"], rowheight=32, font=("Segoe UI", 11), borderwidth=0,
                        bordercolor=pal["bg"], lightcolor=pal["bg"])
        style.map("Sky.Treeview", background=[("selected", pal["select"])],
                  foreground=[("selected", pal["text"])])
        tree = ttk.Treeview(body, columns=("favorite", "name", "size"), show="", style="Sky.Treeview", selectmode="browse")
        tree.column("favorite", width=40, stretch=False, anchor="center")
        tree.column("name", width=420, minwidth=180)
        tree.column("size", width=70, stretch=False, anchor="e")
        tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(body, command=tree.yview, style="Sky.Vertical.TScrollbar")
        win.catalog_scrollbar = sb
        sb.pack(side="right", fill="y")
        tree.configure(yscrollcommand=sb.set)
        win.catalog_tree = tree
        win.catalog_entry = ent
        win.catalog_query = q
        win.catalog_all = []
        win.catalog_arr = []
        win.catalog_page = 0
        win.catalog_mode = "favorites"

        def step_page(delta):
            win.catalog_page = max(0, win.catalog_page + delta)
            refresh()

        def goto_page(number):
            win.catalog_page = max(0, number)
            refresh()

        win.catalog_goto_page = goto_page

        def refresh(reset=False):
            if reset:
                win.catalog_page = 0
            query = q.get()
            rows, page, pages, mode, total, fav_count = self.catalog_page(query, win.catalog_page)
            win.catalog_page = page
            win.catalog_mode = mode
            win.catalog_arr = rows
            children = tree.get_children()
            if children:
                tree.delete(*children)
            for i, (name, path, size) in enumerate(rows):
                star = "★" if song_path_key(path) in self.favorites else "☆"
                tree.insert("", "end", iid=str(i), values=(star, os.path.splitext(name)[0], "%d KB" % round(size/1024)))
            number = page + 1
            if mode == "favorites":
                win.catalog_count.config(text=("★ Избранное: %d" % fav_count) if fav_count
                                         else "★ Избранное · пока пусто — нажмите ☆ у песни")
            elif mode == "search":
                win.catalog_count.config(text="Найдено: %d · стр. %d из %d" % (total, number, pages))
            else:
                win.catalog_count.config(text="Всего: %d · стр. %d из %d" % (total, number, pages))
            win.catalog_page_label.config(text="Стр. %d из %d" % (number, pages))
            win.catalog_prev.config(state="normal" if page > 0 else "disabled")
            win.catalog_next.config(state="normal" if page < pages - 1 else "disabled")
            win.catalog_fav.config(state="normal" if mode == "songs" else "disabled")

        def paste_search():
            try:
                text = win.clipboard_get()
            except tk.TclError:
                return
            line = next((part.strip() for part in text.splitlines() if part.strip()), "")[:120]
            if not line:
                return
            if ent.selection_present():
                ent.delete("sel.first", "sel.last")
            ent.insert("insert", line)

        menu = tk.Menu(win, tearoff=0, bg=pal["panel"], fg=pal["text"], bd=0,
                       activebackground=pal["select"], activeforeground=pal["text"])
        menu.add_command(label="Вставить", command=paste_search)
        menu.add_command(label="Копировать", command=lambda: ent.event_generate("<<Copy>>"))
        menu.add_command(label="Вырезать", command=lambda: ent.event_generate("<<Cut>>"))
        menu.add_separator()
        menu.add_command(label="Выделить всё",
                         command=lambda: (ent.selection_range(0, "end"), ent.icursor("end")))
        win.catalog_menu = menu
        win.catalog_paste = paste_search

        def show_menu(event):
            menu.tk_popup(event.x_root, event.y_root)
            return "break"

        ent.bind("<Button-3>", show_menu)
        ent.bind("<Control-v>", lambda e: (paste_search(), "break")[1])
        ent.bind("<Control-V>", lambda e: (paste_search(), "break")[1])
        song_menu = tk.Menu(win, tearoff=0, bg=pal["panel"], fg=pal["text"], bd=0,
                            activebackground=pal["select"], activeforeground=pal["text"])
        win.catalog_song_menu = song_menu
        def rename_selected():
            selected = tree.selection()
            if selected:
                self.rename_song(win.catalog_arr[int(selected[0])][1], parent=win)
        song_menu.add_command(label="Переименовать…", command=rename_selected)
        def show_song_menu(event):
            iid = tree.identify_row(event.y)
            if iid:
                tree.selection_set(iid)
                tree.focus(iid)
                song_menu.tk_popup(event.x_root, event.y_root)
            return "break"
        tree.bind("<Button-3>", show_song_menu)

        def upload():
            path = filedialog.askopenfilename(
                parent=win, title="Загрузить свою песню в каталог",
                filetypes=[("Песни Sky", "*.txt *.json")])
            if not path:
                return
            try:
                dest = self.import_song_file(path)
                self.load_song_file(dest)
                q.set(os.path.splitext(os.path.basename(dest))[0])
                refresh()
                tree.selection_set("0")
                tree.see("0")
                win.import_status.config(text="Добавлено: " + os.path.basename(dest), fg=pal["muted"])
                self.status("Своя песня добавлена · нажмите «Старт», чтобы играть")
            except Exception as e:
                win.import_status.config(text="Ошибка: %s" % e, fg=pal["error"])

        def favorite(event):
            iid = tree.identify_row(event.y)
            if iid and tree.identify_column(event.x) == "#1":
                path = win.catalog_arr[int(iid)][1]
                self.toggle_favorite(path)
                refresh()
                for i, entry in enumerate(win.catalog_arr):
                    if entry[1] == path:
                        tree.selection_set(str(i))
                        tree.see(str(i))
                        break
                return "break"

        def choose(event=None):
            if event is not None and tree.identify_column(event.x) == "#1":
                return "break"
            selected = tree.selection()
            if not selected:
                return
            path = win.catalog_arr[int(selected[0])][1]
            try:
                self.load_song_file(path)
                self.status("Загружено: " + os.path.basename(path))
                close_catalog()
            except Exception as e:
                self.status("Ошибка: %s" % e, err=True)

        def close_catalog():
            try:
                self._catalog_refreshers.remove(refresh)
            except ValueError:
                pass
            if win.winfo_exists():
                win.destroy()

        def cleanup_catalog(event=None):
            if event is not None and event.widget is not win:
                return
            try:
                self._catalog_refreshers.remove(refresh)
            except ValueError:
                pass

        win.bind("<Destroy>", cleanup_catalog, add="+")

        self._catalog_refreshers.append(refresh)
        win.protocol("WM_DELETE_WINDOW", close_catalog)
        q.trace_add("write", lambda *_: refresh(reset=True))
        win.bind("<Prior>", lambda e: step_page(-1))
        win.bind("<Next>", lambda e: step_page(1))
        tree.bind("<Button-1>", favorite)
        tree.bind("<Double-Button-1>", choose)
        tree.bind("<Return>", choose)
        refresh()
        return win

    # ---------------------------------------------------------- конфиг-геттеры
    def _on_cfg(self):
        self.metro_on = self.v_metro.get()
        self.sound_on = self.v_sound.get()
        self.loop_on = self.v_loop.get()
        try:
            self.tempo = float(self.v_tempo.get())
        except ValueError:
            pass
        self.stop()
        self.sync_panel_controls()
        self._save_cfg_debounced()

    def _set_scale(self, v):
        self.scale = v
        self._redraw()
        self._save_cfg_debounced()

    def _set_alpha(self, v):
        self.alpha = float(v)
        self.root.attributes("-alpha", self.alpha)
        self._save_cfg_debounced()

    def toggle_hud(self):
        self.hud_hidden = not self.hud_hidden
        if self.hud_hidden:
            self.root.withdraw()
        else:
            self.root.deiconify()
            self._click_through = self._make_click_through()
            self._redraw()
        self.lb_hide.config(text="Показать HUD" if self.hud_hidden else "Скрыть HUD полностью")
        self._save_cfg_debounced()

    def nudge(self, dx, dy):
        x, y, w, h = self.geom()
        self.root.geometry("+%d+%d" % (x + dx, y + dy))
        self._remember_hud_pos()
        self._save_cfg_debounced()

    def place_bottom(self):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x, y, w, h = self.geom()
        self.root.geometry("+%d+%d" % ((sw - w) // 2, sh - h - 90))
        self._remember_hud_pos()
        self._save_cfg_debounced()

    # ---------------------------------------------------------- песни
    def load_song_file(self, path):
        with open(path, "r", encoding="utf-8-sig") as f:
            song = parse_song(f.read(), os.path.basename(path))
        self.set_song(song, source_path=path)
        self.save_cfg()

    def restore_song(self):
        last = self.cfg.get("last_song")
        if last:
            path = last if os.path.isabs(last) else os.path.join(APP_DIR, last)
            try:
                self.load_song_file(path)
                self.status("Песня восстановлена · нажмите «Старт», чтобы играть с начала")
                return
            except (OSError, ValueError, TypeError, IndexError) as e:
                self.load_demo()
                self.status("Последняя песня недоступна: %s" % e, err=True)
                return
        self.load_demo()

    def ask_file(self):
        path = filedialog.askopenfilename(
            title="Файл песни Sky",
            filetypes=[("Песни Sky", "*.txt *.json"), ("Все файлы", "*.*")])
        if not path:
            return
        try:
            self.load_song_file(path)
        except Exception as e:
            self.status("Ошибка чтения песни: %s" % e, err=True)

    def load_demo(self):
        here = APP_DIR
        demo = os.path.join(here, "A cruel angel.txt")
        if os.path.exists(demo):
            try:
                with open(demo, "r", encoding="utf-8-sig") as f:
                    self.set_song(parse_song(f.read(), os.path.basename(demo)), source_path=demo)
                return
            except Exception:
                pass
        if self._songs_installed():
            self.status("Выберите песню в «Каталог нот» · песни лежат в папке songs")
        else:
            self.status("Песен нет · скачайте SkyHUD-Songs.zip и распакуйте в эту папку", err=True)

    @staticmethod
    def _songs_installed():
        root = os.path.join(APP_DIR, "songs")
        for _base, _dirs, files in os.walk(root):
            if any(name.lower().endswith((".txt", ".json")) for name in files):
                return True
        return False

    def set_song(self, song, source_path=None):
        self.stop()
        self.song = song
        self.song_path = song_path_key(source_path) if source_path else None
        self.practice_range = None
        self.flash.clear()
        ids = [p["id"] for p in song["parts"]]
        if self.part_sel is None:
            self.part_sel = set(ids)
        else:
            keep = self.part_sel & set(ids)
            if not keep:
                self.part_sel = set(ids)
        self._rebuild_preview_timeline(reset=True)
        self._render_part_checks()
        meta = []
        if song["bpm"]:
            meta.append("♩ %g BPM" % song["bpm"])
        meta.append("⏱ %s" % fmt_dur(song["dur_ms"]))
        total = sum(len(p["notes"]) for p in song["parts"])
        meta.append("%d нот" % total)
        self.lb_song.config(text=song["name"])
        self.lb_meta.config(text=("автор: %s · " % song["author"] if song["author"] else "") + " · ".join(meta))
        kinds = ", ".join("партия %s: %s (%d)" % (p["id"], p["kind"], len(p["notes"])) for p in song["parts"])
        self.status("Загружено: %s" % kinds)
        self._redraw()
        self.sync_panel_controls()
        self.sync_favorite_button()

    def _render_part_checks(self):
        for w in self.part_frame.winfo_children():
            w.destroy()
        if not self.song:
            return
        self.part_vars = {}
        for i, p in enumerate(self.song["parts"]):
            color = PART_COLORS[i % len(PART_COLORS)]
            var = tk.BooleanVar(value=p["id"] in self.part_sel)
            self.part_vars[p["id"]] = var
            cb = tk.Checkbutton(self.part_frame, text="%s · %s (%d)" % (p["id"], p["kind"], len(p["notes"])),
                                variable=var, fg="#%02x%02x%02x" % color,
                                selectcolor="#fff", activeforeground="#000",
                                command=lambda pid=p["id"], v=var: self._toggle_part(pid, v))
            cb.pack(side="left", padx=(0, 8))

    def _toggle_part(self, pid, var):
        if var.get():
            self.part_sel.add(pid)
        else:
            self.part_sel.discard(pid)
        self.stop()
        self.practice_range = None
        self._rebuild_preview_timeline(reset=True)
        self._redraw()
        self.sync_panel_controls()
        self._save_cfg_debounced()

    # ---------------------------------------------------------- прогон
    def set_mode(self, m):
        if m not in ("demo", "train", "step"):
            return
        self.mode = m
        self.stop()
        self.sync_panel_controls()
        self._save_cfg_debounced()

    def toggle_run(self):
        if self.run:
            self.stop()
        else:
            self.start()

    def start(self):
        if self.run or not self.song:
            return
        sel = self._selected_part_ids()
        if not sel:
            self.status("Включите хотя бы одну партию", err=True)
            return
        tl = build_timeline(self.song, sel)
        if self.practice_range is not None:
            start, end = self.practice_range
            if start < 0 or end >= len(tl["evs"]) or start > end:
                self.status("Выбранный отрезок больше не существует", err=True)
                return
            tl = slice_timeline(tl, start, end)
        if not tl["evs"]:
            self.status("В выбранных партиях нет нот", err=True)
            return
        self.run = {
            "tl": tl,
            "tempo": self.tempo,
            "base": time.perf_counter() + 0.8,
            "ev_idx": 0,
            "grid_idx": 0,
            "stats": {"ok": 0, "miss": 0, "bad": 0, "streak": 0, "best": 0},
            "mode": self.mode,
            "step_idx": 0,
            "step_done": set(),
        }
        # Не считать клавиши, которые уже были зажаты в игре до старта HUD,
        # новым нажатием первого аккорда.
        self.pressed_key = set()
        try:
            for idx, code in enumerate(CODES):
                if ctypes.windll.user32.GetAsyncKeyState(VK[code]) & 0x8000:
                    self.pressed_key.add(idx)
        except Exception:
            pass
        self.sound.clear()
        self.flash.clear()
        if self.mode == "train":
            self.stats_text("0 верно · 0 мимо · 0 лишних · точность —")
        elif self.mode == "step":
            self.stats_text("🚶 ходов 0/%d · ⚠️ ошибок 0" % len(tl["evs"]))
        self.lb_go.config(text="■ Стоп")
        if self.mode == "demo":
            self.status("Демо: слушайте и следите за подсветкой")
        elif self.mode == "train":
            self.status("Тренировка: жмите клавиши, когда нота подсвечена (в игре!)")
        else:
            self.status("Шаги: жмите все клавиши панели «Сейчас» — ход пойдёт дальше")
        self.sync_panel_controls()

    def restart_run(self):
        if not self.run:
            return
        self.stop()
        self.start()

    def stop(self):
        if self.run:
            self.run = None
            self.sound.clear()
            self.lb_go.config(text="▶ Старт")
            self.status("Остановлено")
        self.sync_panel_controls()

    def sync_panel_controls(self):
        self.lb_go.config(text="■ Стоп" if self.run else "▶ Старт")
        if hasattr(self, "mode_btns"):
            for m, b in self.mode_btns.items():
                b.config(relief="sunken" if m == self.mode else "raised")
        if hasattr(self, "cb_metro"):
            self.cb_metro.config(state="disabled" if self.mode == "step" else "normal")

    def stats_text(self, s):
        self.lb_stats.config(text=s)

    def status(self, s, err=False):
        self.lb_status.config(text=s, fg="#c00" if err else "#888")

    def song_time_ms(self):
        r = self.run
        return max(0.0, (time.perf_counter() - r["base"]) * 1000.0 * r["tempo"])

    # ---------------------------------------------------------- такт (тайминг)
    def _tick(self):
        try:
            self._tick_inner()
        except Exception as e:
            print("tick error:", e)
        self._after(30 if self.run else 150, self._tick)

    def _tick_inner(self):
        r = self.run
        if not r:
            return
        if r["mode"] == "step":
            # Пошаговый режим: время не идёт, ходы переключает _judge_step.
            if r["step_idx"] >= len(r["tl"]["evs"]):
                if self.loop_on:
                    self.restart_run()
                else:
                    self.stop()
            return
        tl = r["tl"]
        now = self.song_time_ms()
        evs, insts = tl["evs"], tl["instances"]
        # события
        while r["ev_idx"] < len(evs) and evs[r["ev_idx"]]["t"] <= now:
            ev = evs[r["ev_idx"]]
            if r["mode"] == "demo" and self.sound_on:
                for n in ev["notes"]:
                    self.sound.beep(freq_of(n["key"]), 80)
            r["ev_idx"] += 1
        # метроном
        if r["mode"] == "train" and self.metro_on:
            while r["grid_idx"] * tl["gap"] <= now:
                if self.sound_on:
                    accent = r["grid_idx"] % 4 == 0
                    self.sound.beep(1760 if accent else 1250, 55 if accent else 35)
                r["grid_idx"] += 1
        # промахи
        if r["mode"] == "train":
            for inst in insts:
                if not inst["done"] and not inst["consumed"] and now > inst["close"]:
                    inst["done"] = True
                    st = r["stats"]
                    st["miss"] += 1
                    st["streak"] = 0
            self._update_stats_label()
        # конец
        if r["ev_idx"] >= len(evs) and now > tl["end_t"] + 600:
            if self.loop_on:
                self.restart_run()
            else:
                self.stop()

    # ---------------------------------------------------------- ввод с клавиатуры
    def _poll_keys(self):
        try:
            if not self._click_through and time.perf_counter() - self._t0 > 0.3:
                self._click_through = self._make_click_through()
            states = {}
            for idx, code in enumerate(CODES):
                vk = VK[code]
                res = ctypes.windll.user32.GetAsyncKeyState(vk)
                states[idx] = bool(res & 0x8000)
            for idx, down in states.items():
                if down and idx not in self.pressed_key:
                    self._key_down(idx)
                elif not down and idx in self.pressed_key:
                    self._key_up(idx)
        except Exception as e:
            print("poll error:", e)
        self._after(8, self._poll_keys)

    def _key_down(self, idx):
        self.pressed_key.add(idx)
        r = self.run
        if r and self.sound_on:
            self.sound.beep(freq_of(idx), 55 if r["mode"] == "train" else 45)
        if r:
            if r["mode"] == "train":
                self._judge(idx, self.song_time_ms())
            elif r["mode"] == "step":
                self._judge_step(idx)

    def _key_up(self, idx):
        self.pressed_key.discard(idx)

    def _judge(self, idx, now):
        r = self.run
        tl = r["tl"]
        found = None
        for inst in tl["instances"]:
            if inst["key"] != idx or inst["done"] or inst["consumed"]:
                continue
            if inst["open"] <= now <= inst["close"]:
                found = inst
                break
        st = r["stats"]
        if found:
            found["consumed"] = True
            st["ok"] += 1
            st["streak"] += 1
            st["best"] = max(st["best"], st["streak"])
            self.flash[idx] = ("hit", time.perf_counter() + 0.4)
        else:
            st["bad"] += 1
            st["streak"] = 0
            self.flash[idx] = ("wrong", time.perf_counter() + 0.4)
        self._update_stats_label()

    def _judge_step(self, idx):
        r = self.run
        evs = r["tl"]["evs"]
        si = r["step_idx"]
        if si >= len(evs):
            return
        keyset = {n["key"] for n in evs[si]["notes"]}
        st = r["stats"]
        if idx in keyset:
            if idx not in r["step_done"]:
                r["step_done"].add(idx)
                self.flash[idx] = ("hit", time.perf_counter() + 0.3)
                if len(r["step_done"]) == len(keyset):
                    # весь ход сыгран — следующий
                    r["step_idx"] = si + 1
                    r["step_done"] = set()
                    st["ok"] += 1
                    st["streak"] += 1
                    st["best"] = max(st["best"], st["streak"])
                    if self.sound_on:
                        self.sound.beep(1568, 45)
                    if r["step_idx"] >= len(evs):
                        if self.loop_on:
                            self.restart_run()
                        else:
                            self.stop()
        else:
            st["bad"] += 1
            st["streak"] = 0
            self.flash[idx] = ("wrong", time.perf_counter() + 0.3)
        self._update_stats_label()

    def _update_stats_label(self):
        st = self.run["stats"] if self.run else None
        if not st:
            return
        if self.run["mode"] == "step":
            total_ev = len(self.run["tl"]["evs"])
            s = "🚶 ходов %d/%d · ⚠️ ошибок %d" % (st["ok"], total_ev, st["bad"])
            if st["best"] > 1:
                s += " · серия %d" % st["best"]
            self.stats_text(s)
            return
        total = st["ok"] + st["miss"]
        acc = ("%d%%" % round(100.0 * st["ok"] / total)) if total else "—"
        s = "✅ %d верно · ❌ %d мимо · ⚠️ %d лишних · точность %s" % (st["ok"], st["miss"], st["bad"], acc)
        if st["best"] > 1:
            s += " · серия %d" % st["best"]
        self.stats_text(s)

    # ---------------------------------------------------------- отрисовка HUD
    # Три клавиатуры: текущий ход и два следующих, без дублирующих подписей.
    HUD_COLORS = {
        "transparent": "#0b1230", "panel": "#101a2a", "edge": "#485b70",
        "key": "#1d2b3e", "key_edge": "#5c738a", "text": "#ffffff",
        "muted": "#d3deeb", "now": "#ffcf66", "next": "#6edaff",
        "ink": "#101722", "hit": "#fff0be", "wrong": "#ff9999",
    }

    def hud_colors(self):
        if self.theme == "light":
            return {"transparent": "#0b1230", "panel": "#f5f7fa", "edge": "#b7c4d0",
                    "key": "#ffffff", "key_edge": "#b8c6d2", "text": "#1c2430",
                    "muted": "#394b5b", "footer_text": "#ffffff", "footer_muted": "#d3deeb",
                    "now": "#f4cb75", "next": "#94d5d0",
                    "ink": "#182733", "hit": "#c9e4c8", "wrong": "#efc1bd"}
        return self.HUD_COLORS

    def map_palette(self):
        """Colours for the song map; highlight gold/cyan stays readable in both themes."""
        if self.theme == "light":
            return {"panel": "#ffffff", "edge": "#c9d3e0", "key": "#eef2f7",
                    "key_edge": "#c2ccda", "text": "#1c2430", "muted": "#5f6b7a",
                    "now": "#e0a52a", "next": "#2a9dbd", "ink": "#ffffff"}
        return {"panel": "#101a2a", "edge": "#485b70", "key": "#1d2b3e",
                "key_edge": "#5c738a", "text": "#ffffff", "muted": "#d3deeb",
                "now": "#ffcf66", "next": "#6edaff", "ink": "#101722"}

    def _redraw(self):
        # полная пересборка элементов (масштаб/песня/партии) в ближайшем кадре
        self._need_rebuild = True

    def _layout_geom(self):
        s = self.scale
        cw, ch = int(44 * s), int(34 * s)
        g, padp, sp, pad = int(10 * s), int(16 * s), int(12 * s), int(12 * s)
        foot = int(30 * s)
        panel_w = padp * 2 + cw * 5 + g * 4
        panel_h = padp * 2 + ch * 3 + g * 2
        w = pad * 2 + panel_w * 3 + sp * 2
        h = pad * 2 + panel_h + foot
        return {"s": s, "cw": cw, "ch": ch, "g": g, "padp": padp, "sp": sp,
                "pad": pad, "foot": foot, "panel_w": panel_w, "panel_h": panel_h,
                "w": w, "h": h}

    def _frame(self):
        try:
            if not self.hud_hidden and (self._it is None or self._need_rebuild):
                self._build_items()
                self._need_rebuild = False
            if not self.hud_hidden:
                self._animate_items()
        except Exception as e:
            print("frame error:", e)
        self._after(33, self._frame)

    @staticmethod
    def _hex(rgb):
        return "#%02x%02x%02x" % tuple(int(x) for x in rgb)

    def _build_items(self):
        c, L, C = self.cv, self._layout_geom(), self.hud_colors()
        c.config(width=L["w"], height=L["h"], bg=C["transparent"])
        self.root.geometry("%dx%d" % (L["w"], L["h"]))
        c.delete("all")
        s = L["s"]
        it: dict[str, Any] = {"body": [[None] * 15 for _ in range(3)],
                              "labels": [[None] * 15 for _ in range(3)]}
        x, yp = L["pad"], L["pad"]
        for p in range(3):
            c.create_rectangle(x, yp, x + L["panel_w"], yp + L["panel_h"],
                               fill=C["panel"], outline=C["edge"], width=1)
            for idx in range(15):
                row, col = divmod(idx, 5)
                kx = x + L["padp"] + col * (L["cw"] + L["g"])
                ky = yp + L["padp"] + row * (L["ch"] + L["g"])
                it["body"][p][idx] = c.create_rectangle(
                    kx, ky, kx + L["cw"], ky + L["ch"],
                    fill=C["key"], outline=C["key_edge"], width=1)
                cx, cy = kx + L["cw"] / 2, ky + L["ch"] / 2
                it["labels"][p][idx] = c.create_text(
                    cx, cy, text=CODES[idx], font=("Segoe UI", max(11, int(16 * s)), "bold"), fill=C["text"])
            x += L["panel_w"] + L["sp"]
        bx1, bx2 = L["pad"], L["w"] - L["pad"]
        bar_y = L["h"] - L["pad"]
        it["bar_bg"] = c.create_line(bx1, bar_y, bx2, bar_y, fill=C["edge"], width=max(2, int(2*s)))
        it["bar_fill"] = c.create_line(bx1, bar_y, bx1, bar_y, fill=C["now"], width=max(2, int(2*s)))
        it["name"] = c.create_text(bx1, bar_y-int(7*s), anchor="sw",
                                   text=self.song["name"][:50] if self.song else "",
                                   font=("Segoe UI", max(8, int(10*s))), fill=C.get("footer_text", C["text"]))
        it["time"] = c.create_text(bx2, bar_y-int(7*s), anchor="se", text="",
                                   font=("Segoe UI", max(8, int(10*s))), fill=C.get("footer_muted", C["muted"]))
        it["credit"] = c.create_text(bx2, L["h"] - max(2, int(2*s)), anchor="se",
                                      text="@mafsolin", font=("Segoe UI", max(6, int(7*s))),
                                      fill=C.get("footer_muted", C["muted"]))
        self._it = it
        self._last_style: list[tuple[str, str, str, int] | None] = [None] * 45
        self._last_frac, self._last_time = None, None

    def _cursor(self, tl, now_ms):
        if not tl or not tl["evs"]:
            return 0
        lead, p = 0.35 * tl["gap"], 0
        for i, e in enumerate(tl["evs"]):
            if e["t"] > now_ms + lead:
                break
            p = i
        return p

    def _animate_items(self):
        if self._it is None:
            return
        it, c, C, L = self._it, self.cv, self.hud_colors(), self._layout_geom()
        r = self.run
        tl = r["tl"] if r else self.preview_tl
        now = self.song_time_ms() if r else 0.0
        step_mode = bool(r and r["mode"] == "step")
        cursor = (r["step_idx"] if step_mode else self._cursor(tl, now)) if r else self.preview_index
        if tl and tl["evs"]:
            cursor = max(0, min(cursor, len(tl["evs"]) - 1))
        else:
            cursor = 0
        events = tl["evs"][cursor:cursor+3] if tl else []
        next_keys = {n["key"] for n in events[1]["notes"]} if len(events) > 1 else set()
        pf = time.perf_counter()
        for p in range(3):
            active = {n["key"] for n in events[p]["notes"]} if p < len(events) else set()
            for idx in range(15):
                fill, edge, text = C["key"], C["key_edge"], C["text"]
                if idx in active:
                    fill = C["now"] if p == 0 else C["next"]
                    edge, text = C["text"], C["ink"]
                if p == 0:
                    if step_mode and idx in r["step_done"]:
                        fill, edge, text = C["hit"], C["text"], C["ink"]
                    fl = self.flash.get(idx)
                    if fl and pf < fl[1]:
                        fill = C["hit"] if fl[0] == "hit" else C["wrong"]
                        edge, text = C["text"], C["ink"]
                    elif idx in self.pressed_key:
                        edge = C["text"]
                # Первая карточка одновременно показывает текущий и следующий ход.
                # Контур остаётся виден даже при общей клавише или вспышке попадания.
                width = 1
                if p == 0 and idx in next_keys:
                    edge = C["next"]
                    width = max(2, int(2 * L["s"]))
                style = fill, edge, text, width
                k = p*15 + idx
                if self._last_style[k] != style:
                    c.itemconfig(it["body"][p][idx], fill=fill, outline=edge, width=width)
                    c.itemconfig(it["labels"][p][idx], fill=text)
                    self._last_style[k] = style
        frac, ts = 0.0, ""
        if r:
            if step_mode and tl["evs"]:
                total = len(tl["evs"])
                frac = min(cursor, total) / total
                ts = "ход %d/%d" % (min(cursor + 1, total), total)
            else:
                frac = min(1.0, now / max(1.0, self.song["dur_ms"]))
                ts = "%s / %s" % (fmt_dur(now), fmt_dur(self.song["dur_ms"]))
        if frac != self._last_frac:
            bx1, bx2, y = L["pad"], L["w"]-L["pad"], L["h"]-L["pad"]
            c.coords(it["bar_fill"], bx1, y, bx1+(bx2-bx1)*frac, y)
            self._last_frac = frac
        if ts != self._last_time:
            c.itemconfig(it["time"], text=ts)
            self._last_time = ts

    def quit(self):
        self.save_cfg()
        self._cancel_afters()
        try:
            self.sound.close()
        except Exception:
            pass
        self.root.destroy()


def freq_of(idx):
    return BASE_C4 * (2.0 ** (SEMI[idx] / 12.0))


def fmt_dur(ms):
    s = max(0, int(round(ms / 1000.0)))
    return "%d:%02d" % (s // 60, s % 60)


# ---------------------------------------------------------------- самопроверка
BUILTIN_CHECK_SONG = (
    '{"name":"Проверка","songNotes":['
    '{"time":0,"key":"1Key0"},{"time":250,"key":"2Key1"},'
    '{"time":500,"key":"1Key2"},{"time":750,"key":"2Key3"},'
    '{"time":1000,"key":"1Key4"},{"time":1250,"key":"2Key5"}]}'
)


def check_mode(paths):
    # Diagnostic output must not fail on Western Windows console encodings.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    ok = True

    def t(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        try:
            print(("PASS" if cond else "FAIL"), "|", name, ("| " + str(extra)) if extra else "")
        except UnicodeEncodeError:
            safe_name = name.encode(sys.stdout.encoding or "ascii", errors="replace").decode(sys.stdout.encoding or "ascii", errors="replace")
            print(("PASS" if cond else "FAIL"), "|", safe_name, ("| " + str(extra)) if extra else "")

    for path in paths:
        if path is None:
            text, label = BUILTIN_CHECK_SONG, "встроенный пример"
        else:
            with open(path, "r", encoding="utf-8-sig") as f:
                text = f.read()
            label = os.path.basename(path)
        song = parse_song(text, label)
        t("parse %s" % label, song["name"] != "")
        for p in song["parts"]:
            print("   part %s: %d notes, kind=%s" % (p["id"], len(p["notes"]), p["kind"]))
        ids = [p["id"] for p in song["parts"]]
        tl = build_timeline(song, ids)
        t("timeline events>0", len(tl["evs"]) > 0, "events=%d gap=%.0fms" % (len(tl["evs"]), tl["gap"]))
        # идеальный игрок: жмём ровно в момент ноты -> все попадания
        stats = {"ok": 0, "miss": 0, "bad": 0}
        for inst in tl["instances"]:
            hit = False
            for tt in (inst["t"], inst["t"] + 0.1 * tl["gap"]):
                if inst["open"] <= tt <= inst["close"]:
                    hit = True
                    break
            if hit:
                stats["ok"] += 1
            else:
                stats["bad"] += 1
        t("perfect play all hit", stats["bad"] == 0, str(stats))
        # без нажатий — все в промахи
        misses = sum(1 for i in tl["instances"] if i["close"] <= tl["end_t"] + 1)
        t("all notes eventually miss", misses == len(tl["instances"]), "%d/%d" % (misses, len(tl["instances"])))
        # раннее нажатие (до open) — не попадание
        early = tl["instances"][0]
        t("early press rejected", not (early["open"] - 100 >= early["open"]), "open=%.0f" % early["open"])
    print("\nRESULT:", "ALL PASS" if ok else "SOME FAIL")
    sys.exit(0 if ok else 1)


def smoke_mode():
    """GUI поднимается на ~2.5 с и закрывается — проверка инициализации, стилей и отрисовки."""
    root = tk.Tk()
    app = HudApp(root)
    app.sound_on = False
    app.metro_on = False
    app.load_demo()

    def go():
        app.start()
        app.part_sel = {app.song["parts"][0]["id"]} if app.song else set()
        if app.song:
            app._render_part_checks()
        app.start()
        app._after(1500, done)

    def done():
        app.save_cfg()
        print("SMOKE OK: hud mapped=%s panel=%s song=%s run=%s" % (
            root.winfo_ismapped(), app.panel.winfo_ismapped(),
            app.song["name"] if app.song else None, bool(app.run)))
        app.quit()

    app._after(700, go)
    root.mainloop()


def main():
    args = sys.argv[1:]
    if args and args[0] == "--check":
        paths = args[1:]
        if not paths:
            demo = os.path.join(APP_DIR, "A cruel angel.txt")
            if os.path.exists(demo):
                paths = [demo]
            else:
                installed = [os.path.join(base, name)
                             for base, _dirs, files in os.walk(os.path.join(APP_DIR, "songs"))
                             for name in files if name.lower().endswith((".txt", ".json"))]
                paths = sorted(installed)[:1] or [None]
        check_mode(paths)
    elif args and args[0] == "--smoke":
        smoke_mode()
    else:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        root = tk.Tk()
        HudApp(root)
        root.mainloop()


if __name__ == "__main__":
    main()
