#!/usr/bin/env python3
"""Tests for the footer's version stamp, modeled on roost's paint() tests.

The stamp is a dim v<version> in the true bottom-right corner of the last
visible row. Three things must hold, in roost's words: it rides whatever the
last row is, so it can never itself be the thing that gets clipped; it stops
before the final column, because addstr into the last cell wraps onto the next
row and the stray character sticks on a pane border (see draw_header); and it
is dropped whole rather than wrapped or collided when fewer than two spare
columns remain -- the key hints and the data ages are what the footer is for,
so neither may shed or truncate mid-word to make room for a version number.
"""

from __future__ import annotations

import curses
import importlib.machinery
import importlib.util
import re
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "leghorn.py"


def load():
    loader = importlib.machinery.SourceFileLoader("leghorn_footer_under_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


lg = load()


class FakeWin:
    """Records every addstr onto a character grid.

    Raises curses.error when a write would touch the final column, the way the
    real addstr misbehaves there -- but draw_footer swallows curses.error by
    design, so the tests assert on self.calls, not on the exception: the
    footer must never even *attempt* such a write.
    """

    def __init__(self, h, w):
        self.h, self.w = h, w
        self.calls = []
        self.grid = [[" "] * w for _ in range(h)]

    def addstr(self, y, x, text, attr=0):
        self.calls.append((y, x, text, attr))
        if x + len(text) > self.w - 1:
            raise curses.error("wrote into the final column")
        for i, ch in enumerate(text):
            self.grid[y][x + i] = ch

    def row(self, y):
        return "".join(self.grid[y])


class FooterStampTest(unittest.TestCase):
    STAMP = "v" + lg.__version__

    def setUp(self):
        # cp() needs start_color(), which needs a real terminal. The stand-in
        # encodes the pair id into the attribute so a test can read back which
        # colour a write asked for.
        self._cp = lg.cp
        lg.cp = lambda pair: pair << 20

    def tearDown(self):
        lg.cp = self._cp

    def _footer(self, w, h=24, message="", updated=0.0, gh_updated=0.0):
        win = FakeWin(h, w)
        lg.draw_footer(win, h, w, message, updated, gh_updated)
        return win

    def test_version_is_stamped_bottom_right(self):
        win = self._footer(150, updated=time.time())
        row = win.row(23)
        # Rightmost thing on the last row, but never in the final column.
        self.assertTrue(row.rstrip().endswith(self.STAMP))
        self.assertEqual(row[-1], " ")

    def test_stamp_is_dim(self):
        # Exact composition, not truthiness: PDCurses defines A_DIM as 0, so
        # `attr & A_DIM` proves nothing on Windows.
        win = self._footer(150)
        attr = next(a for _, _, t, a in win.calls if t == self.STAMP)
        self.assertEqual(attr, (lg.C_DIM << 20) | curses.A_DIM)

    def test_dropped_rather_than_truncating_the_hints(self):
        """At 80 columns the key hints fill the footer. The stamp must vanish
        whole -- not clip the hints mid-word, not write past the edge, where a
        wrapped line would scroll the display."""
        win = self._footer(80, updated=time.time())
        row = win.row(23)
        self.assertNotIn(self.STAMP, row)
        self.assertIn("q quit", row)
        for _, x, text, _ in win.calls:
            self.assertLessEqual(x + len(text), 79)

    def test_ages_outrank_the_stamp_when_only_one_fits(self):
        """"How old is this data" is the one fact a wall display must keep
        (the header sheds its labels in the same order), so when the row is
        too tight for both, the age keeps its slot and the stamp is dropped."""
        right = "updated %s ago" % lg.cb.ago(0)
        # Sized so the age fits in its original corner slot but not once
        # shifted left to make room for the stamp.
        win = self._footer(16 + len(right), h=10, message="x" * 10,
                           updated=time.time())
        row = win.row(9)
        self.assertIn("updated", row)
        self.assertNotIn(self.STAMP, row)

    def test_ages_shift_left_so_the_stamp_keeps_the_corner(self):
        win = self._footer(150, updated=time.time())
        self.assertRegex(win.row(23),
                         r"updated \S+ ago\s{2,}" + re.escape(self.STAMP) + r" $")

    def test_survives_a_tiny_window(self):
        # 10x40 and smaller: nothing may reach the final column, ever.
        for w, h in ((40, 10), (20, 5)):
            win = self._footer(w, h=h, updated=time.time())
            for _, x, text, _ in win.calls:
                self.assertLessEqual(x + len(text), w - 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
