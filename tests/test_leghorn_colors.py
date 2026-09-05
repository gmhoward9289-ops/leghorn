#!/usr/bin/env python3
"""Tests for init_colors: the identity blue must be *blue* on every curses.

The real bug: init_colors asked for raw palette index 12 as "bright blue",
which is only true under xterm ordering (COLOR_BLUE=4, bright = 4+8). PDCurses,
which backs windows-curses, uses Windows console ordering (COLOR_BLUE=1,
COLOR_RED=4), so 12 = 8+4 rendered the identity colour bright RED on Windows.
The portable spelling is COLOR_BLUE + 8 -- bright variants always sit at
basic+8, whichever way the basic eight are numbered.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "leghorn.py"


def load():
    loader = importlib.machinery.SourceFileLoader("leghorn_colors_under_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


lg = load()


class FakeCursesError(Exception):
    pass


def fake_curses(colors, ordering):
    """A curses stand-in with a given COLORS count and basic-colour numbering.

    ordering is "xterm" (ncurses: blue=4, red=1) or "windows" (PDCurses:
    blue=1, red=4). Records every init_pair call.
    """
    if ordering == "xterm":
        basic = dict(BLACK=0, RED=1, GREEN=2, YELLOW=3, BLUE=4, MAGENTA=5, CYAN=6, WHITE=7)
    elif ordering == "windows":
        basic = dict(BLACK=0, BLUE=1, GREEN=2, CYAN=3, RED=4, MAGENTA=5, YELLOW=6, WHITE=7)
    else:
        raise ValueError(ordering)
    fake = types.SimpleNamespace(
        COLORS=colors,
        error=FakeCursesError,
        pairs=[],
        start_color=lambda: None,
        use_default_colors=lambda: None,
    )
    for name, idx in basic.items():
        setattr(fake, "COLOR_" + name, idx)
    fake.init_pair = lambda pair, fg, bg: fake.pairs.append((pair, fg, bg))
    return fake


def blue_fg(fake):
    fgs = [fg for pair, fg, _bg in fake.pairs if pair == lg.C_BLUE]
    assert len(fgs) == 1, fake.pairs
    return fgs[0]


class TestBrightBlueIndex(unittest.TestCase):
    def test_windows_ordering_requests_bright_blue_not_12(self):
        # The COOPER bug: under PDCurses numbering, 12 is bright red.
        fake = fake_curses(colors=256, ordering="windows")
        with mock.patch.object(lg, "curses", fake):
            lg.init_colors()
        fg = blue_fg(fake)
        self.assertEqual(fg, fake.COLOR_BLUE + 8)
        self.assertEqual(fg, 9)
        self.assertNotEqual(fg, 12, "12 is COLOR_RED + 8 under PDCurses ordering")
        self.assertNotEqual(fg, fake.COLOR_RED + 8)

    def test_xterm_ordering_still_lands_on_12(self):
        # On ncurses COLOR_BLUE + 8 *is* 12, so nothing changes for xterm users.
        fake = fake_curses(colors=256, ordering="xterm")
        with mock.patch.object(lg, "curses", fake):
            lg.init_colors()
        self.assertEqual(blue_fg(fake), 12)
        self.assertEqual(blue_fg(fake), fake.COLOR_BLUE + 8)

    def test_eight_color_terminal_falls_back_to_basic_blue(self):
        # The COLORS >= 16 guard must survive: an 8-colour terminal gets the
        # basic blue (A_BOLD at the use sites does the brightening there).
        for ordering in ("xterm", "windows"):
            fake = fake_curses(colors=8, ordering=ordering)
            with mock.patch.object(lg, "curses", fake):
                lg.init_colors()
            self.assertEqual(blue_fg(fake), fake.COLOR_BLUE, ordering)

    def test_c_blue_uses_carry_bold(self):
        # The pairing the fallback relies on: every C_BLUE draw is A_BOLD.
        src = SCRIPT.read_text(encoding="utf-8")
        uses = [line for line in src.splitlines()
                if "cp(C_BLUE)" in line and "def cp" not in line]
        self.assertTrue(uses, "expected at least one cp(C_BLUE) use")
        for line in uses:
            self.assertIn("A_BOLD", line, line)


if __name__ == "__main__":
    unittest.main()
