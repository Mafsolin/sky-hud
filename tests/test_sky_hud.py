"""Regression tests; real Tk widgets use an off-screen scratch window only."""
import gc
import json
import os
import shutil
import sys
import tempfile
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
        row = next(i for i, entry in enumerate(win.catalog_arr) if entry[1] == target)
        iid = tree.get_children()[row]
        self.root.update()
        tree.see(iid)
        self.root.update()
        x, y, width, height = tree.bbox(iid, "favorite")
        tree.event_generate("<Button-1>", x=x+width//2, y=y+height//2)
        self.assertEqual(win.catalog_arr[0][1], target)
        self.assertEqual(tree.item(tree.get_children()[0], "values")[0], "★")
        self.app.save_cfg()
        self.assertIn(hud.song_path_key(target), self.app.load_cfg()["favorites"])
        self.assertEqual(self.app.catalog_entries(os.path.basename(target)[:12])[0][1], target)
        tree.event_generate("<Button-1>", x=20, y=tree.bbox(tree.get_children()[0])[1]+8)
        self.assertNotIn(hud.song_path_key(target), self.app.favorites)
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
        self.app.load_song_file(target)
        win = self.app.open_catalog()
        try:
            self.root.update()
            row = next(i for i, entry in enumerate(win.catalog_arr) if entry[1] == target)
            iid = win.catalog_tree.get_children()[row]
            x, y, width, height = win.catalog_tree.bbox(iid, "favorite")
            win.catalog_tree.event_generate("<Button-1>", x=x + width // 2, y=y + height // 2)
            self.assertIn(hud.song_path_key(target), self.app.favorites)
            self.assertEqual(self.app.favorite_button.cget("text"), "★")
            values = win.catalog_tree.item(win.catalog_tree.get_children()[row], "values")
            self.assertEqual(values[0], "★")
            self.app.favorite_button.invoke()
            self.assertNotIn(hud.song_path_key(target), self.app.favorites)
            values = next(win.catalog_tree.item(item, "values") for item in win.catalog_tree.get_children()
                          if win.catalog_tree.item(item, "values")[1] == os.path.splitext(os.path.basename(target))[0])
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


if __name__ == "__main__":
    unittest.main()
