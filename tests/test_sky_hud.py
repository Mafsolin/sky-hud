"""Regression tests; real Tk widgets use an off-screen scratch window only."""
import os
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
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("SKY_HUD_TEST_TMPDIR"))
        self.cfg = Path(self.tmp.name) / "config.json"
        self.override = patch.object(hud.HudApp, "CFG_FILE", str(self.cfg))
        self.override.start()
        self.root = tk.Tk()
        self.root.geometry("+10000+10000")
        self.app = hud.HudApp(self.root)
        self.app.panel.geometry("+10000+10000")
        self.root.update_idletasks()

    def tearDown(self):
        self.app.sound.q.put(None)
        self.root.destroy()
        self.override.stop()
        self.tmp.cleanup()

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
            app2.sound.q.put(None)
            root2.destroy()

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
            app2.sound.q.put(None)
            root2.destroy()

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

    def test_saved_control_panel_position_is_restored(self):
        self.app.cfg["px"], self.app.cfg["py"] = 3273, 56
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


if __name__ == "__main__":
    unittest.main()
