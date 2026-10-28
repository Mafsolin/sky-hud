"""Regression tests for live map synchronization and row clicks."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import tkinter as tk

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sky_hud as hud


class LiveMapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("SKY_HUD_TEST_TMPDIR"))
        self.cfg = Path(self.tmp.name) / "config.json"
        self.override = patch.object(hud.HudApp, "CFG_FILE", str(self.cfg))
        self.override.start()
        self.original_app_dir = hud.APP_DIR
        fixture = Path(self.tmp.name) / "app"
        (fixture / "songs" / "Songs").mkdir(parents=True)
        (fixture / "songs" / "Multi-Sheet Songs").mkdir(parents=True)
        (fixture / "songs" / "User Songs").mkdir(parents=True)
        hud.APP_DIR = str(fixture)
        self.custom = patch.object(hud.HudApp, "CUSTOM_SONG_DIR", str(fixture / "songs/User Songs"))
        self.custom.start()
        self.root = tk.Tk()
        self.root.geometry("+10000+10000")
        self.app = hud.HudApp(self.root)

    def tearDown(self):
        self.app.quit()
        self.custom.stop()
        self.override.stop()
        hud.APP_DIR = self.original_app_dir
        self.tmp.cleanup()

    @staticmethod
    def song(name, keys):
        notes = []
        for i, key in enumerate(keys):
            notes.append({"time": i * 250, "key": "1Key%d" % key})
        return hud.parse_song(json.dumps({"name": name, "songNotes": notes}))

    def test_clicking_currently_displayed_row_stops_run_even_when_index_is_same(self):
        self.app.set_song(self.song("click", [0, 1, 2]))
        self.app.mode = "step"
        win = self.app.open_map()
        try:
            self.app.start()
            self.app._judge_step(0)
            self.assertEqual(self.app.run["step_idx"], 1)
            self.assertEqual(self.app.preview_index, 0)
            row = win.map_tree.get_children()[0]
            win.map_tree.selection_set(row)
            win.map_tree.focus(row)
            win.map_tree.event_generate("<<TreeviewSelect>>")
            self.root.update()
            self.assertIsNone(self.app.run)
            self.assertEqual(self.app.preview_index, 0)
        finally:
            win.destroy()

    def test_closing_map_removes_live_refresh_callback(self):
        self.app.set_song(self.song("cleanup", [0, 1]))
        win = self.app.open_map()
        self.assertEqual(len(self.app._map_refreshers), 1)
        win.destroy()
        self.root.update()
        self.assertEqual(self.app._map_refreshers, [])

    def test_open_map_refreshes_rows_after_song_replacement(self):
        self.app.set_song(self.song("first", [0, 1, 2]))
        win = self.app.open_map()
        try:
            self.assertEqual(len(win.map_tree.get_children()), 3)
            self.app.set_song(self.song("second", [10, 11]))
            self.root.update()
            rows = win.map_tree.get_children()
            self.assertEqual(len(rows), 2)
            self.assertEqual(win.map_tree.item(rows[0], "values")[2], "N")
            self.assertEqual(win.range_to.get(), "1")
        finally:
            win.destroy()

    def test_open_map_refreshes_rows_after_part_selection_changes(self):
        song = hud.parse_song(json.dumps({"name": "parts", "songNotes": [
            {"time": 0, "key": "1Key0"}, {"time": 250, "key": "2Key1"},
            {"time": 500, "key": "1Key2"}]}))
        self.app.set_song(song)
        win = self.app.open_map()
        try:
            self.assertEqual(len(win.map_tree.get_children()), 3)
            var = self.app.part_vars["2"]
            var.set(False)
            self.app._toggle_part("2", var)
            self.root.update()
            rows = win.map_tree.get_children()
            self.assertEqual(len(rows), 2)
            self.assertEqual(win.map_tree.item(rows[1], "values")[2], "I")
        finally:
            win.destroy()


if __name__ == "__main__":
    unittest.main()
