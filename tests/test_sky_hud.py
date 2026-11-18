"""Regression tests; real Tk widgets use an off-screen scratch window only."""
import gc
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
import tkinter as tk
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sky_hud as hud


class HudTests(unittest.TestCase):
    def setUp(self):
        # Finalize previous destroyed Tk cycles on the main thread, not a network worker.
        gc.collect()
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("SKY_HUD_TEST_TMPDIR"))
        self.cfg = Path(self.tmp.name) / "config.json"
        self.override = patch.object(hud.HudApp, "CFG_FILE", str(self.cfg))
        self.override.start()
        self.original_app_dir = hud.APP_DIR
        fixture = Path(self.tmp.name) / "app"
        (fixture / "songs" / "Songs" / "A").mkdir(parents=True)
        (fixture / "songs" / "Multi-Sheet Songs" / "B").mkdir(parents=True)
        (fixture / "songs" / "User Songs").mkdir(parents=True)
        fixture_song = '{"name":"Fixture","songNotes":[{"time":0,"key":"1Key0"}]}'
        (fixture / "songs/Songs/A/alpha.txt").write_text(fixture_song, encoding="utf-8")
        (fixture / "songs/Songs/A/beta.txt").write_text(fixture_song, encoding="utf-8")
        (fixture / "songs/Multi-Sheet Songs/B/gamma.txt").write_text(fixture_song, encoding="utf-8")
        (fixture / "A cruel angel.txt").write_text(
            '{"name":"Demo","songNotes":[{"time":0,"key":"1Key0"}]}', encoding="utf-8")
        hud.APP_DIR = str(fixture)
        self.custom_override = patch.object(hud.HudApp, "CUSTOM_SONG_DIR", str(fixture / "songs/User Songs"))
        self.custom_override.start()
        self.root = tk.Tk()
        self.root.geometry("+10000+10000")
        self.app = hud.HudApp(self.root)
        self.app.panel.geometry("+10000+10000")
        self.root.update_idletasks()

    def tearDown(self):
        self.app.quit()
        self.override.stop()
        self.custom_override.stop()
        hud.APP_DIR = self.original_app_dir
        self.tmp.cleanup()

    def test_round3_slider_hover_and_hud_theme(self):
        for theme in ("dark", "light"):
            self.app.set_theme(theme)
            self.root.update()
            def luminance(color):
                rgb = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
                rgb = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb]
                return sum(v * w for v, w in zip(rgb, (.2126, .7152, .0722)))
            def contrast(a, b):
                lo, hi = sorted((luminance(a), luminance(b)))
                return (hi + .05) / (lo + .05)
            for slider in (self.app.s_scale, self.app.s_alpha):
                self.assertNotEqual(slider.cget("activebackground"), slider.cget("troughcolor"))
                self.assertNotEqual(slider.cget("activebackground"), slider.cget("bg"))
                self.assertGreater(contrast(slider.cget("activebackground"), slider.cget("troughcolor")), 2)
                self.assertNotEqual(slider.cget("bg"), slider.cget("troughcolor"))
            colors = self.app.hud_colors()
            self.assertEqual(colors["transparent"], "#0b1230")
            if theme == "dark":
                self.assertEqual(colors, self.app.HUD_COLORS)
            else:
                self.assertNotEqual(colors["panel"], self.app.HUD_COLORS["panel"])
                self.assertEqual(colors["text"], "#1c2430")
            self.app._build_items()
            self.app._animate_items()
            self.assertEqual(self.app.cv.itemcget(self.app._it["body"][0][14], "fill"), colors["key"])

    def test_round3_catalog_no_hint_and_song_menu(self):
        win = self.app.open_catalog()
        try:
            self.root.update()
            def labels(widget):
                return ([widget.cget("text")] if widget.winfo_class() == "Label" else []) + [x for child in widget.winfo_children() for x in labels(child)]
            self.assertFalse(any("избранное на первой странице" in text for text in labels(win)))
            self.assertEqual(win.catalog_song_menu.entrycget(0, "label"), "Переименовать…")
        finally:
            win.destroy()

    def test_round3_rename_updates_references_and_rejects_invalid(self):
        source = Path(hud.APP_DIR) / "songs/Songs/A/alpha.txt"
        self.app.load_song_file(str(source))
        old = self.app.song_path
        self.app.toggle_favorite(str(source))
        win = self.app.open_catalog()
        try:
            self.root.update()
            for invalid in ("", " ... ", "bad/name", "bad:name", "BETA"):
                self.assertFalse(self.app.rename_song(str(source), invalid))
                self.assertTrue(source.exists())
            self.assertTrue(self.app.rename_song(str(source), "Renamed"))
            dest = source.with_name("Renamed.txt")
            new = hud.song_path_key(str(dest))
            self.assertTrue(dest.exists())
            self.assertFalse(source.exists())
            self.assertNotIn(old, self.app.favorites)
            self.assertIn(new, self.app.favorites)
            self.assertEqual(self.app.song_path, new)
            self.assertEqual(self.app.cfg["last_song"], new)
            self.assertEqual(self.app.lb_song.cget("text"), "Renamed")
            self.assertTrue(any(row[1] == str(dest) for row in win.catalog_arr))
        finally:
            win.destroy()

    def test_round3_rename_preserves_search_page_and_handles_oserror(self):
        folder = Path(hud.APP_DIR) / "songs/Songs/A"
        source = folder / "alpha.txt"
        with patch.object(hud.HudApp, "CATALOG_LIMIT", 1):
            win = self.app.open_catalog()
            try:
                win.catalog_query.set("a")
                win.catalog_goto_page(1)
                page = win.catalog_page
                with patch.object(hud.os, "rename", side_effect=OSError("denied")):
                    self.assertFalse(self.app.rename_song(str(source), "another"))
                self.assertIn("denied", self.app.lb_status.cget("text"))
                self.assertTrue(source.exists())
                self.assertTrue(self.app.rename_song(str(source), "aardvark"))
                self.assertEqual(win.catalog_query.get(), "a")
                self.assertEqual(win.catalog_page, page)
            finally:
                win.destroy()

    def test_round3_song_context_menu_targets_clicked_row(self):
        win = self.app.open_catalog()
        try:
            win.geometry("620x700+10000+10000")
            win.catalog_goto_page(1)
            self.root.update()
            tree = win.catalog_tree
            iid = tree.get_children()[0]
            x, y, w, h = tree.bbox(iid)
            with patch.object(win.catalog_song_menu, "tk_popup") as popup:
                tree.event_generate("<Button-3>", x=x + 10, y=y + h // 2)
                self.root.update()
                popup.assert_called_once()
            self.assertEqual(tree.selection(), (iid,))
            expected = win.catalog_arr[int(iid)][1]
            with patch.object(hud.simpledialog, "askstring", return_value=None) as dialog:
                win.catalog_song_menu.invoke(0)
                self.assertEqual(dialog.call_args.kwargs["initialvalue"], Path(expected).stem)
            self.assertEqual(win.catalog_menu.entrycget(0, "label"), "Вставить")
        finally:
            win.destroy()

    def test_theme_live_persistence_and_system(self):
        for theme, dark in (("light", 0), ("dark", 1)):
            self.app.set_theme(theme)
            self.root.update()
            self.assertEqual(self.app.palette, hud.THEMES[theme])
            self.assertEqual(self.app.panel.cget("bg"), hud.THEMES[theme]["bg"])
            self.assertEqual(self._titlebar_dark(self.app.panel), dark)
            self.assertEqual(json.loads(self.cfg.read_text(encoding="utf-8"))["theme"], theme)
        with patch.object(hud, "system_theme", return_value="light") as system:
            self.app.set_theme("system")
            self.assertEqual(self.app.theme, "light")
            system.assert_called_once()
        self.assertEqual(json.loads(self.cfg.read_text(encoding="utf-8"))["theme"], "system")

    def test_settings_theme_menu_changes_all_open_windows(self):
        self.app.set_theme("light")
        self.app.set_song(hud.parse_song('{"songNotes":[{"time":0,"key":"1Key0"}]}'))
        catalog = self.app.open_catalog()
        mapwin = self.app.open_map()
        settings = self.app.open_settings()
        self.root.update()
        settings.theme_menu.current(2)
        settings.theme_menu.event_generate("<<ComboboxSelected>>")
        self.assertEqual(str(settings.theme_menu.cget("state")), "readonly")
        self.assertEqual(tuple(settings.theme_menu.cget("values")), ("Системная", "Светлая", "Тёмная"))
        settings.theme_menu.tk.call(settings.theme_menu.cget("postcommand"))
        popdown = settings.tk.call("ttk::combobox::PopdownWindow", settings.theme_menu)
        self.assertEqual(settings.tk.call(popdown + ".f.l", "cget", "-background"), hud.THEMES["dark"]["panel"])
        self.root.update()
        self.assertEqual(self.app.theme, "dark")
        self.assertEqual(settings.theme_var.get(), "Тёмная")
        self.assertEqual(settings.cget("bg"), hud.THEMES["dark"]["bg"])
        self.assertEqual(self._titlebar_dark(settings), 1)
        self.assertFalse(catalog.winfo_exists())
        self.assertFalse(mapwin.winfo_exists())
        windows = [w for w in self.app.panel.winfo_children() if isinstance(w, tk.Toplevel)]
        for title in ("каталог", "карта песни"):
            win = next(w for w in windows if title in w.title())
            self.assertEqual(win.cget("bg"), hud.THEMES["dark"]["bg"])
            self.assertEqual(self._titlebar_dark(win), 1)

    def test_panel_grid_spacing_height_and_stable_toggle(self):
        self.root.update()
        buttons = self.app._panel_buttons
        before = self.app.panel.winfo_width(), self.app.panel.winfo_rootx()
        heights = [b.winfo_height() for b in buttons]
        self.assertGreaterEqual(min(heights), 30)
        self.assertLessEqual(max(heights) - min(heights), 2)
        rows = ([buttons[0], buttons[1], buttons[4]], [buttons[2], buttons[3]])
        for row in rows:
            widths = [b.winfo_width() for b in row]
            self.assertLessEqual(max(widths) - min(widths), 2)
            gaps = [b.winfo_rootx() - (a.winfo_rootx() + a.winfo_width()) for a, b in zip(row, row[1:])]
            self.assertTrue(all(8 <= gap <= 10 for gap in gaps), gaps)
        vertical_gap = rows[1][0].winfo_rooty() - (rows[0][0].winfo_rooty() + rows[0][0].winfo_height())
        self.assertTrue(8 <= vertical_gap <= 10, vertical_gap)
        self.assertTrue(all(b.winfo_rootx() + b.winfo_width() <= before[1] + before[0] for b in buttons))
        self.app.lb_hide.invoke()
        self.root.update()
        self.assertLessEqual(abs(self.app.panel.winfo_width() - before[0]), 2)
        self.assertEqual(self.app.panel.winfo_rootx(), before[1])
        self.app.lb_hide.invoke()
        self.root.update()
        self.assertLessEqual(abs(self.app.panel.winfo_width() - before[0]), 2)

    def test_theme_preserves_catalog_query_and_page(self):
        with patch.object(hud.HudApp, "CATALOG_LIMIT", 1):
            catalog = self.app.open_catalog()
            catalog.catalog_query.set("a")
            catalog.catalog_next.invoke()
            self.assertEqual(catalog.catalog_page, 1)
            self.app.set_theme("dark")
            self.root.update()
            replacement = next(w for w in self.app.panel.winfo_children()
                               if isinstance(w, tk.Toplevel) and "каталог" in w.title())
            self.assertEqual(replacement.catalog_query.get(), "a")
            self.assertEqual(replacement.catalog_page, 1)

    def test_catalog_few_favorites_and_scrollbars_follow_both_palettes(self):
        from tkinter import ttk
        path = str(Path(hud.APP_DIR) / "songs/Songs/A/alpha.txt")
        self.app.favorites = {hud.song_path_key(path)}
        self.app.set_theme("dark")
        win = self.app.open_catalog()
        self.root.update()
        self.assertEqual(len(win.catalog_tree.get_children()), 1)
        style = ttk.Style(win)
        self.assertEqual(style.lookup("Sky.Treeview", "fieldbackground"), hud.THEMES["dark"]["bg"])
        self.assertEqual(style.lookup("Sky.Treeview", "background"), hud.THEMES["dark"]["bg"])
        self.assertEqual(style.lookup("Sky.Vertical.TScrollbar", "troughcolor"), hud.THEMES["dark"]["bg"])
        self.assertEqual(style.lookup("Sky.Vertical.TScrollbar", "arrowcolor"), hud.THEMES["dark"]["text"])
        self.assertEqual(win.catalog_scrollbar.cget("style"), "Sky.Vertical.TScrollbar")
        self.app.set_song(hud.parse_song('{"songNotes":[{"time":0,"key":"1Key0"}]}'))
        mapwin = self.app.open_map()
        self.assertEqual(mapwin.map_scrollbar.cget("style"), "Sky.Vertical.TScrollbar")
        self.app.set_theme("light")
        self.root.update()
        self.assertEqual(style.lookup("Sky.Vertical.TScrollbar", "troughcolor"), hud.THEMES["light"]["bg"])
        self.assertEqual(style.lookup("Sky.Vertical.TScrollbar", "background"), hud.THEMES["light"]["button"])
        self.assertEqual(style.lookup("Sky.Treeview", "fieldbackground"), hud.THEMES["light"]["bg"])

    def test_missing_songs_points_to_separate_songs_archive(self):
        empty = Path(self.tmp.name) / "app-only"
        empty.mkdir()
        original = hud.APP_DIR
        try:
            hud.APP_DIR = str(empty)
            self.app.song = None
            self.app.load_demo()
        finally:
            hud.APP_DIR = original
        self.assertIsNone(self.app.song)
        self.assertIn("SkyHUD-Songs", self.app.lb_status.cget("text"))

    def test_missing_demo_with_installed_songs_asks_to_pick_from_catalog(self):
        appdir = Path(self.tmp.name) / "with-songs"
        (appdir / "songs" / "Songs").mkdir(parents=True)
        (appdir / "songs/Songs/x.txt").write_text(
            '{"songNotes":[{"time":0,"key":"1Key0"}]}', encoding="utf-8")
        original = hud.APP_DIR
        try:
            hud.APP_DIR = str(appdir)
            self.app.song = None
            self.app.load_demo()
        finally:
            hud.APP_DIR = original
        self.assertIsNone(self.app.song)
        self.assertIn("Каталог нот", self.app.lb_status.cget("text"))

    def test_new_settings_start_in_manual_step_mode(self):
        # Manual aid: Start must wait for the player's keys, not run the song by clock.
        self.assertEqual(self.app.mode, "step")
        self.assertEqual(self.app.run, None)

    def test_start_waits_for_keypress_instead_of_advancing_by_time(self):
        song = hud.parse_song('{"songNotes":['
                              '{"time":0,"key":"1Key0"},'
                              '{"time":250,"key":"1Key1"},'
                              '{"time":500,"key":"1Key2"}]}')
        self.app.set_song(song)
        self.app.start()
        self.assertIsNotNone(self.app.run)
        self.assertEqual(self.app.run["mode"], "step")
        # Делаем вид, что прошло очень много времени: время не должно двигать ход.
        self.app.song_time_ms = lambda: 10 ** 9
        for _ in range(5):
            self.app._tick_inner()
        self.assertEqual(self.app.run["step_idx"], 0)
        self.assertEqual(self.app.run["ev_idx"], 0)
        self.assertEqual(self.app.run["stats"]["miss"], 0)

    def test_wrong_key_does_not_advance_manual_step(self):
        song = hud.parse_song('{"songNotes":[{"time":0,"key":"1Key0"},'
                              '{"time":250,"key":"1Key1"}]}')
        self.app.set_song(song)
        self.app.start()
        self.app._judge_step(1)
        self.assertEqual(self.app.run["step_idx"], 0)
        self.assertEqual(self.app.run["stats"]["bad"], 1)
        self.app._judge_step(0)
        self.assertEqual(self.app.run["step_idx"], 1)

    def test_whole_song_practice_keeps_manual_step_mode(self):
        song = hud.parse_song('{"songNotes":[{"time":0,"key":"1Key0"},'
                              '{"time":250,"key":"1Key1"}]}')
        self.app.set_song(song)
        win = self.app.open_map()
        win.geometry("+10000+10000")
        try:
            win.practice_button.invoke()
            self.assertEqual(self.app.mode, "step")
            win.clear_practice_button.invoke()
            self.assertEqual(self.app.mode, "step")
            self.assertIsNone(self.app.practice_range)
            self.app.start()
            self.assertEqual(self.app.run["mode"], "step")
        finally:
            win.destroy()

    def test_catalog_pages_show_300_and_search_finds_songs_beyond_the_first_page(self):
        folder = Path(hud.APP_DIR) / "songs" / "Songs" / "Bulk"
        folder.mkdir(parents=True, exist_ok=True)
        note = '{"songNotes":[{"time":0,"key":"1Key0"}]}'
        for i in range(340):
            (folder / ("zz bulk %03d.txt" % i)).write_text(note, encoding="utf-8")
        self.app._catalog_cache = None
        win = self.app.open_catalog()
        try:
            self.root.update()
            tree = win.catalog_tree
            self.assertEqual(win.catalog_mode, "favorites")
            self.assertEqual(len(tree.get_children()), 0)
            self.assertIn("Избранное", win.catalog_count.cget("text"))
            win.catalog_next.invoke()
            self.root.update()
            self.assertEqual(len(tree.get_children()), 300)
            self.assertIn("стр. 2 из 3", win.catalog_count.cget("text"))
            win.catalog_next.invoke()
            self.root.update()
            expected_last = len([e for e in self.app.catalog_entries("")
                                 if hud.song_path_key(e[1]) not in self.app.favorites]) - 300
            self.assertEqual(len(tree.get_children()), expected_last)
            self.assertIn("стр. 3 из 3", win.catalog_count.cget("text"))
            self.assertEqual(win.catalog_next.cget("state"), "disabled")
            win.catalog_prev.invoke()
            self.root.update()
            self.assertEqual(len(tree.get_children()), 300)
            win.catalog_query.set("zz bulk 339")
            self.root.update()
            rows = tree.get_children()
            self.assertEqual(len(rows), 1)
            self.assertIn("zz bulk 339", tree.item(rows[0], "values")[1])
            self.assertIn("Найдено: 1", win.catalog_count.cget("text"))
            self.assertEqual(win.catalog_mode, "search")
        finally:
            win.destroy()

    def test_favorites_page_comes_first_and_is_excluded_from_song_pages(self):
        entries = self.app.catalog_entries("")
        self.assertGreaterEqual(len(entries), 2)
        target = entries[0][1]
        stem = os.path.splitext(os.path.basename(target))[0]
        self.app.toggle_favorite(target)
        win = self.app.open_catalog()
        try:
            self.root.update()
            self.assertEqual(win.catalog_mode, "favorites")
            self.assertEqual([e[1] for e in win.catalog_arr], [target])
            self.assertIn("Избранное: 1", win.catalog_count.cget("text"))
            self.assertEqual(win.catalog_fav.cget("state"), "disabled")
            win.catalog_next.invoke()
            self.root.update()
            self.assertEqual(win.catalog_mode, "songs")
            names = [win.catalog_tree.item(i, "values")[1] for i in win.catalog_tree.get_children()]
            self.assertNotIn(stem, names)
            self.assertEqual(win.catalog_fav.cget("state"), "normal")
            win.catalog_fav.invoke()
            self.root.update()
            self.assertEqual(win.catalog_mode, "favorites")
            win.catalog_query.set("zzz")
            self.root.update()
            self.assertEqual(win.catalog_mode, "search")
            self.assertEqual(win.catalog_fav.cget("state"), "disabled")
        finally:
            win.destroy()

    def test_catalog_paste_works_from_ctrl_v_and_context_menu(self):
        win = self.app.open_catalog()
        try:
            self.root.update()
            self.assertTrue(win.catalog_entry.bind("<Control-v>"))
            self.root.clipboard_clear()
            self.root.clipboard_append("Flower Dance")
            self.root.update()
            win.catalog_paste()
            self.root.update()
            self.assertEqual(win.catalog_query.get(), "Flower Dance")
            win.catalog_entry.delete(0, "end")
            self.root.clipboard_clear()
            self.root.clipboard_append("Alpha")
            self.root.update()
            win.catalog_menu.invoke(0)
            self.root.update()
            self.assertEqual(win.catalog_query.get(), "Alpha")
        finally:
            win.destroy()

    @staticmethod
    def _titlebar_dark(win):
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
        value = ctypes.c_int(-1)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
        return value.value

    def test_light_and_dark_themes_are_applied_to_panel_and_catalog(self):
        for theme in ("light", "dark"):
            with patch.object(hud, "system_theme", return_value=theme):
                root2 = tk.Tk()
                root2.geometry("+10000+10000")
                app2 = hud.HudApp(root2)
                try:
                    self.assertEqual(app2.theme, theme)
                    self.assertEqual(app2.panel.cget("bg"), hud.THEMES[theme]["bg"])
                    self.assertEqual(app2.lb_go.cget("bg"), hud.THEMES[theme]["accent"])
                    self.root.update()
                    self.assertEqual(self._titlebar_dark(app2.panel), 1 if theme == "dark" else 0)
                    win = app2.open_catalog()
                    win.geometry("+10000+10000")
                    self.root.update()
                    self.assertEqual(win.cget("bg"), hud.THEMES[theme]["bg"])
                    self.assertEqual(win.catalog_tree.cget("style"), "Sky.Treeview")
                    self.assertEqual(self._titlebar_dark(win), 1 if theme == "dark" else 0)
                    win.destroy()
                finally:
                    app2.quit()

    def test_loop_is_enabled_by_default_for_new_settings(self):
        self.assertTrue(self.app.loop_on)
        self.assertTrue(self.app.v_loop.get())

    def test_sound_bus_shutdown_joins_worker(self):
        bus = hud.SoundBus()
        bus.close()
        self.assertFalse(bus.t.is_alive())

    def test_update_package_members_are_safe_and_preserve_user_data(self):
        import zipfile
        package = Path(self.tmp.name) / "update.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("SkyHUD.exe", b"new")
            archive.writestr("SkyHUD-Updater.exe", b"updater")
            archive.writestr("README-PORTABLE.md", b"readme")
        import sky_updater
        self.assertEqual(set(sky_updater.safe_members(str(package))),
                         {"SkyHUD.exe", "SkyHUD-Updater.exe", "README-PORTABLE.md"})
        install = Path(self.tmp.name) / "install"
        install.mkdir()
        (install / "SkyHUD.exe").write_bytes(b"old")
        (install / "sky_hud_config.json").write_text("keep", encoding="utf-8")
        (install / "songs").mkdir()
        (install / "songs/User Songs").mkdir()
        (install / "songs/User Songs/mine.txt").write_text("keep", encoding="utf-8")
        sky_updater.install_package(str(package), str(install), restart=False)
        self.assertEqual((install / "SkyHUD.exe").read_bytes(), b"new")
        self.assertEqual((install / "sky_hud_config.json").read_text(encoding="utf-8"), "keep")
        self.assertEqual((install / "songs/User Songs/mine.txt").read_text(encoding="utf-8"), "keep")

    def test_settings_buttons_download_verified_package_then_launch_sidecar(self):
        import io
        import time
        import zipfile
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("SkyHUD.exe", b"hud")
            archive.writestr("SkyHUD-Updater.exe", b"updater")
        data = stream.getvalue()
        import hashlib
        release = {"tag_name": "v999.0.0", "assets": [{"name": "SkyHUD-Update.zip",
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(), "size": len(data),
            "browser_download_url": "https://github.com/Mafsolin/sky-hud/releases/download/v999.0.0/SkyHUD-Update.zip"}]}
        win = self.app.open_settings()
        win.geometry("+10000+10000")
        def wait_until(predicate):
            deadline = time.monotonic() + 3
            while not predicate() and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
            self.assertTrue(predicate())
        prepared = None
        with patch.object(hud.urllib.request, "urlopen", side_effect=[
                io.BytesIO(json.dumps(release).encode()), io.BytesIO(data)]), \
                patch.object(hud, "launch_update") as launch, patch.object(self.app, "quit") as quit_app:
            win.update_button.invoke()
            wait_until(lambda: str(win.install_button.cget("state")) == "normal")
            win.install_button.invoke()
            wait_until(lambda: launch.called)
            prepared = launch.call_args.args[0]
            self.assertEqual(Path(prepared[2]).read_bytes(), b"updater")
            self.assertTrue(quit_app.called)
        if prepared:
            shutil.rmtree(prepared[0])
        win.destroy()

    def test_closing_settings_during_download_discards_prepared_files(self):
        import threading
        import time
        work = Path(self.tmp.name) / "prepared-update"
        work.mkdir()
        release_worker = threading.Event()
        started = threading.Event()
        def delayed_download(_release):
            started.set()
            release_worker.wait(2)
            return str(work), str(work / "package.zip"), str(work / "updater.exe"), "0" * 64
        win = self.app.open_settings()
        win.geometry("+10000+10000")
        win._release = {"assets": [{"name": "SkyHUD-Update.zip", "browser_download_url": "https://github.com/Mafsolin/sky-hud/"}]}
        win.install_button.config(state="normal")
        with patch.object(hud, "download_update", side_effect=delayed_download):
            win.install_button.invoke()
            self.assertTrue(started.wait(1))
            win.destroy()
            release_worker.set()
            deadline = time.monotonic() + 1
            while work.exists() and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
        self.assertFalse(work.exists())

    def test_settings_window_shows_creator_repo_and_update_button(self):
        win = self.app.open_settings()
        try:
            self.assertIn("@mafsolin", win.creator_label.cget("text"))
            self.assertEqual(win.update_button.cget("text"), "Проверить обновления")
            self.assertIn("Mafsolin/sky-hud", win.repo_label.cget("text"))
        finally:
            win.destroy()

    def test_update_message_rejects_malformed_payload(self):
        self.assertIn("некоррект", hud.update_message([]).lower())

    def test_update_message_distinguishes_current_and_new_release(self):
        current = hud.update_message({"tag_name": "v" + hud.APP_VERSION})
        newer = hud.update_message({"tag_name": "v999.0.0", "html_url": "https://github.com/Mafsolin/sky-hud/releases/tag/v999.0.0"})
        self.assertIn("актуальна", current.lower())
        self.assertIn("доступна", newer.lower())

    def test_hud_contains_subtle_creator_credit(self):
        self.app.load_demo()
        self.app._build_items()
        texts = [self.app.cv.itemcget(i, "text") for i in self.app.cv.find_all()
                 if self.app.cv.type(i) == "text"]
        self.assertIn("@mafsolin", texts)

    def test_hud_and_control_panel_have_no_redundant_hints(self):
        self.app.load_demo()
        self.app.start()
        self.app.cv.configure(width=1, height=1)
        self.app._build_items()
        self.app._animate_items()
        texts = [self.app.cv.itemcget(i, "text") for i in self.app.cv.find_all()
                 if self.app.cv.type(i) == "text"]
        self.assertFalse(any(s.startswith(("СЕЙЧАС", "+1", "+2", "ДАЛЬШЕ")) for s in texts), texts)
        self.assertFalse(self.app.lb_stats.winfo_manager(), "Statistics should not be shown")
        self.assertEqual(sum(s in hud.CODES for s in texts), 45)

    def test_hide_button_hides_only_hud_and_restores_it(self):
        self.app.load_demo()
        self.app.start()
        run = self.app.run
        self.app.lb_hide.invoke()
        self.root.update_idletasks()
        self.assertFalse(self.root.winfo_ismapped())
        self.assertTrue(self.app.panel.winfo_ismapped())
        self.assertIs(self.app.run, run)
        self.assertEqual(self.app.lb_hide.cget("text"), "Показать HUD")
        self.app.lb_hide.invoke()
        self.root.update_idletasks()
        self.assertTrue(self.root.winfo_ismapped())
        self.assertEqual(self.app.lb_hide.cget("text"), "Скрыть HUD полностью")

    def test_catalog_star_pins_song_and_persists_favorite(self):
        entries = self.app.catalog_entries("")
        self.assertGreater(len(entries), 2)
        target = entries[-1][1]
        win = self.app.open_catalog()
        win.geometry("620x700+10000+10000")
        tree = win.catalog_tree
        try:
            self.root.update()
            win.catalog_next.invoke()
            self.root.update()
            row = next(i for i, entry in enumerate(win.catalog_arr) if entry[1] == target)
            iid = tree.get_children()[row]
            self.root.update()
            tree.see(iid)
            self.root.update()
            x, y, width, height = tree.bbox(iid, "favorite")
            tree.event_generate("<Button-1>", x=x+width//2, y=y+height//2)
            self.root.update()
            win.catalog_fav.invoke()
            self.root.update()
            self.assertEqual(win.catalog_arr[0][1], target)
            self.assertEqual(tree.item(tree.get_children()[0], "values")[0], "★")
            self.app.save_cfg()
            self.assertIn(hud.song_path_key(target), self.app.load_cfg()["favorites"])
            self.assertEqual(self.app.catalog_entries(os.path.basename(target)[:12])[0][1], target)
            tree.event_generate("<Button-1>", x=20, y=tree.bbox(tree.get_children()[0])[1]+8)
            self.root.update()
            self.assertNotIn(hud.song_path_key(target), self.app.favorites)
        finally:
            win.destroy()

    def test_last_song_restores_without_autostart_and_start_resets_position(self):
        import json
        path = Path(self.tmp.name) / "chosen.txt"
        path.write_text(json.dumps([{"name": "Remember me", "songNotes": [
            {"time": 0, "key": "1Key0"}, {"time": 250, "key": "1Key3"}]}]), encoding="utf-8")
        self.app.load_song_file(str(path))
        self.app.start()
        self.app._judge_step(0)
        self.assertEqual(self.app.run["step_idx"], 1)
        self.app.save_cfg()
        root2 = tk.Tk()
        root2.geometry("+10000+10000")
        app2 = hud.HudApp(root2)
        app2.panel.geometry("+10000+10000")
        try:
            self.assertEqual(app2.song["name"], "Remember me")
            self.assertIsNone(app2.run)
            self.assertEqual(app2.lb_go.cget("text"), "▶ Старт")
            app2.start()
            self.assertEqual(app2.run["step_idx"], 0)
        finally:
            app2.quit()

    def test_preferences_survive_restart_with_hud_hidden(self):
        import json
        self.app.favorites = {"songs/Songs/A/a song.txt"}
        self.app.toggle_hud()
        self.app.save_cfg()
        root2 = tk.Tk()
        root2.geometry("+10000+10000")
        app2 = hud.HudApp(root2)
        app2.panel.geometry("+10000+10000")
        try:
            root2.update_idletasks()
            self.assertFalse(root2.winfo_ismapped())
            self.assertTrue(app2.panel.winfo_ismapped())
            self.assertEqual(app2.favorites, self.app.favorites)
        finally:
            app2.quit()

    def test_missing_last_song_falls_back_without_autostart(self):
        self.app.cfg["last_song"] = "missing-song.txt"
        self.app.restore_song()
        self.assertIsNotNone(self.app.song)
        self.assertIsNone(self.app.run)
        self.assertIn("недоступна", self.app.lb_status.cget("text"))

    def test_portable_song_path_and_duplicate_names_are_distinct(self):
        first = str(Path(hud.APP_DIR) / "songs/Songs/A/same.txt")
        second = str(Path(hud.APP_DIR) / "songs/Multi-Sheet Songs/A/same.txt")
        self.assertEqual(hud.song_path_key(first), "songs/Songs/A/same.txt")
        self.app.toggle_favorite(first)
        self.assertNotIn(hud.song_path_key(second), self.app.favorites)
        self.assertTrue(os.path.isabs(hud.song_path_key(str(self.cfg))))

    def test_preview_starts_with_first_event_even_for_close_notes(self):
        song = hud.parse_song('{"songNotes":[{"time":0,"key":"1Key0"},{"time":10,"key":"1Key1"}]}')
        self.app.set_song(song)
        self.app._build_items()
        self.app._animate_items()
        self.assertEqual(self.app.cv.itemcget(self.app._it["body"][0][0], "fill"), self.app.HUD_COLORS["now"])
        self.assertEqual(self.app.cv.itemcget(self.app._it["body"][0][1], "fill"), self.app.HUD_COLORS["key"])

    def test_song_map_lists_all_fragments_and_navigation_is_manual(self):
        song = hud.parse_song('{"name":"Карта","songNotes":['
                              '{"time":0,"key":"1Key0"},'
                              '{"time":250,"key":"1Key3"},'
                              '{"time":500,"key":"1Key7"}]}')
        self.app.set_song(song)
        win = self.app.open_map()
        try:
            win.geometry("620x520+10000+10000")
            self.root.update()
            rows = list(range(win.map_grid.step_count))
            self.assertEqual(len(rows), 3)
            self.assertIsNone(self.app.run)
            win.select_step(rows[1])
            self.root.update()
            self.assertEqual(self.app.preview_index, 1)
            self.assertIsNone(self.app.run)
            win.next_button.invoke()
            self.assertEqual(self.app.preview_index, 2)
            win.previous_button.invoke()
            self.assertEqual(self.app.preview_index, 1)
        finally:
            win.destroy()

    def test_song_map_selects_practice_range_without_autostart(self):
        song = hud.parse_song('{"name":"Отрезок","songNotes":['
                              '{"time":0,"key":"1Key0"},'
                              '{"time":250,"key":"1Key1"},'
                              '{"time":500,"key":"1Key2"},'
                              '{"time":750,"key":"1Key3"}]}')
        self.app.set_song(song)
        win = self.app.open_map()
        try:
            win.range_from.set("2")
            win.range_to.set("3")
            win.practice_button.invoke()
            self.assertEqual(self.app.practice_range, (1, 2))
            self.assertEqual(self.app.preview_index, 1)
            self.assertIsNone(self.app.run)
            self.app.start()
            self.assertEqual(len(self.app.run["tl"]["evs"]), 2)
            self.assertEqual(self.app.run["tl"]["evs"][0]["t"], 0)
        finally:
            win.destroy()

    def test_selected_song_favorite_button_adds_and_removes_favorite(self):
        self.app.load_demo()
        key = self.app.song_path
        self.assertEqual(self.app.favorite_button.cget("text"), "☆")
        self.app.favorite_button.invoke()
        self.assertEqual(self.app.favorite_button.cget("text"), "★")
        self.assertIn(key, self.app.favorites)
        self.app.save_cfg()
        self.app.favorite_button.invoke()
        self.assertEqual(self.app.favorite_button.cget("text"), "☆")
        self.assertNotIn(key, self.app.favorites)

    def test_catalog_and_main_favorite_button_share_one_state(self):
        target = self.app.catalog_entries("")[0][1]
        stem = os.path.splitext(os.path.basename(target))[0]
        self.app.load_song_file(target)
        win = self.app.open_catalog()
        try:
            self.root.update()
            win.catalog_next.invoke()
            self.root.update()
            row = next(i for i, entry in enumerate(win.catalog_arr) if entry[1] == target)
            iid = win.catalog_tree.get_children()[row]
            x, y, width, height = win.catalog_tree.bbox(iid, "favorite")
            win.catalog_tree.event_generate("<Button-1>", x=x + width // 2, y=y + height // 2)
            self.root.update()
            self.assertIn(hud.song_path_key(target), self.app.favorites)
            self.assertEqual(self.app.favorite_button.cget("text"), "★")
            win.catalog_fav.invoke()
            self.root.update()
            values = win.catalog_tree.item(win.catalog_tree.get_children()[0], "values")
            self.assertEqual(values[0], "★")
            self.app.favorite_button.invoke()
            self.root.update()
            self.assertNotIn(hud.song_path_key(target), self.app.favorites)
            win.catalog_next.invoke()
            self.root.update()
            values = next(win.catalog_tree.item(item, "values") for item in win.catalog_tree.get_children()
                          if win.catalog_tree.item(item, "values")[1] == stem)
            self.assertEqual(values[0], "☆")
        finally:
            win.destroy()

    def test_parser_ignores_invalid_time_without_losing_valid_notes(self):
        song = hud.parse_song('{"songNotes":['
                              '{"time":0,"key":"1 Key 3"},'
                              '{"time":"NaN","key":"1Key4"},'
                              '{"time":250,"key":"Key 5"}]}')
        self.assertEqual(song["parts"][0]["events"], [(0.0, [3]), (250.0, [5])])

    def test_import_rejects_unsupported_extension_without_creating_directory(self):
        unsupported = Path(self.tmp.name) / "song.csv"
        unsupported.write_text("{}", encoding="utf-8")
        custom = Path(self.tmp.name) / "User Songs"
        with patch.object(hud.HudApp, "CUSTOM_SONG_DIR", str(custom)):
            with self.assertRaises(ValueError):
                self.app.import_song_file(str(unsupported))
        self.assertFalse(custom.exists())

    def test_map_practice_button_switches_to_manual_step_mode(self):
        song = hud.parse_song('{"songNotes":[{"time":0,"key":"1Key0"},{"time":250,"key":"1Key1"}]}')
        self.app.set_song(song)
        win = self.app.open_map()
        try:
            win.range_from.set("1")
            win.range_to.set("2")
            win.practice_button.invoke()
            self.assertEqual(self.app.mode, "step")
            self.assertEqual(self.app.practice_range, (0, 1))
            self.assertIn("ручная практика", self.app.lb_status.cget("text"))
        finally:
            win.destroy()

    def test_saved_playback_preferences_restore(self):
        self.app.mode = "step"
        self.app.metro_on = True
        self.app.sound_on = True
        self.app.loop_on = True
        self.app.save_cfg()
        root2 = tk.Tk()
        root2.geometry("+10000+10000")
        app2 = hud.HudApp(root2)
        try:
            self.assertEqual(app2.mode, "step")
            self.assertTrue(app2.metro_on)
            self.assertTrue(app2.sound_on)
            self.assertTrue(app2.loop_on)
        finally:
            app2.quit()

    def test_saved_hud_position_survives_hide_show(self):
        self.app._build_items()
        self.root.update()
        original = self.app.geom()[:2]
        self.app.toggle_hud()
        self.app.save_cfg()
        self.app.toggle_hud()
        self.root.update()
        restored = self.app.geom()[:2]
        self.assertEqual(restored, original)

    def test_song_map_stops_active_run_when_navigating_preview(self):
        song = hud.parse_song('{"name":"Активная карта","songNotes":['
                              '{"time":0,"key":"1Key0"},'
                              '{"time":250,"key":"1Key1"},'
                              '{"time":500,"key":"1Key2"}]}')
        self.app.set_song(song)
        self.app.mode = "step"
        self.app.start()
        self.assertIsNotNone(self.app.run)
        win = self.app.open_map()
        try:
            rows = list(range(win.map_grid.step_count))
            win.select_step(rows[1])
            self.root.update()
            self.assertIsNone(self.app.run)
            self.assertEqual(self.app.preview_index, 1)
        finally:
            win.destroy()

    def test_changing_selected_part_clears_practice_range(self):
        song = hud.parse_song('{"name":"Партии","songNotes":['
                              '{"time":0,"key":"1Key0"},'
                              '{"time":250,"key":"2Key1"},'
                              '{"time":500,"key":"1Key2"}]}')
        self.app.set_song(song)
        win = self.app.open_map()
        try:
            win.range_from.set("1")
            win.range_to.set("2")
            win.practice_button.invoke()
            self.assertEqual(self.app.practice_range, (0, 1))
            var = self.app.part_vars["2"]
            var.set(False)
            self.app._toggle_part("2", var)
            self.assertIsNone(self.app.practice_range)
        finally:
            win.destroy()

    def test_saved_control_panel_position_is_restored(self):
        self.app.cfg["px"], self.app.cfg["py"] = 3273, 56
        with patch.object(self.app, "_desktop_bounds", return_value=(0, 0, 3840, 1080)):
            self.app.place_panel()
        self.root.update_idletasks()
        self.assertEqual(self.app.panel.winfo_x(), 3273)
        self.assertEqual(self.app.panel.winfo_y(), 56)

    def test_hud_uses_plain_rectangles_without_decorative_rings(self):
        self.app._build_items()
        shapes = [self.app.cv.type(i) for i in self.app.cv.find_all()]
        self.assertNotIn("oval", shapes)
        self.assertNotIn("polygon", shapes)
        self.assertEqual(shapes.count("rectangle"), 48)

    def test_key_labels_are_large_bold_and_high_contrast(self):
        import tkinter.font as tkfont
        self.app.scale = 1.0
        self.app._build_items()
        self.app._animate_items()
        item = self.app._it["labels"][0][0]
        font = tkfont.Font(root=self.root, font=self.app.cv.itemcget(item, "font"))
        self.assertGreaterEqual(font.actual("size"), 16)
        self.assertEqual(font.actual("weight"), "bold")
        def luminance(color):
            rgb = [int(color[i:i+2], 16)/255 for i in (1, 3, 5)]
            rgb = [v/12.92 if v <= .04045 else ((v+.055)/1.055)**2.4 for v in rgb]
            return sum(v*w for v, w in zip(rgb, (.2126, .7152, .0722)))
        C = self.app.HUD_COLORS
        for background, foreground in ((C["key"], C["text"]), (C["now"], C["ink"]), (C["next"], C["ink"])):
            lo, hi = sorted((luminance(background), luminance(foreground)))
            self.assertGreaterEqual((hi+.05)/(lo+.05), 7.0)

    def test_first_panel_outlines_next_chord_without_replacing_current_fill(self):
        song = hud.parse_song('{"songNotes":['
            '{"time":0,"key":"1Key0"},{"time":0,"key":"1Key4"},'
            '{"time":250,"key":"1Key1"},{"time":250,"key":"1Key4"},'
            '{"time":500,"key":"1Key2"}]}')
        self.app.set_song(song)
        self.app._build_items()
        self.app._animate_items()
        C = self.app.HUD_COLORS
        def option(panel, key, name):
            return self.app.cv.itemcget(self.app._it["body"][panel][key], name)
        self.assertEqual(option(0, 1, "outline"), C["next"])
        self.assertGreaterEqual(float(option(0, 1, "width")), 2)
        self.assertEqual(option(0, 1, "fill"), C["key"])
        self.assertEqual(option(0, 0, "fill"), C["now"])
        # A key present in BOTH chords keeps the current fill and the next outline.
        self.assertEqual(option(0, 4, "fill"), C["now"])
        self.assertEqual(option(0, 4, "outline"), C["next"])
        self.assertNotEqual(option(2, 1, "outline"), C["next"])
        self.app.mode = "step"
        self.app.start()
        self.app._judge_step(0)
        self.app._judge_step(4)
        self.app._animate_items()
        self.assertEqual(option(0, 2, "outline"), C["next"])
        self.assertNotEqual(option(0, 1, "outline"), C["next"])
        self.assertEqual(float(option(0, 1, "width")), 1)
        self.app._judge_step(1)
        self.app._judge_step(4)
        self.app._animate_items()
        self.assertNotEqual(option(0, 2, "outline"), C["next"])
        self.assertEqual(float(option(0, 2, "width")), 1)

    def test_upload_button_imports_song_into_persistent_catalog(self):
        source = Path(self.tmp.name) / "Моя песня.txt"
        content = '{"name":"Моя песня","songNotes":[{"time":0,"key":"1Key3"}]}'
        source.write_text(content, encoding="utf-8-sig")
        custom = Path(self.tmp.name) / "User Songs"
        with patch.object(hud.HudApp, "CUSTOM_SONG_DIR", str(custom), create=True):
            win = self.app.open_catalog()
            win.geometry("620x700+10000+10000")
            with patch.object(hud.filedialog, "askopenfilename", return_value=str(source)) as dialog:
                win.import_button.invoke()
            self.assertEqual(dialog.call_args.kwargs["parent"], win)
            imported = custom / source.name
            self.assertTrue(imported.exists())
            self.assertEqual(imported.read_bytes(), source.read_bytes())
            self.assertTrue(any(e[1] == str(imported) for e in win.catalog_arr))
            self.assertEqual(self.app.song["name"], "Моя песня")
            self.assertIsNone(self.app.run)
            self.app._catalog_cache = None
            self.assertTrue(any(e[1] == str(imported) for e in self.app.catalog_entries("Моя")))
            win.destroy()

    def test_import_keeps_same_name_songs_and_reuses_identical_copy(self):
        first = Path(self.tmp.name) / "first" / "song.txt"
        second = Path(self.tmp.name) / "second" / "song.txt"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_text('{"name":"one","songNotes":[{"time":0,"key":"1Key0"}]}', encoding="utf-8")
        second.write_text('{"name":"two","songNotes":[{"time":0,"key":"1Key1"}]}', encoding="utf-8")
        custom = Path(self.tmp.name) / "User Songs"
        with patch.object(hud.HudApp, "CUSTOM_SONG_DIR", str(custom)):
            one = self.app.import_song_file(str(first))
            two = self.app.import_song_file(str(second))
            self.assertNotEqual(one, two)
            self.assertEqual(Path(one).read_bytes(), first.read_bytes())
            self.assertEqual(Path(two).read_bytes(), second.read_bytes())
            self.assertEqual(self.app.import_song_file(str(first)), one)
            self.assertEqual(self.app.import_song_file(one), one)
            self.assertEqual(len(list(custom.iterdir())), 2)

    def test_upload_cancel_or_invalid_file_leaves_song_unchanged(self):
        custom = Path(self.tmp.name) / "User Songs"
        invalid = Path(self.tmp.name) / "bad.txt"
        invalid.write_text("not a song", encoding="utf-8")
        original = self.app.song
        with patch.object(hud.HudApp, "CUSTOM_SONG_DIR", str(custom)):
            win = self.app.open_catalog()
            win.geometry("620x700+10000+10000")
            with patch.object(hud.filedialog, "askopenfilename", return_value=""):
                win.import_button.invoke()
            self.assertIs(self.app.song, original)
            with patch.object(hud.filedialog, "askopenfilename", return_value=str(invalid)):
                win.import_button.invoke()
            self.assertIs(self.app.song, original)
            self.assertFalse(custom.exists())
            self.assertIn("Ошибка", win.import_status.cget("text"))
            win.destroy()

    def test_offscreen_saved_positions_are_rescued_onto_the_desktop(self):
        self.app.cfg["px"], self.app.cfg["py"] = -30463, -31076
        self.app.place_panel()
        self.root.update_idletasks()
        vx, vy, vw, vh = self.app._desktop_bounds()
        px, py = self.app.panel.winfo_x(), self.app.panel.winfo_y()
        self.assertTrue(vx <= px < vx + vw - 40, (px, vw))
        self.assertTrue(vy <= py < vy + vh - 40, (py, vh))

    def test_saving_never_persists_hidden_window_coordinates(self):
        import json
        self.app.root.geometry("-30463-31076")
        self.app.panel.geometry("-30463-31076")
        self.root.update_idletasks()
        self.app.save_cfg()
        saved = json.loads(self.cfg.read_text(encoding="utf-8"))
        vx, vy, vw, vh = self.app._desktop_bounds()
        for key, lo, hi in (("x", vx, vx + vw), ("px", vx, vx + vw),
                            ("y", vy, vy + vh), ("py", vy, vy + vh)):
            self.assertTrue(lo <= saved[key] < hi, (key, saved[key]))
        self.assertGreaterEqual(saved["w"], 50)

    def test_invalid_saved_hud_position_is_rescued(self):
        self.app.root.geometry("-30463-31076")
        self.app._move_cfg(-30463, -31076)
        self.root.update_idletasks()
        vx, vy, vw, vh = self.app._desktop_bounds()
        x, y, _w, _h = self.app.geom()
        self.assertTrue(vx <= x < vx + vw - 40, (x, vw))
        self.assertTrue(vy <= y < vy + vh - 40, (y, vh))

    def test_start_uses_selected_parts(self):
        song = hud.parse_song('{"songNotes":['
                              '{"time":0,"key":"1Key0"},'
                              '{"time":100,"key":"2Key1"}]}')
        self.app.set_song(song)
        self.app.part_sel = {"1"}
        self.app.start()
        self.assertEqual([n["part"] for n in self.app.run["tl"]["instances"]], ["1"])

    def test_train_mode_uses_timed_judge(self):
        from unittest.mock import patch
        song = hud.parse_song('{"songNotes":[{"time":0,"key":"1Key0"}]}')
        self.app.set_song(song)
        self.app.mode = "train"
        self.app.start()
        with patch.object(self.app, "_judge") as judge:
            self.app.song_time_ms = lambda: 0.0
            self.app._key_down(0)
        judge.assert_called_once_with(0, 0.0)

    def test_loop_restarts_step_run_from_first_fragment(self):
        song = hud.parse_song('{"songNotes":[{"time":0,"key":"1Key0"}]}')
        self.app.set_song(song)
        self.app.mode = "step"
        self.app.loop_on = True
        self.app.start()
        old_run = self.app.run
        self.app._judge_step(0)
        self.assertIsNotNone(self.app.run)
        self.assertIsNot(self.app.run, old_run)
        self.assertEqual(self.app.run["step_idx"], 0)

    def test_loop_restarts_selected_manual_practice_range_from_first_fragment(self):
        song = hud.parse_song('{"songNotes":['
                              '{"time":0,"key":"1Key0"},'
                              '{"time":250,"key":"1Key1"},'
                              '{"time":500,"key":"1Key2"},'
                              '{"time":750,"key":"1Key3"}]}')
        self.app.set_song(song)
        self.app.mode = "step"
        self.app.practice_range = (1, 2)
        self.app.loop_on = True
        self.app.start()
        first_run = self.app.run
        self.assertEqual([event["t"] for event in first_run["tl"]["evs"]], [0, 250])
        self.app._judge_step(1)
        self.app._judge_step(2)
        self.assertIsNotNone(self.app.run)
        self.assertIsNot(self.app.run, first_run)
        self.assertEqual(self.app.run["step_idx"], 0)
        self.assertEqual([event["t"] for event in self.app.run["tl"]["evs"]], [0, 250])

    def test_loop_restarts_timed_run_from_first_event(self):
        song = hud.parse_song('{"songNotes":['
                              '{"time":0,"key":"1Key0"},'
                              '{"time":250,"key":"1Key1"}]}')
        self.app.set_song(song)
        self.app.mode = "train"
        self.app.loop_on = True
        self.app.start()
        self.app.song_time_ms = lambda: self.app.run["tl"]["end_t"] + 601
        old_run = self.app.run
        self.app._tick_inner()
        self.assertIsNotNone(self.app.run)
        self.assertIsNot(self.app.run, old_run)
        self.assertEqual(self.app.run["ev_idx"], 0)

    def test_loop_can_be_disabled_after_a_running_manual_practice_cycle(self):
        song = hud.parse_song('{"songNotes":[{"time":0,"key":"1Key0"}]}')
        self.app.set_song(song)
        self.app.mode = "step"
        self.app.practice_range = (0, 0)
        self.app.loop_on = True
        self.app.start()
        self.app._judge_step(0)
        self.assertIsNotNone(self.app.run)
        self.app.loop_on = False
        self.app._judge_step(0)
        self.assertIsNone(self.app.run)

    def test_loop_checkbox_is_explicitly_labeled_as_cycle(self):
        self.assertEqual(self.app.cb_loop.cget("text"), "Повтор (цикл)")

    def test_timeline_deduplicates_same_physical_key_across_parts(self):
        song = hud.parse_song('{"songNotes":['
                              '{"time":0,"key":"1Key0"},'
                              '{"time":0,"key":"2Key0"}]}')
        tl = hud.build_timeline(song, ["1", "2"])
        self.assertEqual(len(tl["evs"]), 1)
        self.assertEqual([n["key"] for n in tl["evs"][0]["notes"]], [0])
        self.assertEqual(len(tl["instances"]), 1)

    def test_huge_note_times_are_ignored_and_step_start_is_instant(self):
        path = Path(self.tmp.name) / "huge.txt"
        path.write_text('{"songNotes":[{"time":0,"key":"1Key0"},{"time":100,"key":"1Key1"},'
                        '{"time":1e308,"key":"1Key2"}]}', encoding="utf-8")
        self.app.load_song_file(str(path))
        self.assertLess(self.app.song["dur_ms"], hud.MAX_SONG_MS)
        self.app.mode = "step"
        started = time.perf_counter()
        self.app.start()
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertIsNotNone(self.app.run)
        self.assertNotIn("grid", self.app.run)
        self.app.stop()

    def test_broken_config_types_fall_back_to_safe_defaults(self):
        cfg = Path(self.tmp.name) / "bad-config.json"
        cases = (("[]", (1.0, 0.85, "step")),
                 ('{"scale":"bad","alpha":9,"mode":"train","parts":"x"}', (1.0, 1.0, "step")))
        for payload, expected in cases:
            cfg.write_text(payload, encoding="utf-8")
            with patch.object(hud.HudApp, "CFG_FILE", str(cfg)):
                root = tk.Tk()
                root.geometry("+10000+10000")
                app = hud.HudApp(root)
                try:
                    self.assertEqual((app.scale, app.alpha, app.mode), expected)
                finally:
                    app.quit()

    def test_broken_key_number_does_not_discard_valid_zero_based_notes(self):
        song = hud.parse_song('{"songNotes":[{"time":0,"key":"1Key0"},{"time":10,"key":"1Key99"}]}')
        self.assertEqual(song["parts"][0]["events"], [(0.0, [0])])

    def test_empty_song_list_is_a_format_error(self):
        with self.assertRaises(ValueError):
            hud.parse_song("[]")

    def test_non_numeric_bpm_is_normalised(self):
        song = hud.parse_song('{"bpm":"fast","songNotes":[{"time":0,"key":"1Key0"}]}')
        self.assertEqual(song["bpm"], 0.0)
        self.assertEqual(hud.parse_song('{"bpm":"120","songNotes":[{"time":0,"key":"1Key0"}]}')["bpm"], 120.0)
        path = Path(self.tmp.name) / "bpm.txt"
        path.write_text('{"name":"BPM","bpm":"fast","songNotes":[{"time":0,"key":"1Key0"}]}',
                        encoding="utf-8")
        self.app.load_song_file(str(path))
        self.assertEqual(self.app.song["name"], "BPM")

    def test_paste_replaces_the_selected_query(self):
        win = self.app.open_catalog()
        try:
            self.root.update()
            win.catalog_query.set("abc")
            self.root.update()
            win.catalog_entry.selection_range(0, "end")
            self.root.clipboard_clear()
            self.root.clipboard_append("xyz")
            self.root.update()
            win.catalog_paste()
            self.root.update()
            self.assertEqual(win.catalog_query.get(), "xyz")
        finally:
            win.destroy()

    def test_favourite_pages_are_capped_and_empty_catalog_has_one_page(self):
        entries = [("f%03d.txt" % i, "E:/songs/f%03d.txt" % i, 1, "f%03d.txt" % i,
                    "e:/songs/f%03d.txt" % i) for i in range(340)]
        self.app._catalog_cache = entries
        self.app.favorites = {entry[4] for entry in entries}
        self.app._catalog_split_cache = None
        rows, page, pages, mode, total, fav = self.app.catalog_page("", 0)
        self.assertEqual((len(rows), page, pages, mode, fav), (300, 0, 2, "favorites", 340))
        rows, page, pages, mode, total, fav = self.app.catalog_page("", 1)
        self.assertEqual((len(rows), page, mode), (40, 1, "favorites"))
        self.app._catalog_cache = []
        self.app.favorites = set()
        self.app._catalog_split_cache = None
        rows, page, pages, mode, total, fav = self.app.catalog_page("", 0)
        self.assertEqual((rows, pages, mode), ([], 1, "favorites"))
        self.app._catalog_cache = None
        self.app._catalog_split_cache = None

    def test_catalog_destroy_unregisters_refresh(self):
        win = self.app.open_catalog()
        self.root.update()
        self.assertTrue(self.app._catalog_refreshers)
        win.destroy()
        self.root.update()
        self.assertFalse(self.app._catalog_refreshers)

    def test_map_click_does_not_overwrite_manual_practice_range(self):
        notes = ",".join('{"time":%d,"key":"1Key0"}' % (i * 100) for i in range(25))
        path = Path(self.tmp.name) / "long.txt"
        path.write_text('{"songNotes":[%s]}' % notes, encoding="utf-8")
        self.app.load_song_file(str(path))
        win = self.app.open_map()
        win.geometry("+10000+10000")
        try:
            self.root.update()
            win.range_from.set("4")
            win.range_to.set("9")
            win.select_step(15)
            self.root.update()
            self.assertEqual((win.range_from.get(), win.range_to.get()), ("4", "9"))
        finally:
            win.destroy()
            self.root.update()

    def test_open_map_reuses_one_window(self):
        self.app.load_demo()
        win = self.app.open_map()
        win.geometry("+10000+10000")
        try:
            self.assertIs(self.app.open_map(), win)
        finally:
            win.destroy()
            self.root.update()


if __name__ == "__main__":
    unittest.main()
