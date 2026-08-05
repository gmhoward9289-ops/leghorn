#!/usr/bin/env python3
"""Tests for leghorn's pure UI logic (no curses drawing).

Importing leghorn.py is safe headless: nothing at module scope draws, and the
data layer it loads only shells out when called. These cover the decisions the
key loop makes before it paints -- filters, sorts, elision, scroll clamping,
and the colour buckets -- which are the bits that have already shipped bugs.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "leghorn.py"


def load():
    loader = importlib.machinery.SourceFileLoader("leghorn_logic_under_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


lg = load()


def row(**kw):
    base = {
        "name": "s1", "status": "Idle", "context_pct": 40,
        "contested": False, "task": "", "git": {},
    }
    base.update(kw)
    return base


class CommitsWidthTest(unittest.TestCase):
    def test_clamps_to_min_and_max(self):
        self.assertEqual(lg.commits_width(200), lg.COMMITS_MAX)
        self.assertEqual(lg.commits_width(100), lg.COMMITS_MIN)

    def test_override_never_starves_sessions(self):
        # Even a huge override must leave SESSIONS_MIN for the left pane.
        w = 120
        got = lg.commits_width(w, override=200)
        self.assertEqual(got, w - lg.SESSIONS_MIN)
        self.assertGreaterEqual(got, 20)


class ElideTest(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(lg.elide("leghorn", 20), "leghorn")

    def test_slash_keeps_both_ends(self):
        # Plain truncation would make every worktree of one repo identical.
        text = "counting-chicken-wings/chore-deploy-env"
        got = lg.elide(text, 20)
        self.assertEqual(len(got), 20)
        self.assertIn("…", got)
        self.assertTrue(got.startswith("counting"))
        self.assertTrue(got.endswith("env"))

    def test_no_slash_truncates_from_the_end(self):
        self.assertEqual(lg.elide("abcdefghijklmnop", 8), "abcdefgh")


class MatchesFilterTest(unittest.TestCase):
    def test_all_keeps_everything(self):
        self.assertTrue(lg.matches(row(), "all"))

    def test_contested(self):
        self.assertTrue(lg.matches(row(contested=True), "contested"))
        self.assertFalse(lg.matches(row(contested=False), "contested"))

    def test_needs_attention_strips_spaces(self):
        self.assertTrue(lg.matches(row(status="Needs Input"), "needs attention"))
        self.assertFalse(lg.matches(row(status="Idle"), "needs attention"))

    def test_uncommitted(self):
        dirty = row(git={"staged": 1, "dirty": 0, "untracked": 0})
        self.assertTrue(lg.matches(dirty, "uncommitted"))
        self.assertFalse(lg.matches(row(), "uncommitted"))

    def test_claimed(self):
        self.assertTrue(lg.matches(row(task="fix the join"), "claimed"))
        self.assertFalse(lg.matches(row(task=""), "claimed"))


class ApplySortTest(unittest.TestCase):
    def test_name_sort_is_alphabetical(self):
        rows = [row(name="z"), row(name="a"), row(name="m")]
        self.assertEqual(
            [r["name"] for r in lg.apply_sort(rows, "name")],
            ["a", "m", "z"],
        )

    def test_context_sort_highest_first(self):
        rows = [row(name="low", context_pct=10),
                row(name="high", context_pct=90),
                row(name="mid", context_pct=40)]
        self.assertEqual(
            [r["name"] for r in lg.apply_sort(rows, "context")],
            ["high", "mid", "low"],
        )

    def test_dirty_sort_puts_uncommitted_first(self):
        clean = row(name="clean", git={})
        dirty = row(name="dirty", git={"staged": 0, "dirty": 2, "untracked": 0})
        behind = row(name="behind", git={"staged": 0, "dirty": 0, "untracked": 0,
                                         "behind": 5})
        got = [r["name"] for r in lg.apply_sort([clean, behind, dirty], "dirty")]
        self.assertEqual(got[0], "dirty")
        # Equal dirt (0) then higher behind wins.
        self.assertEqual(got[1:], ["behind", "clean"])

    def test_commit_age_stale_floats_never_committed_last(self):
        # Higher last_age (seconds since last commit) sorts first; missing age
        # uses -1 so it never pretends to be brand new.
        fresh = row(name="fresh", git={"last_age": 10})
        old = row(name="old", git={"last_age": 9000})
        none = row(name="none", git={})
        got = [r["name"] for r in lg.apply_sort([old, none, fresh], "commit age")]
        self.assertEqual(got, ["old", "fresh", "none"])

    def test_default_attention_sort_uses_ccboard_key(self):
        blocked = row(name="blocked", status="Needs Input")
        idle = row(name="idle", status="Idle", context_pct=90)
        got = [r["name"] for r in lg.apply_sort([idle, blocked], "attention")]
        self.assertEqual(got, ["blocked", "idle"])


class ClampScrollTest(unittest.TestCase):
    def test_zero_visible_returns_zero(self):
        self.assertEqual(lg.clamp_scroll(5, 3, 0), 0)

    def test_selection_above_scroll_pulls_up(self):
        self.assertEqual(lg.clamp_scroll(2, 5, 10), 2)

    def test_selection_below_window_pulls_down(self):
        # sel=15, scroll=0, visible=10 -> scroll so 15 is last visible (6).
        self.assertEqual(lg.clamp_scroll(15, 0, 10), 6)

    def test_selection_inside_window_unchanged(self):
        self.assertEqual(lg.clamp_scroll(7, 5, 10), 5)


class ColourBucketTest(unittest.TestCase):
    def test_status_color_buckets(self):
        self.assertEqual(lg.status_color("Error"), lg.C_RED)
        self.assertEqual(lg.status_color("Needs Input"), lg.C_YELLOW)
        self.assertEqual(lg.status_color("Processing"), lg.C_GREEN)
        self.assertEqual(lg.status_color("Idle"), lg.C_DIM)

    def test_ctx_color_buckets(self):
        self.assertEqual(lg.ctx_color(120), lg.C_RED)
        self.assertEqual(lg.ctx_color(70), lg.C_YELLOW)
        self.assertEqual(lg.ctx_color(40), lg.C_DIM)
        self.assertEqual(lg.ctx_color(None), lg.C_DIM)


class SessionLayoutTest(unittest.TestCase):
    def test_core_fits_when_there_is_room(self):
        # Core columns cost ~66 with gaps; under that, git is the first drop.
        layout = lg.session_layout(80, use_git=True)
        for key in ("dot", "name", "project", "status", "ctx", "git"):
            self.assertIn(key, layout)

    def test_too_narrow_drops_git_before_status(self):
        layout = lg.session_layout(60, use_git=True)
        self.assertIn("status", layout)
        self.assertIn("ctx", layout)
        self.assertNotIn("git", layout)

    def test_no_git_drops_git_column(self):
        layout = lg.session_layout(120, use_git=False)
        self.assertNotIn("git", layout)


if __name__ == "__main__":
    unittest.main()
