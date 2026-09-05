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
        # The real hazard (CLAUDE.md) is the LAST column: a write into it
        # wraps onto the next row's pane border. Match test_leghorn_footer and
        # fail on any write that reaches w - 1, not only one that passes w.
        if x + len(text) > self.w - 1:
            raise curses.error("wrote into the final column")
        for i, ch in enumerate(text):
            self.grid[y][x + i] = ch

    def row(self, y):
        return "".join(self.grid[y])


class Clock:
    """A steppable stand-in for time.monotonic."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, seconds):
        self.t += seconds


def fitter(memory=10.0, clock=None):
    return lg.ColumnFitter(memory=memory, clock=clock or Clock())


class NoColor(unittest.TestCase):
    def setUp(self):
        self._cp = lg.cp
        lg.cp = lambda pair: pair << 20

    def tearDown(self):
        lg.cp = self._cp


class ColumnFitterTest(unittest.TestCase):
    def test_grows_immediately(self):
        f = fitter()
        self.assertEqual(f.fit("k", 3, 11), 3)
        self.assertEqual(f.fit("k", 9, 11), 9)

    def test_caps_at_ceiling(self):
        self.assertEqual(fitter().fit("k", 40, 11), 11)

    def test_floor_of_one(self):
        self.assertEqual(fitter().fit("k", 0, 11), 1)

    def test_does_not_shrink_within_memory_window(self):
        """A wide row scrolling out of the feed must not snap the column
        narrower on the next frame -- widths that dance are worse than fixed."""
        clock = Clock()
        f = fitter(memory=10.0, clock=clock)
        f.fit("k", 10, 11)
        for _ in range(4):  # 9.6s elapsed at the end: still inside the window
            clock.tick(2.4)
            self.assertEqual(f.fit("k", 3, 11), 10)

    def test_shrinks_after_memory_window(self):
        clock = Clock()
        f = fitter(memory=10.0, clock=clock)
        f.fit("k", 10, 11)
        clock.tick(10.5)
        self.assertEqual(f.fit("k", 3, 11), 3)

    def test_memory_is_wall_clock_not_frames(self):
        """Key-repeat draws many frames a second; a paused draw loop produces
        none. Neither must change how long a width is remembered."""
        clock = Clock()
        f = fitter(memory=10.0, clock=clock)
        f.fit("k", 10, 11)
        for _ in range(200):  # a burst of frames in 2 seconds must not expire it
            clock.tick(0.01)
            self.assertEqual(f.fit("k", 3, 11), 10)
        clock.tick(60)  # one frame after a long pause must expire it
        self.assertEqual(f.fit("k", 3, 11), 3)

    def test_keys_are_independent(self):
        f = fitter()
        f.fit("a", 9, 11)
        self.assertEqual(f.fit("b", 2, 11), 2)

    def test_default_memory_is_seconds(self):
        self.assertIsInstance(lg.FIT_MEMORY, float)
        self.assertGreaterEqual(lg.FIT_MEMORY, 5.0)


class FittedWidthsTest(unittest.TestCase):
    def test_width_is_widest_cell_capped(self):
        rows = [make_row(name="ab", project="repo"),
                make_row(name="abcd", project="a-very-long-project-name-here")]
        cells = [lg.session_cells(r, True) for r in rows]
        widths = lg.fitted_widths(cells, fitter())
        self.assertEqual(widths["name"], 4)
        self.assertEqual(widths["project"], lg.COL_WIDTH["project"])  # capped
        for key, w in widths.items():
            self.assertLessEqual(w, lg.COL_WIDTH[key])
            self.assertGreaterEqual(w, 1)

    def test_task_and_dot_are_not_fitted(self):
        """task is the leftover column and dot's ceiling is one cell: fitting
        either is work the layout never reads."""
        cells = [lg.session_cells(make_row(task="a long task string"), True)]
        widths = lg.fitted_widths(cells, fitter())
        self.assertNotIn("task", widths)
        self.assertNotIn("dot", widths)
        self.assertEqual(set(widths), set(lg.COL_FITTED))


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
        self.assertEqual(fit["name"], lg.COL_WIDTH["name"] - 3)

    def test_total_never_exceeds_inner_width(self):
        for inner in (30, 40, 72, 80, 120):
            layout = lg.session_layout(inner, True)
            used = sum(layout.values()) + lg.GAP * (len(layout) - 1)
            self.assertLessEqual(used, inner, "inner=%d" % inner)

    def test_fitted_widths_never_grow_a_column_past_its_bid(self):
        """widths above the ceiling (or above what the layout granted) are
        clamped -- fitting only ever narrows."""
        wide = {k: 99 for k in lg.COL_WIDTH}
        self.assertEqual(lg.session_layout(120, True, wide),
                         lg.session_layout(120, True))

    def test_membership_is_independent_of_fitted_widths(self):
        """The key set comes from the COL_WIDTH ceilings alone. A fleet of
        short names must not let branch/age in only for a long name to evict
        them the frame it appears (and re-admit them when the memory window
        expires) -- that is hysteresis in one direction, and the table jumps."""
        narrow = {k: 1 for k in lg.COL_FITTED}
        for inner in (30, 38, 40, 60, 72, 78, 80, 120):
            for use_git in (True, False):
                base = lg.session_layout(inner, use_git)
                fit = lg.session_layout(inner, use_git, narrow)
                self.assertEqual(set(base), set(fit),
                                 "inner=%d use_git=%s" % (inner, use_git))
                # ...and the savings all landed on task (when there is one;
                # below that width the gutter savings are trailing blank).
                if "task" in base:
                    self.assertEqual(sum(base.values()), sum(fit.values()))
                else:
                    self.assertLessEqual(sum(fit.values()), sum(base.values()))


class DrawSessionsTest(NoColor):
    def draw(self, rows, w=40, h=10, fitter_=None):
        win = FakeWin(h, w)
        pane = lg.Pane(win, 0, 0, h, w, "SESSIONS")
        lg.draw_sessions(pane, rows, 0, 0, True, fitter_ or fitter())
        return win

    def layout_of(self, rows, w, fitter_):
        cells = [lg.session_cells(r, True) for r in rows]
        return lg.session_layout(w - 2, True, lg.fitted_widths(cells, fitter_))

    def test_wide_row_appearing_does_not_change_the_column_set(self):
        """Reviewer's repro: at inner 78 a long name used to drop task from
        37 to 13 and lose branch/age for a frame; at inner 38 status/ctx/git/
        branch vanished. The key set must be the same before and after."""
        short = [make_row(name="a%d" % i, project="r", branch="m",
                          status="Idle", task="t") for i in range(3)]
        wide = make_row(name="a-very-long-name", project="counting-chicken-wings",
                        tree="chore-deploy-env", branch="feat/long-branch-name",
                        status="Needs Input", task="a long task")
        for inner in (78, 38):
            f = fitter()
            before = self.layout_of(short, inner + 2, f)
            self.draw(short, w=inner + 2, fitter_=f)
            after = self.layout_of(short + [wide], inner + 2, f)
            self.draw(short + [wide], w=inner + 2, fitter_=f)
            self.assertEqual(set(before), set(after), "inner=%d" % inner)
            self.assertEqual(set(after), set(lg.session_layout(inner, True)),
                             "inner=%d" % inner)

    def test_no_dead_gutter_for_short_names(self):
        """With 2-char names and 4-char projects, the status must start well
        left of where the fixed 11/19-wide columns used to put it."""
        rows = [make_row(name="a1", project="repo", status="Idle")]
        win = self.draw(rows, w=80)
        line = win.row(1)
        # Every gutter is exactly GAP wide: the columns sit shoulder to
        # shoulder instead of each padding out to its COL_WIDTH ceiling
        # (which would put 9 blanks after "a1" and 15 after "repo"). No
        # branch here: the ceilings never admitted it at inner 78, and the
        # fitted savings widen the task column rather than buying a column
        # the next long name would evict again.
        self.assertIn("a1  repo  Idle  12%  clean", line)
        self.assertNotIn("main", line)
        # Once the terminal is wide enough for branch under the ceilings, it
        # is fitted too.
        wide = self.draw(rows, w=122).row(1)
        self.assertIn("a1  repo  main  Idle  12%  clean  1m", wide)

    def busy_rows(self, n):
        return [make_row(name="agent-%d" % i, project="counting-chicken-wings",
                         tree="chore-deploy-env", status="Needs Input",
                         branch="feat/standardize-tokens",
                         task="standardize the design tokens across the flock")
                for i in range(n)]

    def test_forty_columns_never_writes_into_the_final_column(self):
        win = self.draw(self.busy_rows(6), w=40, h=10)  # FakeWin raises
        self.assertIn("agent-0", win.row(1))
        for y in range(1, 7):
            self.assertEqual(win.grid[y][39], " ")

    def test_eighty_columns_never_writes_into_the_final_column(self):
        """The comfortable window too: a full 24x80 with more rows than fit,
        every column present and the task text longer than its share."""
        win = self.draw(self.busy_rows(30), w=80, h=24)
        self.assertIn("agent-0", win.row(1))
        self.assertIn("agent-21", win.row(22))
        for y in range(1, 23):
            self.assertEqual(win.grid[y][79], " ")

    def test_widths_stable_while_rows_churn(self):
        """The frame after the widest row disappears must render the surviving
        rows at the same columns as the frame before."""
        f = fitter()
        keep = make_row(name="a1", project="repo", status="Idle")
        wide = make_row(name="long-name-x", project="repo", status="Idle")
        before = self.draw([keep, wide], w=80, fitter_=f)
        after = self.draw([keep], w=80, fitter_=f)
        self.assertEqual(before.row(1), after.row(1))


class DrawCommitsTest(NoColor):
    def commit(self, repo, subject="did a thing", ts=None):
        return {"repo": repo, "subject": subject, "ts": ts or time.time(),
                "sha": "abc1234", "author": "g", "refs": "main", "count": 1}

    def draw(self, commits, w=60, h=8):
        win = FakeWin(h, w)
        pane = lg.Pane(win, 0, 0, h, w, "COMMITS")
        lg.draw_commits(pane, commits, 0, 0, False,
                        fitter=fitter())
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
                       fitter=fitter())
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
