#!/usr/bin/env python3
"""Tests for ccboard's GitHub feed (github_feed and friends).

Run:  python3 -m unittest discover -s ~/Claude/bin/tests -v

Stdlib unittest, same reasons as test_pipeline_check.py. Unlike those tests,
these DO fake gh: the behaviours under test -- the rollup dedupe, the
superseded-red collapse, the stuck marking, the refuse-to-under-report
preflight -- are all decisions this code makes about data it has already
received, and both dedupe rules exist because a live run showed the raw data
lying. The fixture payloads below are copies of those liars.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "ccboard.py"


def load():
    spec = importlib.util.spec_from_loader(
        "ccboard_under_test",
        importlib.machinery.SourceFileLoader("ccboard_under_test", str(SCRIPT)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cb = load()


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class TestPrCheckState(unittest.TestCase):
    def test_undeduped_rollup_judges_only_the_newest_run(self):
        # The PR #588 shape: a re-run on the same head commit leaves BOTH
        # check runs in the rollup under one name. Face value says red;
        # the newest run per name says green, and it is right.
        rollup = [
            {"name": "Check required PR sections", "conclusion": "FAILURE",
             "completedAt": "2026-07-31T17:34:05Z"},
            {"name": "Check required PR sections", "conclusion": "SUCCESS",
             "completedAt": "2026-07-31T17:50:42Z"},
        ]
        state, red = cb.pr_check_state(rollup)
        self.assertEqual(state, "green")
        self.assertEqual(red, [])

    def test_red_names_the_failing_check(self):
        rollup = [
            {"name": "ci", "conclusion": "SUCCESS", "completedAt": "2026-08-01T10:00:00Z"},
            {"name": "lint", "conclusion": "FAILURE", "completedAt": "2026-08-01T10:00:01Z"},
        ]
        state, red = cb.pr_check_state(rollup)
        self.assertEqual(state, "red")
        self.assertEqual(red, ["lint"])

    def test_cancelled_is_not_red(self):
        # Cancelling a run is usually somebody superseding it on purpose.
        rollup = [{"name": "ci", "conclusion": "CANCELLED",
                   "completedAt": "2026-08-01T10:00:00Z"}]
        state, red = cb.pr_check_state(rollup)
        self.assertEqual(state, "green")
        self.assertEqual(red, [])

    def test_pending_beats_green(self):
        rollup = [
            {"name": "ci", "conclusion": "SUCCESS", "completedAt": "2026-08-01T10:00:00Z"},
            {"name": "deploy", "conclusion": "", "startedAt": "2026-08-01T10:00:05Z"},
        ]
        self.assertEqual(cb.pr_check_state(rollup)[0], "pending")

    def test_status_context_shape(self):
        # StatusContext rows carry context/state, not name/conclusion.
        rollup = [{"context": "codecov", "state": "FAILURE",
                   "createdAt": "2026-08-01T10:00:00Z"}]
        state, red = cb.pr_check_state(rollup)
        self.assertEqual((state, red), ("red", ["codecov"]))

    def test_empty_rollup_is_none_not_green(self):
        self.assertEqual(cb.pr_check_state([]), ("none", []))
        self.assertEqual(cb.pr_check_state(None), ("none", []))


class FakeRepo:
    name = "demo"


class TestRepoEvents(unittest.TestCase):
    """_repo_events with gh_json monkeypatched -- the collapse decisions."""

    def events(self, runs, prs):
        orig = cb.gh_json
        payload = {"run": runs, "pr": prs}
        cb.gh_json = lambda gh, args, cwd: payload[args[0]]
        try:
            return cb._repo_events("gh", FakeRepo())
        finally:
            cb.gh_json = orig

    def test_superseded_failure_is_history(self):
        # The swamplink deploy shape: a failure a later run of the same
        # workflow+branch already fixed kept reporting for hours. gh returns
        # newest first, so the success is seen first and the failure drops.
        now = time.time()
        runs = [
            {"status": "completed", "conclusion": "success", "name": "deploy",
             "headBranch": "main", "createdAt": iso(now - 60), "updatedAt": iso(now - 60)},
            {"status": "completed", "conclusion": "failure", "name": "deploy",
             "headBranch": "main", "createdAt": iso(now - 7200), "updatedAt": iso(now - 7200)},
        ]
        got = self.events(runs, [])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["state"], "success")

    def test_same_workflow_different_branch_both_speak(self):
        now = time.time()
        runs = [
            {"status": "completed", "conclusion": "success", "name": "ci",
             "headBranch": "main", "createdAt": iso(now - 60), "updatedAt": iso(now - 60)},
            {"status": "completed", "conclusion": "failure", "name": "ci",
             "headBranch": "feat/x", "createdAt": iso(now - 120), "updatedAt": iso(now - 120)},
        ]
        states = {e["branch"]: e["state"] for e in self.events(runs, [])}
        self.assertEqual(states, {"main": "success", "feat/x": "failed"})

    def test_live_run_older_than_threshold_is_stuck(self):
        # The first fleet scan had two dependabot graph runs QUEUED for 17
        # hours; ranking them live pinned them to the top forever.
        now = time.time()
        runs = [
            {"status": "queued", "conclusion": None, "name": "graph",
             "headBranch": "main", "createdAt": iso(now - 17 * 3600),
             "updatedAt": iso(now - 17 * 3600)},
            {"status": "in_progress", "conclusion": None, "name": "ci",
             "headBranch": "main", "createdAt": iso(now - 90), "updatedAt": iso(now - 90)},
        ]
        states = {e["workflow"]: e["state"] for e in self.events(runs, [])}
        self.assertEqual(states, {"graph": "stuck", "ci": "in_progress"})

    def test_both_sources_failing_is_none_one_failing_is_not(self):
        self.assertIsNone(self.events(None, None))
        self.assertEqual(self.events(None, []), [])

    def test_pr_event_carries_the_verdict(self):
        prs = [{"number": 7, "title": "T", "headRefName": "b", "isDraft": False,
                "createdAt": "2026-08-01T10:00:00Z", "updatedAt": "2026-08-01T11:00:00Z",
                "reviewDecision": "APPROVED", "url": "u",
                "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS",
                                       "completedAt": "2026-08-01T10:30:00Z"}]}]
        (e,) = self.events([], prs)
        self.assertEqual((e["kind"], e["checks"], e["review"]), ("pr", "green", "APPROVED"))


class TestGithubFeed(unittest.TestCase):
    def test_broken_preflight_warns_instead_of_under_reporting(self):
        # An empty feed and a feed that cannot see are different facts.
        orig = cb.gh_preflight
        cb.gh_preflight = lambda: "token expired"
        try:
            events, warn = cb.github_feed()
        finally:
            cb.gh_preflight = orig
        self.assertEqual(events, [])
        self.assertEqual(warn, "token expired")

    def test_ordering_live_then_red_then_fresh(self):
        now = time.time()
        raw = [
            {"kind": "run", "state": "success", "ts": now - 10, "repo": "a"},
            {"kind": "run", "state": "failed", "ts": now - 9000, "repo": "b"},
            {"kind": "pr", "checks": "red", "ts": now - 8000, "repo": "c"},
            {"kind": "run", "state": "in_progress", "ts": now - 50, "repo": "d"},
            {"kind": "run", "state": "stuck", "ts": now - 61200, "repo": "e"},
            {"kind": "pr", "checks": "green", "ts": now - 5, "repo": "f"},
        ]
        orig_pre, orig_repos, orig_ev = cb.gh_preflight, cb.github_repos, cb._repo_events
        cb.gh_preflight = lambda: ""
        cb.github_repos = lambda: [FakeRepo()]
        cb._repo_events = lambda gh, r: list(raw)
        try:
            events, warn = cb.github_feed()
        finally:
            cb.gh_preflight, cb.github_repos, cb._repo_events = orig_pre, orig_repos, orig_ev
        self.assertEqual(warn, "")
        # Within the red tier the fresher event leads: c (-8000s) beats b (-9000s).
        self.assertEqual([e["repo"] for e in events], ["d", "c", "b", "e", "f", "a"])


class TestEpoch(unittest.TestCase):
    def test_trailing_z(self):
        self.assertAlmostEqual(cb._gh_epoch("1970-01-01T00:01:00Z"), 60.0)

    def test_surprise_is_none_not_a_crash(self):
        self.assertIsNone(cb._gh_epoch("not a date"))
        self.assertIsNone(cb._gh_epoch(""))
        self.assertIsNone(cb._gh_epoch(None))


if __name__ == "__main__":
    unittest.main()
