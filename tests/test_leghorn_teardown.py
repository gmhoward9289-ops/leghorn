#!/usr/bin/env python3
"""Tests for exit teardown: the prompt must come back exactly as it was.

Two real bugs live here. Quitting mid-sweep used to hold the shell prompt
hostage while the interpreter joined every queued git/gh call in the sweep
pools (their worker threads are non-daemon). And on Windows, PDCurses'
endwin does not undo curs_set(0), so the prompt returned with an invisible
cursor.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import threading
import time
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_loader(
        name + "_under_test",
        importlib.machinery.SourceFileLoader(name + "_under_test",
                                             str(HERE / (name + ".py"))),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cb = load("henhouse")
leghorn = load("leghorn")


class TestCancelDrainsSweeps(unittest.TestCase):
    """With CANCEL set, queued git/gh work must become instant no-ops --
    no subprocess may be spawned at all."""

    def setUp(self):
        cb.CANCEL.clear()

    def tearDown(self):
        cb.CANCEL.clear()

    def _forbid_subprocess(self):
        def boom(*a, **k):
            raise AssertionError("subprocess spawned after CANCEL")
        self._saved = cb.subprocess.run
        cb.subprocess = types.SimpleNamespace(
            run=boom,
            TimeoutExpired=Exception,
        )

    def _restore_subprocess(self):
        import subprocess
        cb.subprocess = subprocess

    def test_git_is_a_noop_after_cancel(self):
        cb.CANCEL.set()
        self._forbid_subprocess()
        try:
            self.assertIsNone(cb.git(".", "status"))
        finally:
            self._restore_subprocess()

    def test_gh_json_is_a_noop_after_cancel(self):
        cb.CANCEL.set()
        self._forbid_subprocess()
        try:
            self.assertIsNone(cb.gh_json("gh", ["pr", "list"], "."))
        finally:
            self._restore_subprocess()

    def test_model_stop_sets_cancel(self):
        model = leghorn.Model(5.0, True, True, False)
        self.assertFalse(leghorn.cb.CANCEL.is_set())
        model.stop()
        self.assertTrue(leghorn.cb.CANCEL.is_set())
        leghorn.cb.CANCEL.clear()


class TestCancelKillsInFlight(unittest.TestCase):
    """CANCEL alone only stops calls that have not started a subprocess yet.
    cancel_all() must also kill one already blocked in communicate() -- that
    is the difference between the prompt coming back instantly on quit and
    coming back only after that call's own timeout (up to GH_TIMEOUT=20s)."""

    def setUp(self):
        cb.CANCEL.clear()

    def tearDown(self):
        cb.CANCEL.clear()

    def test_cancel_all_kills_a_call_already_running(self):
        result = {}
        started = threading.Event()

        def worker():
            started.set()
            result["out"] = cb._run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=30)

        t = threading.Thread(target=worker)
        t.start()
        started.wait(timeout=5)
        time.sleep(0.3)  # give the child process time to actually launch
        cb.cancel_all()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(),
                         "cancel_all() did not kill the in-flight subprocess")
        # kill() makes communicate() return normally, just with a non-zero
        # code -- _run reports that raw result and leaves "was this killed
        # vs. a real failure" to the caller, exactly as git()/gh_json()/
        # gather_git() already do by treating any non-zero code as None.
        out = result.get("out")
        self.assertIsNotNone(out)
        self.assertNotEqual(out.returncode, 0)


class TestCursorRestore(unittest.TestCase):
    """loop() must re-show the cursor on every way out, including a crash --
    endwin alone does not do it on windows-curses."""

    def _run_loop(self, body, curs_set):
        saved_curses, saved_loop = leghorn.curses, leghorn._loop

        class FakeError(Exception):
            pass

        leghorn.curses = types.SimpleNamespace(curs_set=curs_set, error=FakeError)
        leghorn._loop = body
        try:
            leghorn.loop(None, None, None)
        finally:
            leghorn.curses, leghorn._loop = saved_curses, saved_loop

    def test_cursor_restored_on_clean_exit(self):
        calls = []
        self._run_loop(lambda *a: None, lambda v: calls.append(v))
        self.assertEqual(calls, [1])

    def test_cursor_restored_when_the_loop_crashes(self):
        calls = []

        def crash(*a):
            raise RuntimeError("draw blew up")

        with self.assertRaises(RuntimeError):
            self._run_loop(crash, lambda v: calls.append(v))
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
