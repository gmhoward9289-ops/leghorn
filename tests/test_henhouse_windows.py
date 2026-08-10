#!/usr/bin/env python3
"""Tests for the two places henhouse has to care what OS it is on.

Both behaviours were written on Windows, lived in an untracked copy of this
file on that machine alone, and were absent here -- so this file exists to
stop them going missing a second time.

The liveness one is not a nicety. `os.kill(pid, 0)` is a read-only probe on
POSIX, but CPython on Windows routes every signal except CTRL_C_EVENT and
CTRL_BREAK_EVENT to TerminateProcess: the probe kills what it asks about. A
dashboard that polls every live Claude Code session would have terminated all
of them. The test below therefore asserts on a real child process still being
alive after the probe, not merely on the return value.

These run and mean something on both platforms: the win32 tests skip on POSIX,
and the path-join tests run everywhere because normcase is a no-op on POSIX
and must stay harmless there.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "henhouse.py"


def load():
    spec = importlib.util.spec_from_loader(
        "coop_under_test",
        importlib.machinery.SourceFileLoader("coop_under_test", str(SCRIPT)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cb = load()


class TestLiveness(unittest.TestCase):
    def test_reports_self_alive_and_a_dead_pid_dead(self):
        self.assertTrue(cb.alive(os.getpid()))
        self.assertFalse(cb.alive(999999))

    def test_probing_a_process_does_not_kill_it(self):
        """The whole reason the win32 branch exists."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(cb.alive(child.pid))
            self.assertIsNone(child.poll(), "alive() terminated the process it probed")
        finally:
            child.kill()
            child.wait(timeout=10)

    @unittest.skipUnless(sys.platform == "win32", "win32-only path")
    def test_win32_never_reaches_os_kill(self):
        """Belt and braces: on Windows the os.kill call must be unreachable."""
        called = []
        real_kill = os.kill
        os.kill = lambda *a, **k: called.append(a)
        try:
            cb.alive(os.getpid())
        finally:
            os.kill = real_kill
        self.assertEqual(called, [], "win32 branch fell through to os.kill")


class TestGitStateJoin(unittest.TestCase):
    """git-roost reports `toplevel` with forward slashes even on Windows,
    while the dirs henhouse looks up with are native. Unjoined, every git
    column on Windows read as a dash."""

    def _gather(self, dirs, toplevel):
        record = {
            "toplevel": toplevel, "staged": 0, "unstaged": 1, "untracked": 0,
            "ahead": 0, "behind": 0, "base": "main", "operation": "",
            "last_subject": "x", "last_ts": None,
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps([record]), "")
        real_find, real_run = cb.find_git_roost, subprocess.run
        cb.find_git_roost = lambda: ["git-roost"]
        cb.subprocess.run = lambda *a, **k: completed
        try:
            return cb.gather_git(dirs)
        finally:
            cb.find_git_roost, cb.subprocess.run = real_find, real_run

    # An absolute tree in each platform's own spelling, plus the forward-slash
    # form git-roost reports for it. On POSIX the two are identical, which is
    # the point: the join must stay correct where there is nothing to fold.
    if sys.platform == "win32":
        NATIVE_ROOT, REPORTED_ROOT = r"C:\repos\roost", "C:/repos/roost"
    else:
        NATIVE_ROOT, REPORTED_ROOT = "/repos/roost", "/repos/roost"

    def test_separator_spelling_does_not_break_the_join(self):
        states = self._gather([self.NATIVE_ROOT], self.REPORTED_ROOT)
        self.assertIsNotNone(states.get(self.NATIVE_ROOT),
                             "git state did not reach the directory it belongs to")
        self.assertEqual(states[self.NATIVE_ROOT]["dirty"], 1)

    def test_a_dir_inside_a_probed_tree_still_finds_its_state(self):
        inner = os.path.join(self.NATIVE_ROOT, "packaging")
        states = self._gather([inner], self.REPORTED_ROOT)
        self.assertIsNotNone(states.get(inner))


if __name__ == "__main__":
    unittest.main()
