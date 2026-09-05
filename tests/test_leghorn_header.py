#!/usr/bin/env python3
"""Tests for the header's shedding order and the honesty of its chips.

Two invariants, both from CLAUDE.md and both broken on main before this test
existed:

1. The clock is the LAST thing the header gives up. Mode labels shed first,
   then stat chips right-to-left, and only then the clock -- a wall display
   must always answer "when did this last update". The old header stopped
   *adding* chips at the right edge but never removed one to make room, so at
   40 and 60 columns it showed "3 sessions · 1 shared · 1 uncommitted" and
   no clock at all.

2. The chips count the whole fleet. draw_header used to receive the filtered
   rows, so "N uncommitted" silently changed meaning the moment `f` cycled
   the filter; only "N shown" is about the filtered view, and it says so.

Same grid-fake approach as test_leghorn_footer.py: FakeWin raises on any
write that reaches the final column, because that write wraps onto the
SESSIONS pane's top border.
"""

from __future__ import annotations

import curses
import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "leghorn.py"


def load():
    loader = importlib.machinery.SourceFileLoader("leghorn_header_under_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


lg = load()

UPDATED = 1700000000.0  # any fixed instant; the test reads the clock back


def make_row(git_dir, dirty=0, behind=0, contested=False):
    return {"contested": contested, "git_dir": git_dir,
            "git": {"staged": dirty, "dirty": 0, "untracked": 0,
                    "ahead": 0, "behind": behind}}


class FakeWin:
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


class HeaderTest(unittest.TestCase):
    def setUp(self):
        self._cp = lg.cp
        lg.cp = lambda pair: pair << 20
        self.clock = lg.time.strftime("%H:%M:%S", lg.time.localtime(UPDATED))

    def tearDown(self):
        lg.cp = self._cp

    # Three trees: one dirty, one behind, one shared -- every fleet chip lit.
    FLEET = [make_row("a", dirty=1), make_row("b", behind=1),
             make_row("c", contested=True)]

    def header(self, w, rows_all=None, shown=None, gh_events=(), filt="all"):
        rows_all = self.FLEET if rows_all is None else rows_all
        shown = len(rows_all) if shown is None else shown
        win = FakeWin(3, w)
        lg.draw_header(win, w, rows_all, shown, UPDATED, False,
                       "attention", filt, gh_events, "normal")
        return win

    # -- the clock survives everything -----------------------------------

    def test_clock_survives_at_40_60_and_80(self):
        for w in (40, 60, 80):
            line = self.header(w).row(0)
            self.assertIn(self.clock, line, "w=%d: %r" % (w, line))
            self.assertTrue(line.rstrip().endswith(self.clock),
                            "clock is not the right-most thing at w=%d: %r" % (w, line))

    def test_chips_shed_right_to_left_before_the_clock(self):
        """At 40 columns only "3 sessions" fits beside the clock; at 60 the
        shared chip is back ("1 uncommitted" misses by one cell -- the clock
        wins the tie); at 80 every chip fits (mode labels still shed -- they
        go first)."""
        at40 = self.header(40).row(0)
        self.assertIn("3 sessions", at40)
        self.assertNotIn("shared", at40)
        self.assertNotIn("uncommitted", at40)

        at60 = self.header(60).row(0)
        self.assertIn("3 sessions · 1 shared", at60)
        self.assertNotIn("uncommitted", at60)
        self.assertNotIn("behind", at60)

        at80 = self.header(80).row(0)
        self.assertIn("3 sessions · 1 shared · 1 uncommitted · 1 behind", at80)

    def test_mode_labels_shed_before_chips(self):
        """A width where the full right side cannot fit but every chip can:
        the labels go, the chips stay. Preserves CLAUDE.md's fallback chain."""
        wide = self.header(140).row(0)
        self.assertIn("sort:attention  filter:all  speed:normal", wide)
        mid = self.header(90).row(0)
        self.assertNotIn("speed:", mid)
        self.assertIn("1 behind", mid)
        self.assertIn(self.clock, mid)

    def test_first_chip_goes_before_the_clock(self):
        """Even "N sessions" yields to the clock when only one of them fits."""
        # NAME (7) + margins = col 10; clock is 10 wide; needs w > 22.
        line = self.header(24).row(0)
        self.assertIn(self.clock, line)
        self.assertNotIn("sessions", line)

    def test_nothing_touches_the_final_column(self):
        """FakeWin raises on a final-column write and draw_header swallows
        curses.error -- so assert on the recorded calls, as the footer tests
        do: the header must never even attempt such a write."""
        for w in range(20, 121):
            win = self.header(w)
            for _, x, text, _ in win.calls:
                self.assertLessEqual(x + len(text), w - 1,
                                     "w=%d wrote %r at %d" % (w, text, x))

    def test_plan_header_is_total(self):
        """Every width from the narrowest up yields a plan whose chips end
        before the final column and whose right text (if any) fits."""
        chips = lg.header_chips(self.FLEET, 3)
        rights = ("sort:attention  filter:all  speed:normal  12:00:00  ",
                  "sort:attention  filter:all  12:00:00  ", "12:00:00  ")
        for w in range(12, 160):
            kept, right = lg.plan_header(w, 10, chips, rights)
            self.assertLess(lg.chips_end(10, kept), w)
            if right is not None:
                self.assertGreater(w - len(right) - 2, lg.chips_end(10, kept))
            elif w > 22:
                self.fail("clock dropped at w=%d though it fits" % w)

    # -- the chips count the fleet, not the filtered view -----------------

    def test_chips_are_computed_over_the_whole_fleet(self):
        """Filtering down to the one clean tree must not turn the header into
        "1 sessions" with no uncommitted/behind/shared -- those counts are
        fleet facts. Only "N shown" is about the filtered list."""
        line = self.header(100, rows_all=self.FLEET, shown=1,
                           filt="contested").row(0)
        self.assertIn("3 sessions · 1 shown · 1 shared · 1 uncommitted · 1 behind",
                      line)

    def test_shown_chip_only_when_filtered(self):
        self.assertNotIn("shown", self.header(100).row(0))
        self.assertIn("2 shown", self.header(100, shown=2).row(0))

    def test_header_chips_dedupe_on_tree(self):
        """Two sessions in one dirty tree are one uncommitted tree."""
        rows = [make_row("same", dirty=1), make_row("same", dirty=1)]
        texts = [t for t, _ in lg.header_chips(rows, 2)]
        self.assertIn("1 uncommitted", texts)

    def test_ci_chips_from_gh_events(self):
        events = [{"kind": "run", "state": "failed"},
                  {"kind": "run", "state": "queued"},
                  {"kind": "pr", "checks": "red"}]
        texts = [t for t, _ in lg.header_chips(self.FLEET, 3, events)]
        self.assertIn("2 ci red", texts)
        self.assertIn("1 ci running", texts)


if __name__ == "__main__":
    unittest.main()
