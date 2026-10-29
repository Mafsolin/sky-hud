"""Virtualized sheet of rectangular 5-column, 3-row keyboards for Sky HUD."""
import tkinter as tk


class SongMapGrid(tk.Canvas):
    CARD_W = 206
    CARD_H = 142
    GAP = 12
    PAD = 10

    def __init__(self, master, colors, codes, on_select):
        super().__init__(master, bg=colors["panel"], highlightthickness=0, takefocus=True)
        self.colors = colors
        self.codes = codes
        self.on_select = on_select
        self.events = []
        self.selected_index = 0
        self.practice_range = None
        self.columns = 1
        self.rendered_cards = {}
        self.bind("<Configure>", self._resize)
        self.bind("<Button-1>", self._clicked)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Button-4>", lambda _e: self.yview_scroll(-1, "units"))
        self.bind("<Button-5>", lambda _e: self.yview_scroll(1, "units"))
        for key, delta in (("Left", -1), ("Right", 1)):
            self.bind("<%s>" % key, lambda _e, d=delta: self._move(d))
        self.bind("<Up>", lambda _e: self._move(-self.columns))
        self.bind("<Down>", lambda _e: self._move(self.columns))
        self.configure(yscrollincrement=1)

    @property
    def step_count(self):
        return len(self.events)

    def set_events(self, events, selected=0, practice=None):
        self.events = list(events)
        self.selected_index = max(0, min(selected, len(self.events) - 1))
        self.practice_range = practice
        self._resize()
        self.see_step(self.selected_index)

    def _resize(self, _event=None):
        width = max(1, self.winfo_width())
        self.columns = max(1, (width - 2 * self.PAD + self.GAP) // (self.CARD_W + self.GAP))
        count_rows = (len(self.events) + self.columns - 1) // self.columns
        height = 2 * self.PAD + count_rows * (self.CARD_H + self.GAP)
        self.configure(scrollregion=(0, 0, width, max(self.winfo_height(), height)))
        if _event is not None:
            self.see_step(self.selected_index)
        else:
            self._draw()

    def _draw(self):
        self.delete("all")
        self.rendered_cards = {}
        top = self.canvasy(0)
        bottom = top + max(1, self.winfo_height())
        stride = self.CARD_H + self.GAP
        first = max(0, int((top - self.PAD) // stride))
        last = max(first, int((bottom - self.PAD) // stride) + 1)
        c = self.colors
        for index in range(first * self.columns, min(len(self.events), (last + 1) * self.columns)):
            row, column = divmod(index, self.columns)
            x = self.PAD + column * (self.CARD_W + self.GAP)
            y = self.PAD + row * stride
            in_range = self.practice_range is not None and self.practice_range[0] <= index <= self.practice_range[1]
            selected = index == self.selected_index
            outline = c["next"] if selected else (c["now"] if in_range else c["edge"])
            body = self.create_rectangle(x, y, x + self.CARD_W, y + self.CARD_H,
                fill=c["panel"], outline=outline, width=2 if selected or in_range else 1)
            self.create_text(x + 10, y + 16, anchor="w", text="#%d" % (index + 1),
                fill=c["now"] if in_range else c["text"], font=("Segoe UI", 10, "bold"))
            seconds = max(0, int(self.events[index]["t"] // 1000))
            self.create_text(x + self.CARD_W - 10, y + 16, anchor="e", text="%d:%02d" % divmod(seconds, 60),
                fill=c["muted"], font=("Segoe UI", 9))
            active = {note["key"] for note in self.events[index]["notes"]}
            keys, labels = [], []
            for key, letter in enumerate(self.codes):
                key_row, key_col = divmod(key, 5)
                x1, y1 = x + 10 + key_col * 38, y + 36 + key_row * 32
                keys.append(self.create_rectangle(x1, y1, x1 + 34, y1 + 27,
                    fill=c["now"] if key in active else c["key"], outline=c["key_edge"], width=1))
                labels.append(self.create_text(x1 + 17, y1 + 13, text=letter,
                    fill=c["ink"] if key in active else c["text"], font=("Segoe UI", 11, "bold")))
            self.rendered_cards[index] = {"body": body, "keys": keys, "labels": labels}
        if not self.events:
            self.create_text(20, 30, anchor="w", text="В выбранных партиях нет ходов", fill=c["text"])

    def yview(self, *args):
        result = super().yview(*args)
        if args:
            self._draw()
        return result

    def yview_scroll(self, number, what):
        self.yview("scroll", number, what)

    def yview_moveto(self, fraction):
        self.yview("moveto", fraction)

    def see_step(self, index):
        if not self.events:
            self._draw()
            return
        index = max(0, min(index, len(self.events) - 1))
        y = self.PAD + (index // self.columns) * (self.CARD_H + self.GAP)
        top = self.canvasy(0)
        view_height = max(1, self.winfo_height())
        region_height = float(self.cget("scrollregion").split()[3])
        if y < top:
            self.yview_moveto(max(0, y - self.PAD) / region_height)
        elif y + self.CARD_H > top + view_height:
            self.yview_moveto(max(0, y + self.CARD_H + self.PAD - view_height) / region_height)
        self._draw()

    def select_step(self, index, practice=None):
        self.selected_index = index
        self.practice_range = practice
        self.see_step(index)

    def _clicked(self, event):
        x, y = self.canvasx(event.x) - self.PAD, self.canvasy(event.y) - self.PAD
        if x < 0 or y < 0:
            return
        column, dx = divmod(x, self.CARD_W + self.GAP)
        row, dy = divmod(y, self.CARD_H + self.GAP)
        index = int(row) * self.columns + int(column)
        if column < self.columns and dx <= self.CARD_W and dy <= self.CARD_H and index < len(self.events):
            self.focus_set()
            self.on_select(index)

    def _wheel(self, event):
        if event.delta:
            steps = max(1, abs(int(event.delta)) // 120)
            self.yview_scroll((-28 if event.delta > 0 else 28) * steps, "units")
        return "break"

    def _move(self, delta):
        if self.events:
            self.on_select(max(0, min(self.selected_index + delta, len(self.events) - 1)))
        return "break"
