#!/usr/bin/env python3
"""Tests for leghorn's refresh-speed presets.

Importing leghorn.py is safe headless: nothing at module scope touches curses,
and the data layer it loads is coop, which only shells out when called.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "leghorn.py"


def load():
    loader = importlib.machinery.SourceFileLoader("leghorn_speed_under_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


lg = load()


class SpeedTableTest(unittest.TestCase):
    def test_presets_are_ordered_slowest_last(self):
        # The ladder is the whole feature: a preset that refreshes faster than
        # the one above it makes the names lie.
        sessions = [i for _, i, _ in lg.SPEEDS]
        gh = [g for _, _, g in lg.SPEEDS]
        self.assertEqual(sessions, sorted(sessions))
        self.assertEqual(gh, sorted(gh))

    def test_gh_never_sweeps_faster_than_a_minute(self):
        # A sweep is seconds of network against a rate-limited API; anything
        # under a minute overlaps rather than arriving sooner.
        for name, _, gh in lg.SPEEDS:
            with self.subTest(speed=name):
                self.assertGreaterEqual(gh, 60)

    def test_named_lookup(self):
        self.assertEqual(lg.speed_settings("slow"), ("slow", 300.0, 21600.0))
        self.assertEqual(lg.speed_settings("ultra")[1], 1.0)

    def test_unknown_name_falls_back_to_the_default(self):
        self.assertEqual(lg.speed_settings("nonsense"),
                         lg.speed_settings(lg.DEFAULT_SPEED))

    def test_default_is_a_real_preset(self):
        self.assertIn(lg.DEFAULT_SPEED, [n for n, _, _ in lg.SPEEDS])


class ModelSpeedTest(unittest.TestCase):
    def _model(self):
        m = lg.Model(5.0, True, True, True, 75.0)
        m._wake.clear()
        m._gh_wake.clear()
        return m

    def test_set_speed_retunes_and_wakes_the_session_thread(self):
        # Waking is the point: a wait started under slow is six hours long, so
        # a preset change that only rewrote the fields would not take effect
        # until the next tick.
        m = self._model()
        m.set_speed(300.0, 21600.0)
        self.assertEqual((m.interval, m.github_interval), (300.0, 21600.0))
        self.assertTrue(m._wake.is_set())

    def test_slowing_down_does_not_fire_a_github_sweep(self):
        # Cycling p toward a slower preset must not sweep: four presses back
        # around the ladder would otherwise mean four network sweeps in a row,
        # the exact overlap the 60s floor exists to prevent.
        m = self._model()
        m.set_speed(300.0, 21600.0)
        self.assertFalse(m._gh_wake.is_set())

    def test_speeding_up_does_fire_one(self):
        # Going the other way, the pending wait is longer than the new period,
        # so it has to be cut short or the change does not take effect at all.
        m = self._model()
        m.set_speed(1.0, 60.0)
        self.assertTrue(m._gh_wake.is_set())

    def test_refresh_now_still_sweeps(self):
        # r is the explicit "go now" and must keep hitting both.
        m = self._model()
        m.refresh_now()
        self.assertTrue(m._wake.is_set())
        self.assertTrue(m._gh_wake.is_set())


if __name__ == "__main__":
    unittest.main()
