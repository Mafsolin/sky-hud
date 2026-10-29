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

    def test_map_steps_are_rectangular_five_by_three_grids(self):
        song = hud.parse_song(json.dumps({"songNotes": [
            {"time": 0, "key": "1Key0"}, {"time": 0, "key": "1Key7"},
            {"time": 0, "key": "1Key14"}, {"time": 250, "key": "1Key3"}]}))
        self.app.set_song(song)
        win = self.app.open_map()
        win.geometry("760x650+10000+10000")
        try:
            self.root.update()
            grid = win.map_grid
            self.assertEqual(grid.step_count, 2)
            card = grid.rendered_cards[0]
            self.assertEqual(len(card["keys"]), 15)
            self.assertEqual([grid.itemcget(i, "text") for i in card["labels"]], hud.CODES)
            positions = [grid.coords(i) for i in card["keys"]]
            self.assertEqual(len({p[0] for p in positions}), 5)
            self.assertEqual(len({p[1] for p in positions}), 3)
            for key, item in enumerate(card["keys"]):
                self.assertEqual(grid.type(item), "rectangle")
                self.assertEqual(grid.itemcget(item, "fill"),
                    hud.HudApp.HUD_COLORS["now"] if key in (0, 7, 14) else hud.HudApp.HUD_COLORS["key"])
            self.assertFalse(any(isinstance(w, hud.ttk.Treeview) for w in win.winfo_children()))
        finally:
            win.destroy()

    def test_long_map_renders_only_visible_grids_and_can_reach_last_step(self):
        self.app.set_song(self.song("long", [i % 15 for i in range(5000)]))
        win = self.app.open_map()
        win.geometry("760x650+10000+10000")
        try:
            self.root.update()
            grid = win.map_grid
            self.assertEqual(grid.step_count, 5000)
            self.assertLess(len(grid.rendered_cards), 100)
            win.select_step(4999)
            self.root.update()
            self.assertIn(4999, grid.rendered_cards)
            self.assertEqual(self.app.preview_index, 4999)
            self.assertIsNone(self.app.run)
            self.assertLess(len(grid.rendered_cards), 100)
        finally:
            win.destroy()

    def test_clicking_card_selects_step_and_stops_run(self):
        self.app.set_song(self.song("click cards", [0, 7, 14]))
        self.app.mode = "step"
        win = self.app.open_map()
        win.geometry("760x650+10000+10000")
        try:
            self.root.update()
            grid = win.map_grid
            for step in (1, 1, 0):
                self.app.start()
                self.assertIsNotNone(self.app.run)
                x1, y1, x2, y2 = grid.coords(grid.rendered_cards[step]["body"])
                grid.event_generate("<Button-1>", x=int(x1 + 20), y=int(y1 + 20 - grid.canvasy(0)))
                self.root.update()
                self.assertEqual(self.app.preview_index, step)
                self.assertEqual(grid.selected_index, step)
                self.assertIsNone(self.app.run)
            win.range_from.set("1")
            win.range_to.set("2")
            win.practice_button.invoke()
            self.assertEqual(grid.practice_range, (0, 1))
        finally:
            win.destroy()

    def test_first_step_is_fully_visible_when_map_opens_and_resizes(self):
        self.app.set_song(self.song("viewport", [i % 15 for i in range(100)]))
        win = self.app.open_map()
        win.geometry("760x650+10000+10000")
        try:
            self.root.update()
            grid = win.map_grid
            first_top = grid.coords(grid.rendered_cards[0]["body"])[1]
            self.assertLessEqual(grid.canvasy(0), first_top)
            win.select_step(80)
            win.geometry("1000x650+10000+10000")
            self.root.update()
            self.assertIn(80, grid.rendered_cards)
        finally:
            win.destroy()

    def test_start_and_end_buttons_pick_range_by_clicking_cards(self):
        self.app.set_song(self.song("range pick", [0, 1, 2, 3, 4, 5]))
        self.app.mode = "step"
        win = self.app.open_map()
        win.geometry("900x680+10000+10000")
        try:
            self.root.update()
            grid = win.map_grid
            win.start_button.invoke()
            self.assertEqual(win.pick_mode, "start")
            self.click_card(grid, 2)
            self.assertIsNone(win.pick_mode)
            self.assertEqual((win.range_from.get(), win.range_to.get()), ("3", "3"))
            self.assertEqual(grid.practice_range, (2, 2))
            win.end_button.invoke()
            self.assertEqual(win.pick_mode, "end")
            self.click_card(grid, 5)
            self.assertIsNone(win.pick_mode)
            self.assertEqual((win.range_from.get(), win.range_to.get()), ("3", "6"))
            self.assertEqual(grid.practice_range, (2, 5))
            win.practice_button.invoke()
            self.assertEqual(self.app.practice_range, (2, 5))
            self.assertEqual(self.app.mode, "step")
            self.app.start()
            self.assertEqual([event["notes"][0]["key"] for event in self.app.run["tl"]["evs"]],
                             [2, 3, 4, 5])
        finally:
            win.destroy()

    def test_end_before_start_is_rejected_and_end_pick_stays_active(self):
        self.app.set_song(self.song("bad range", [0, 1, 2, 3]))
        win = self.app.open_map()
        win.geometry("900x680+10000+10000")
        try:
            self.root.update()
            grid = win.map_grid
            win.start_button.invoke()
            self.click_card(grid, 2)
            win.end_button.invoke()
            self.click_card(grid, 0)
            self.assertEqual(win.pick_mode, "end")
            self.assertEqual(grid.practice_range, (2, 2))
            self.assertIn("раньше", self.app.lb_status.cget("text"))
            self.click_card(grid, 3)
            self.assertEqual(grid.practice_range, (2, 3))
        finally:
            win.destroy()

    def test_clicking_card_without_pick_mode_does_not_change_practice_range(self):
        self.app.set_song(self.song("plain click", [0, 1, 2, 3]))
        win = self.app.open_map()
        win.geometry("900x680+10000+10000")
        try:
            self.root.update()
            grid = win.map_grid
            win.start_button.invoke()
            self.click_card(grid, 1)
            self.click_card(grid, 3)
            self.assertEqual(grid.practice_range, (1, 1))
            self.assertEqual(self.app.preview_index, 3)
        finally:
            win.destroy()

    @staticmethod
    def click_card(grid, index):
        x1, y1, _x2, _y2 = grid.coords(grid.rendered_cards[index]["body"])
        grid.event_generate("<Button-1>", x=int(x1 + 20), y=int(y1 + 20 - grid.canvasy(0)))

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
            win.select_step(0)
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
            self.assertEqual(win.map_grid.step_count, 3)
            self.app.set_song(self.song("second", [10, 11]))
            self.root.update()
            rows = list(range(win.map_grid.step_count))
            self.assertEqual(len(rows), 2)
            self.assertEqual(win.map_grid.events[0]["notes"][0]["key"], 10)
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
            self.assertEqual(win.map_grid.step_count, 3)
            var = self.app.part_vars["2"]
            var.set(False)
            self.app._toggle_part("2", var)
            self.root.update()
            rows = list(range(win.map_grid.step_count))
            self.assertEqual(len(rows), 2)
            self.assertEqual(win.map_grid.events[1]["notes"][0]["key"], 2)
        finally:
            win.destroy()


if __name__ == "__main__":
    unittest.main()
