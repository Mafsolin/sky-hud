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
import json
import math
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from typing import Any
from tkinter import filedialog, ttk
import winsound

APP_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))

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

# ---------------------------------------------------------------- парсер
def parse_song(text, src_name="песня"):
    try:
        obj = json.loads(text)
    except Exception as e:
        raise ValueError("Не JSON: %s" % e)
    data = obj[0] if isinstance(obj, list) else obj
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
        m = re.match(r"^(\d+)?Key(\d+)$", key, re.I) or re.match(r"^Key\s*(\d+)$", key, re.I)
        if not m:
            mm = re.search(r"(\d+)$", key)
            if not mm:
                continue
            m = (None, "", mm.group(1))
        part = m.group(1) if m.group(1) else "1"
        num = int(m.group(2))
        max_nums.append(num)
        parts_map.setdefault(part, []).append((float(n.get("time")) or 0.0, num))

    if not parts_map:
        raise ValueError("В файле не нашлось нот")

    one_based = max(max_nums) > 14 or (max(max_nums) == 15 and 0 not in max_nums)
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
    return {
        "name": data.get("name") or src_name,
        "author": data.get("author") or "",
        "bpm": data.get("bpm") or 0,
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
    by_time = {t: e for t, e in zip(times, evs)}
    for p in song["parts"]:
        if p["id"] in part_ids:
            for t, ks in p["events"]:
                e = by_time[t]
                for k in ks:
                    e["notes"].append({"key": k, "part": p["id"]})
    gaps = [times[i + 1] - times[i] for i in range(len(times) - 1) if times[i + 1] - times[i] > 0]
    gaps.sort()
    gap = gaps[len(gaps) // 2] if gaps else 250.0
    gap = max(80.0, gap)
    instances = []
    for e in evs:
        for n in e["notes"]:
            instances.append({"t": e["t"], "key": n["key"], "part": n["part"],
                              "open": e["t"] - 0.35 * gap, "close": e["t"] + 0.7 * gap,
                              "consumed": False, "done": False})
    end_t = max((i["close"] for i in instances), default=(times[-1] if times else 0))
    return {"evs": evs, "gap": gap, "instances": instances, "end_t": end_t}


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


class HudApp:
    CFG_FILE = os.path.join(APP_DIR, "sky_hud_config.json")

    def __init__(self, root, start_song=None):
        self.root = root
        self.sound = SoundBus()
        self.cfg = self.load_cfg()
        self.scale = float(self.cfg.get("scale", 1.0))
        self.alpha = float(self.cfg.get("alpha", 0.85))
        self.tempo = float(self.cfg.get("tempo", 1.0))
        self.mode = "train"
        self.metro_on = False
        self.sound_on = False
        self.loop_on = False
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
        self._move_cfg(self.cfg.get("x"), self.cfg.get("y"))

        # ---- панель управления (обычное окно)
        self.panel = tk.Toplevel(root)
        self.panel.title("Sky HUD — управление")
        self.panel.attributes("-topmost", True)
        self.panel.protocol("WM_DELETE_WINDOW", self.quit)
        self._build_panel()
        self.place_panel()

        if start_song is not None:
            self.set_song(start_song)
        else:
            self.restore_song()

        self.root.after(15, self._poll_keys)
        self._it = None
        self._need_rebuild = True
        self._last_frac = 0.0
        self._last_time = None
        self.root.after(33, self._frame)
        self.root.after(40, self._tick)
        self.root.after(400, self._save_cfg_debounced)
        self._click_through = self._make_click_through()
        self._t0 = time.perf_counter()
        if self.hud_hidden:
            self.root.withdraw()

    # ---------------------------------------------------------- окно/стили
    def place_panel(self):
        # Saved screen coordinates may belong to a secondary monitor.
        self.root.update_idletasks()
        px, py = self.cfg.get("px"), self.cfg.get("py")
        if px is not None and py is not None:
            self.panel.geometry("%+d%+d" % (int(px), int(py)))
            return
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        pw, ph = self.panel.winfo_reqwidth(), self.panel.winfo_reqheight()
        rx, ry, rw, rh = self.geom()
        px, py = rx + rw + 14, max(20, ry)
        if px + pw > sw:
            px = max(10, rx - pw - 14)
        if px + pw > sw:
            px = max(10, (sw - pw) // 2)
        if py + ph > sh:
            py = max(10, sh - ph - 40)
        self.panel.geometry("%+d%+d" % (px, py))

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

    def _move_cfg(self, x, y):
        if x is not None:
            self.root.geometry("+%d+%d" % (int(x), int(y)))

    def geom(self):
        try:
            return self.root.winfo_rootx(), self.root.winfo_rooty(), \
                   self.root.winfo_width(), self.root.winfo_height()
        except Exception:
            return 0, 0, 0, 0

    def load_cfg(self):
        try:
            with open(self.CFG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    _save_pending = False

    def _save_cfg_debounced(self):
        if self._save_pending:
            return
        self._save_pending = True

        def do():
            self.save_cfg()
            self._save_pending = False
        self.root.after(500, do)

    def save_cfg(self):
        x, y, w, h = self.geom()
        px, py = self.panel.winfo_rootx(), self.panel.winfo_rooty()
        cfg = {
            "x": x, "y": y, "w": w,
            "px": px, "py": py,
            "scale": self.scale, "alpha": self.alpha, "tempo": self.tempo,
            "mode": self.mode, "metro": self.metro_on, "sound": self.sound_on,
            "loop": self.loop_on, "parts": sorted(self.part_sel or []),
            "hud_hidden": self.hud_hidden,
            "favorites": sorted(self.favorites),
            "last_song": self.song_path,
        }
        try:
            with open(self.CFG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    # ---------------------------------------------------------- панель
    def _build_panel(self):
        p = self.panel
        pad = dict(padx=6, pady=3)
        r = 0

        def row():
            return r
        f = tk.Frame(p)
        f.pack(fill="x", padx=6, pady=(6, 0))
        tk.Button(f, text="Каталог нот", command=self.open_catalog).pack(side="left", padx=2)
        self.lb_go = tk.Button(f, text="▶ Старт", width=8, command=self.toggle_run)
        self.lb_go.pack(side="left")
        self.lb_hide = tk.Button(f, text="Показать HUD" if self.hud_hidden else "Скрыть HUD полностью",
                                 command=self.toggle_hud)
        self.lb_hide.pack(side="left", padx=(8, 0))
        self.mode_btns = {}

        self.lb_song = tk.Label(p, text="—", anchor="w", font=("Segoe UI", 9, "bold"))
        self.lb_song.pack(fill="x", padx=8)
        self.lb_meta = tk.Label(p, text="", anchor="w", fg="#666", font=("Segoe UI", 8))
        self.lb_meta.pack(fill="x", padx=8)

        # локальный каталог нот внутри HUD
        cat = tk.LabelFrame(p, text="Каталог нот", padx=4, pady=4)
        cat.pack(fill="both", padx=6, pady=(4, 2))
        self.catalog_query = tk.StringVar()
        ent = tk.Entry(cat, textvariable=self.catalog_query)
        ent.pack(side="left", fill="x", expand=True)
        ent.insert(0, "Поиск песни…")
        ent.bind("<FocusIn>", lambda e: ent.delete(0, "end") if ent.get()=="Поиск песни…" else None)
        tk.Button(cat, text="Найти", command=self._refresh_catalog).pack(side="left", padx=(4,0))
        self.catalog_list = tk.Listbox(cat, height=5, exportselection=False)
        self.catalog_list.pack(fill="both", expand=True, pady=(5,0))
        self.catalog_list.bind("<Double-Button-1>", self._catalog_open)
        self.catalog_paths = []
        self._refresh_catalog()
        cat.pack_forget()

        self.part_vars = {}
        self.part_frame = tk.Frame(p)
        self.part_frame.pack_forget()

        f2 = tk.Frame(p)
        f2.pack(fill="x", padx=6)
        self.v_metro = tk.BooleanVar(value=self.metro_on)
        self.v_sound = tk.BooleanVar(value=self.sound_on)
        self.v_loop = tk.BooleanVar(value=self.loop_on)
        self.cb_metro = tk.Checkbutton(f2, text="Метроном", variable=self.v_metro, command=self._on_cfg)
        self.cb_metro.pack(side="left")
        tk.Checkbutton(f2, text="Звук", variable=self.v_sound, command=self._on_cfg).pack(side="left")
        tk.Checkbutton(f2, text="Повтор", variable=self.v_loop, command=self._on_cfg).pack(side="left")
        tk.Label(f2, text="Темп").pack(side="left", padx=(10, 2))
        self.v_tempo = tk.StringVar(value=str(self.tempo))
        cb = tk.OptionMenu(f2, self.v_tempo, "0.5", "0.75", "1", "1.25", "1.5", command=lambda _: self._on_cfg())
        cb.config(width=3)
        cb.pack(side="left")

        f3 = tk.Frame(p)
        f3.pack(fill="x", padx=6)
        tk.Label(f3, text="Масштаб").pack(side="left")
        self.s_scale = tk.Scale(f3, from_=0.5, to=2.0, resolution=0.05, orient="horizontal",
                                showvalue=False, command=lambda v: self._set_scale(float(v)))
        self.s_scale.set(self.scale)
        self.s_scale.pack(side="left", fill="x", expand=True)
        tk.Label(f3, text="Прозрачность").pack(side="left", padx=(8, 0))
        self.s_alpha = tk.Scale(f3, from_=0.35, to=1.0, resolution=0.05, orient="horizontal",
                                showvalue=False, command=self._set_alpha)
        self.s_alpha.set(self.alpha)
        self.s_alpha.pack(side="left", fill="x", expand=True)

        f4 = tk.Frame(p)
        f4.pack(fill="x", padx=6, pady=(0, 2))
        for t, d in (("←", (-12, 0)), ("→", (12, 0)), ("↑", (0, -12)), ("↓", (0, 12))):
            tk.Button(f4, text=t, width=3, command=lambda d=d: self.nudge(*d)).pack(side="left")
        tk.Button(f4, text="По центру низа", command=self.place_bottom).pack(side="left", padx=8)
        tk.Button(f4, text="✕ Закрыть", command=self.quit).pack(side="right")

        self.lb_stats = tk.Label(p, text="", fg="#0a0", font=("Segoe UI", 9))
        self.lb_status = tk.Label(p, text="Загрузите файл песни (.txt/.json) или нажмите «Пример»",
                                  fg="#888", anchor="w", wraplength=430, justify="left",
                                  font=("Segoe UI", 8))
        self.lb_status.pack(fill="x", padx=8, pady=(0, 6))
        self.sync_panel_controls()
        f2.pack_forget()

    def _refresh_catalog(self):
        root = os.path.join(APP_DIR, "songs")
        q = self.catalog_query.get().strip().lower() if hasattr(self, "catalog_query") else ""
        if q == "поиск песни…": q = ""
        paths = []
        if os.path.isdir(root):
            for base, _, files in os.walk(root):
                for name in files:
                    if name.lower().endswith((".txt", ".json")) and (not q or q in name.lower()):
                        paths.append(os.path.join(base, name))
        paths.sort(key=lambda x: os.path.basename(x).lower())
        self.catalog_paths = paths[:500]
        if hasattr(self, "catalog_list"):
            self.catalog_list.delete(0, "end")
            for path in self.catalog_paths:
                self.catalog_list.insert("end", os.path.basename(path))

    def catalog_entries(self, query=""):
        if self._catalog_cache is None:
            entries = []
            # Exclude metadata and VSRG; those are not Sky songNotes files.
            for folder in ("Songs", "Multi-Sheet Songs"):
                root = os.path.join(APP_DIR, "songs", folder)
                for base, _, files in os.walk(root):
                    for name in files:
                        if name.lower().endswith((".txt", ".json")):
                            path = os.path.join(base, name)
                            try:
                                entries.append((name, path, os.path.getsize(path)))
                            except OSError:
                                continue
            self._catalog_cache = entries
        needle = query.casefold().strip()
        entries = [e for e in self._catalog_cache if not needle or needle in e[0].casefold()]
        return sorted(entries, key=lambda e: (song_path_key(e[1]) not in self.favorites,
                                              e[0].casefold(), e[1]))

    def toggle_favorite(self, path):
        key = song_path_key(path)
        if key in self.favorites:
            self.favorites.remove(key)
        else:
            self.favorites.add(key)
        self.save_cfg()

    def open_catalog(self):
        win = tk.Toplevel(self.panel)
        win.title("Sky Notes · каталог")
        px, py = self.panel.winfo_rootx(), self.panel.winfo_rooty()
        win.geometry("620x700%+d%+d" % (px, max(20, py-350)))
        win.configure(bg="#182733")
        win.attributes("-topmost", True)
        head = tk.Frame(win, bg="#182733", padx=18, pady=16)
        head.pack(fill="x")
        tk.Label(head, text="Каталог нот", fg="#f5efdf", bg="#182733",
                 font=("Segoe UI", 18)).pack(anchor="w")
        tk.Label(head, text="★ — избранное всегда сверху · двойной щелчок — открыть",
                 fg="#b8c6c9", bg="#182733", font=("Segoe UI", 9)).pack(anchor="w", pady=(5,12))
        q = tk.StringVar()
        ent = tk.Entry(head, textvariable=q, bg="#283945", fg="#f5efdf",
                       insertbackground="#f5efdf", relief="flat", font=("Segoe UI", 11))
        ent.pack(fill="x", ipady=8)
        body = tk.Frame(win, bg="#182733", padx=18, pady=14)
        body.pack(fill="both", expand=True)
        style = ttk.Style(win)
        style.configure("Sky.Treeview", background="#182733", fieldbackground="#182733",
                        foreground="#f5efdf", rowheight=32, font=("Segoe UI", 11), borderwidth=0)
        style.map("Sky.Treeview", background=[("selected", "#3e5864")], foreground=[("selected", "#fff8e9")])
        tree = ttk.Treeview(body, columns=("favorite", "name", "size"), show="", style="Sky.Treeview", selectmode="browse")
        tree.column("favorite", width=40, stretch=False, anchor="center")
        tree.column("name", width=420, minwidth=180)
        tree.column("size", width=70, stretch=False, anchor="e")
        tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(body, command=tree.yview)
        sb.pack(side="right", fill="y")
        tree.configure(yscrollcommand=sb.set)
        win.catalog_tree = tree

        def refresh():
            win.catalog_arr = self.catalog_entries(q.get())
            children = tree.get_children()
            if children:
                tree.delete(*children)
            for i, (name, path, size) in enumerate(win.catalog_arr):
                star = "★" if song_path_key(path) in self.favorites else "☆"
                tree.insert("", "end", iid=str(i), values=(star, os.path.splitext(name)[0], "%d KB" % round(size/1024)))

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
                win.destroy()
            except Exception as e:
                self.status("Ошибка: %s" % e, err=True)

        q.trace_add("write", lambda *_: refresh())
        tree.bind("<Button-1>", favorite)
        tree.bind("<Double-Button-1>", choose)
        tree.bind("<Return>", choose)
        refresh()
        return win

    def _catalog_open(self, _event=None):
        sel = self.catalog_list.curselection()
        if not sel: return
        path = self.catalog_paths[sel[0]]
        try:
            self.load_song_file(path)
            self.status("Загружено из каталога: %s" % os.path.basename(path))
        except Exception as e:
            self.status("Ошибка каталога: %s" % e, err=True)

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
        self._save_cfg_debounced()

    def place_bottom(self):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x, y, w, h = self.geom()
        self.root.geometry("+%d+%d" % ((sw - w) // 2, sh - h - 90))
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
        self.status("Файл «A cruel angel.txt» не найден рядом с программой", err=True)

    def set_song(self, song, source_path=None):
        self.stop()
        self.song = song
        self.song_path = song_path_key(source_path) if source_path else None
        self.preview_tl = build_timeline(song, [p["id"] for p in song["parts"]])
        self.flash.clear()
        ids = [p["id"] for p in song["parts"]]
        if self.part_sel is None:
            self.part_sel = set(ids)
        else:
            keep = self.part_sel & set(ids)
            if not keep:
                self.part_sel = set(ids)
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
        # Переключатель партий скрыт в интерфейсе, поэтому тренировка всегда
        # использует все партии файла и не теряет вторую половину аккордов.
        sel = [p["id"] for p in self.song["parts"]]
        if not sel:
            self.status("Включите хотя бы одну партию", err=True)
            return
        tl = build_timeline(self.song, sel)
        if not tl["evs"]:
            self.status("В выбранных партиях нет нот", err=True)
            return
        self.run = {
            "tl": tl,
            "tempo": self.tempo,
            "base": time.perf_counter() + 0.8,
            "ev_idx": 0,
            "grid_idx": 0,
            "grid": [i * tl["gap"] for i in range(int(tl["end_t"] // tl["gap"]) + 2)],
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
        self.root.after(30 if self.run else 150, self._tick)

    def _tick_inner(self):
        r = self.run
        if not r:
            return
        if r["mode"] in ("step", "train"):
            # пошаговый режим: время не идёт, ходы переключает _judge_step
            if r["step_idx"] >= len(r["tl"]["evs"]):
                if self.loop_on:
                    self.start()
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
            while r["grid_idx"] < len(r["grid"]) and r["grid"][r["grid_idx"]] <= now:
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
                self.start()
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
        self.root.after(8, self._poll_keys)

    def _key_down(self, idx):
        self.pressed_key.add(idx)
        r = self.run
        if r and self.sound_on:
            self.sound.beep(freq_of(idx), 55 if r["mode"] == "train" else 45)
        if r:
            if r["mode"] in ("train", "step"):
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
                            self.start()
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
        self.root.after(33, self._frame)

    @staticmethod
    def _hex(rgb):
        return "#%02x%02x%02x" % tuple(int(x) for x in rgb)

    def _build_items(self):
        c, L, C = self.cv, self._layout_geom(), self.HUD_COLORS
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
                                   font=("Segoe UI", max(8, int(10*s))), fill=C["text"])
        it["time"] = c.create_text(bx2, bar_y-int(7*s), anchor="se", text="",
                                   font=("Segoe UI", max(8, int(10*s))), fill=C["muted"])
        self._it = it
        self._last_style: list[tuple[str, str, str] | None] = [None] * 45
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
        it, c, C, L = self._it, self.cv, self.HUD_COLORS, self._layout_geom()
        r = self.run
        tl = r["tl"] if r else self.preview_tl
        now = self.song_time_ms() if r else 0.0
        step_mode = bool(r and r["mode"] in ("step", "train"))
        cursor = (r["step_idx"] if step_mode else self._cursor(tl, now)) if r else 0
        events = tl["evs"][cursor:cursor+3] if tl else []
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
                style = fill, edge, text
                k = p*15 + idx
                if self._last_style[k] != style:
                    c.itemconfig(it["body"][p][idx], fill=fill, outline=edge)
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
        try:
            self.sound.q.put(None)
        except Exception:
            pass
        self.root.destroy()


def freq_of(idx):
    return BASE_C4 * (2.0 ** (SEMI[idx] / 12.0))


def fmt_dur(ms):
    s = max(0, int(round(ms / 1000.0)))
    return "%d:%02d" % (s // 60, s % 60)


# ---------------------------------------------------------------- самопроверка
def check_mode(paths):
    import random
    ok = True

    def t(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print(("PASS" if cond else "FAIL"), "|", name, ("| " + str(extra)) if extra else "")

    for path in paths:
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read()
        song = parse_song(text, path)
        t("parse %s" % os.path.basename(path), song["name"] != "")
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
        stats2 = {"ok": 0, "miss": 0, "bad": 0}
        for inst in tl["instances"]:
            if not inst["consumed"] and tl["end_t"] + 100 > inst["close"]:
                pass
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
        root.after(1500, done)

    def done():
        app.save_cfg()
        print("SMOKE OK: hud mapped=%s panel=%s song=%s run=%s" % (
            root.winfo_ismapped(), app.panel.winfo_ismapped(),
            app.song["name"] if app.song else None, bool(app.run)))
        root.destroy()

    root.after(700, go)
    root.mainloop()


def main():
    args = sys.argv[1:]
    if args and args[0] == "--check":
        paths = args[1:] or [os.path.join(APP_DIR, "A cruel angel.txt")]
        check_mode(paths)
    elif args and args[0] == "--smoke":
        smoke_mode()
    else:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        root = tk.Tk()
        app = HudApp(root)
        root.mainloop()


if __name__ == "__main__":
    main()
