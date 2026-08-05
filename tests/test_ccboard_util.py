#!/usr/bin/env python3
"""Tests for small pure helpers in ccboard: ago, uncommitted."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "ccboard.py"


def load():
    loader = importlib.machinery.SourceFileLoader("ccboard_util_under_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cb = load()


class AgoTest(unittest.TestCase):
    def test_none_is_dash(self):
        self.assertEqual(cb.ago(None), "-")

    def test_seconds_minutes_hours_days(self):
        self.assertEqual(cb.ago(0), "0s")
        self.assertEqual(cb.ago(59), "59s")
        self.assertEqual(cb.ago(60), "1m")
        self.assertEqual(cb.ago(3599), "59m")
        self.assertEqual(cb.ago(3600), "1h")
        self.assertEqual(cb.ago(86399), "23h")
        self.assertEqual(cb.ago(86400), "1d")
        self.assertEqual(cb.ago(86400 * 3), "3d")


class UncommittedTest(unittest.TestCase):
    def test_sums_all_three_sides(self):
        r = {"git": {"staged": 2, "dirty": 3, "untracked": 1}}
        self.assertEqual(cb.uncommitted(r), 6)

    def test_missing_git_is_clean(self):
        self.assertEqual(cb.uncommitted({}), 0)
        self.assertEqual(cb.uncommitted({"git": None}), 0)
        self.assertEqual(cb.uncommitted({"git": {}}), 0)


if __name__ == "__main__":
    unittest.main()
