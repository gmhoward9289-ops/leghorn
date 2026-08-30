#!/usr/bin/env python3
"""Tests for content-fitted column widths.

The session table's COL_WIDTH values used to be fixed widths: every column was
padded out to the widest thing it could ever hold, so a fleet of short names
rendered with dead gutters between columns. They are now ceilings -- a column
is drawn as wide as its widest current cell, capped at the old constant, with
hysteresis (ColumnFitter) so widths never dance frame-to-frame as rows churn.
The commits and github panes' repo columns get the same treatment.

Rendering is checked against a fake curses window on a character grid, the
same way test_leghorn_footer.py does -- the pty route is unavailable on
Windows, and Pane.put clips deterministically so the grid is the truth.
"""

from __future__ import annotations

import curses
import importlib.machinery
import importlib.util
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "leghorn.py"


def load():
    loader = importlib.machinery.SourceFileLoader("leghorn_columns_under_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


lg = load()


def make_row(name="a1", project="repo", tree="", branch="main", status="idle",
             ctx=12.0, task="", contested=False, git=None):
    if git is None:
        git = {"staged": 0, "dirty": 0, "untracked": 0,
               "ahead": 0, "behind": 0, "base": "main",
               "last": "subj", "last_age": 60}
    return {"name": name, "project": project, "tree": tree, "branch": branch,
            "status": status, "context_pct": ctx, "task": task,
            "contested": contested, "git": git, "git_dir": "/x", "dir": "/x",
            "pid": 1, "cost_usd": 0.0, "subagents": 0, "source": "claude"}


class FakeWin:
    """Records every addstr onto a character grid (see test_leghorn_footer)."""

    def __init__(self, h, w):
        self.h, self.w = h, w
        self.calls = []
        self.grid = [[" "] * w for _ in range(h)]

    def addstr(self, y, x, text, attr=0):
        self.calls.append((y, x, text, attr))
        if x + len(text) > self.w:
            raise curses.error("wrote past the window edge")
        for i, ch in enumerate(text):
            self.grid[y][x + i] = ch

    def row(self, y):
        return "".join(self.grid[y])


class NoColor(unittest.TestCase):
    def setUp(self):
        self._cp = lg.cp
        lg.cp = lambda pair: pair << 20

    def tearDown(self):
        lg.cp = self._cp


class ColumnFitterTest(unittest.TestCase):
    def test_grows_immediately(self):
        f = lg.ColumnFitter(memory=4)
        self.assertEqual(f.fit("k", 3, 11), 3)
        self.assertEqual(f.fit("k", 9, 11), 9)

    def test_caps_at_ceiling(self):
        f = lg.ColumnFitter(memory=4)
        self.assertEqual(f.fit("k", 40, 11), 11)

    def test_floor_of_one(self):
        f = lg.ColumnFitter(memory=4)
        self.assertEqual(f.fit("k", 0, 11), 1)

    def test_does_not_shrink_within_memory_window(self):
        """A wide row scrolling out of the feed must not snap the column
        narrower on the next frame -- widths that dance are worse than fixed."""
        f = lg.ColumnFitter(memory=5)
        f.fit("k", 10, 11)
        for _ in range(4):  # wider value still inside the window
            self.assertEqual(f.fit("k", 3, 11), 10)

    def test_shrinks_after_memory_window(self):
        f = lg.ColumnFitter(memory=5)
        f.fit("k", 10, 11)
        for _ in range(5):
            f.fit("k", 3, 11)
        self.assertEqual(f.fit("k", 3, 11), 3)

    def test_keys_are_independent(self):
        f = lg.ColumnFitter(memory=4)
        f.fit("a", 9, 11)
        self.assertEqual(f.fit("b", 2, 11), 2)


class FittedWidthsTest(unittest.TestCase):
    def test_width_is_widest_cell_capped(self):
        rows = [make_row(name="ab", project="repo"),
                make_row(name="abcd", project="a-very-long-project-name-here")]
        cells = [lg.session_cells(r, True) for r in rows]
        widths = lg.fitted_widths(cells, lg.ColumnFitter(memory=2))
        self.assertEqual(widths["name"], 4)
        self.assertEqual(widths["project"], lg.COL_WIDTH["project"])  # capped
        for key, w in widths.items():
            self.assertLessEqual(w, lg.COL_WIDTH[key])
            self.assertGreaterEqual(w, 1)


class SessionLayoutTest(unittest.TestCase):
    def test_defaults_to_ceilings(self):
        self.assertEqual(lg.session_layout(120, True),
                         lg.session_layout(120, True, lg.COL_WIDTH))

    def test_narrow_widths_leave_more_for_task(self):
        """Fitting only shrinks what the fixed columns claim; the freed space
        lands on the task column via the existing leftover rule."""
        narrow = {k: max(1, v - 3) for k, v in lg.COL_WIDTH.items()}
        wide = lg.session_layout(120, True)
        fit = lg.session_layout(120, True, narrow)
        self.assertGreater(fit["task"], wide["task"])

    def test_total_never_exceeds_inner_width(self):
        for inner in (30, 40, 72, 80, 120):
            layout = lg.session_layout(inner, True)
            used = sum(layout.values()) + lg.GAP * (len(layout) - 1)
            self.assertLessEqual(used, inner, "inner=%d" % inner)


class DrawSessionsTest(NoColor):
    def draw(self, rows, w=40, h=10, fitter=None):
        win = FakeWin(h, w)
        pane = lg.Pane(win, 0, 0, h, w, "SESSIONS")
        lg.draw_sessions(pane, rows, 0, 0, True,
                         fitter or lg.ColumnFitter(memory=2))
        return win

    def test_no_dead_gutter_for_short_names(self):
        """With 2-char names and 4-char projects, the status must start well
        left of where the fixed 11/19-wide columns used to put it."""
        rows = [make_row(name="a1", project="repo", status="Idle")]
        win = self.draw(rows, w=80)
        line = win.row(1)
        # Every gutter is exactly GAP wide: the columns sit shoulder to
        # shoulder instead of each padding out to its COL_WIDTH ceiling
        # (which would put 9 blanks after "a1" and 15 after "repo").
        self.assertIn("a1  repo  main  Idle  12%  clean", line)

    def test_forty_columns_never_writes_past_the_edge(self):
        rows = [make_row(name="agent-%d" % i, project="counting-chicken-wings",
                         tree="chore-deploy-env", status="Needs Input",
                         task="standardize the design tokens") for i in range(6)]
        win = self.draw(rows, w=40)  # FakeWin raises if a write escapes
        self.assertIn("agent-0", win.row(1))

    def test_widths_stable_while_rows_churn(self):
        """The frame after the widest row disappears must render the surviving
        rows at the same columns as the frame before."""
        fitter = lg.ColumnFitter(memory=10)
        keep = make_row(name="a1", project="repo", status="Idle")
        wide = make_row(name="long-name-x", project="repo", status="Idle")
        before = self.draw([keep, wide], w=80, fitter=fitter)
        after = self.draw([keep], w=80, fitter=fitter)
        self.assertEqual(before.row(1), after.row(1))


class DrawCommitsTest(NoColor):
    def commit(self, repo, subject="did a thing", ts=None):
        return {"repo": repo, "subject": subject, "ts": ts or time.time(),
                "sha": "abc1234", "author": "g", "refs": "main", "count": 1}

    def draw(self, commits, w=60, h=8):
        win = FakeWin(h, w)
        pane = lg.Pane(win, 0, 0, h, w, "COMMITS")
        lg.draw_commits(pane, commits, 0, 0, False,
                        fitter=lg.ColumnFitter(memory=2))
        return win

    def test_repo_column_fits_content(self):
        win = self.draw([self.commit("roost"), self.commit("leghorn")])
        # repo column is 7 wide (leghorn), not the fixed 14: the subject of
        # the "roost" row starts right after 7+1 columns of repo.
        line = win.row(1)
        self.assertEqual(line.find("did a thing") - line.find("roost"), 8)

    def test_repo_column_still_capped(self):
        win = self.draw([self.commit("a-repo-name-well-past-the-old-cap")])
        line = win.row(1)
        start = line.find("a-repo")
        self.assertEqual(line.find("did a thing") - start,
                         lg.COMMIT_REPO_MAX + 1)


class DrawGithubTest(NoColor):
    def event(self, repo, title="fix it"):
        return {"kind": "pr", "repo": repo, "number": 7, "title": title,
                "checks": "green", "review": "", "draft": False, "red": [],
                "branch": "main", "ts": time.time()}

    def draw(self, events, w=70, h=8):
        win = FakeWin(h, w)
        pane = lg.Pane(win, 0, 0, h, w, "GITHUB")
        lg.draw_github(pane, events, "", 0, 0, False,
                       fitter=lg.ColumnFitter(memory=2))
        return win

    def test_repo_column_fits_content(self):
        win = self.draw([self.event("roost"), self.event("wings")])
        line = win.row(1)
        self.assertEqual(line.find("#7 fix it") - line.find("roost"), 6)

    def test_repo_column_still_capped(self):
        win = self.draw([self.event("a-repo-name-well-past-the-old-cap")])
        line = win.row(1)
        self.assertEqual(line.find("#7 fix it") - line.find("a-repo"),
                         lg.GITHUB_REPO_MAX + 1)


if __name__ == "__main__":
    unittest.main()
