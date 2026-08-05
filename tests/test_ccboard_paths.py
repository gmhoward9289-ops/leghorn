#!/usr/bin/env python3
"""Tests for path labelling: split_path, project_from_files, tree_path, contested.

These decide what shows in the project column and which tree gets a git probe.
Wrong answers here make every session look like "GitHub" and every contested
pair look clean -- the two bugs that made ccboard exist in the first place.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "ccboard.py"


def load():
    loader = importlib.machinery.SourceFileLoader("ccboard_paths_under_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cb = load()


class SplitPathTest(unittest.TestCase):
    def test_empty_is_dash(self):
        self.assertEqual(cb.split_path(""), ("-", ""))
        self.assertEqual(cb.split_path(None), ("-", ""))

    def test_worktree_keeps_only_the_tree_name(self):
        # A path deep in a worktree must not push the subdirectory into the label.
        d = str(cb.HOME / "GitHub" / ".worktrees" / "leghorn" / "feat-x")
        deep = str(cb.HOME / "GitHub" / ".worktrees" / "leghorn" / "feat-x" / "src" / "a.py")
        self.assertEqual(cb.split_path(d), ("leghorn", "feat-x"))
        self.assertEqual(cb.split_path(deep), ("leghorn", "feat-x"))

    def test_worktree_primary_when_tree_missing(self):
        d = str(cb.HOME / "GitHub" / ".worktrees" / "leghorn")
        self.assertEqual(cb.split_path(d), ("leghorn", "(primary)"))

    def test_github_container_is_not_the_project(self):
        root = str(cb.HOME / "GitHub")
        self.assertEqual(cb.split_path(root), ("(GitHub root)", "unscoped"))
        repo = str(cb.HOME / "GitHub" / "leghorn")
        self.assertEqual(cb.split_path(repo), ("leghorn", "(primary)"))

    def test_home_project_is_first_segment(self):
        d = str(cb.HOME / "Claude" / "scratch")
        self.assertEqual(cb.split_path(d), ("Claude", ""))

    def test_outside_home_uses_basename(self):
        # Platform-native absolute path that cannot be relative_to(HOME).
        if os.name == "nt":
            raw = r"D:\other\toolbox"
        else:
            raw = "/opt/toolbox"
        self.assertEqual(cb.split_path(raw), ("toolbox", ""))


class ProjectFromFilesTest(unittest.TestCase):
    def test_infra_paths_do_not_vote(self):
        # INFRA matches forward-slash markers in the raw string, which is how
        # Claude Code records paths even on Windows.
        paths = [
            str(cb.HOME).replace("\\", "/") + "/.claude/memory.md",
            "/tmp/scratch.txt",
            "/private/tmp/scratch.txt",
            str(cb.HOME).replace("\\", "/") + "/Claude/bin/hook.py",
        ]
        self.assertIsNone(cb.project_from_files(paths))

    def test_majority_project_wins(self):
        a = str(cb.HOME / "GitHub" / "leghorn" / "a.py")
        b = str(cb.HOME / "GitHub" / "leghorn" / "b.py")
        c = str(cb.HOME / "GitHub" / "roost" / "c.py")
        self.assertEqual(
            cb.project_from_files([a, b, c]),
            ("leghorn", "(primary)"),
        )

    def test_empty_is_none(self):
        self.assertIsNone(cb.project_from_files([]))
        self.assertIsNone(cb.project_from_files(None))


class TreePathTest(unittest.TestCase):
    def test_pseudo_projects_have_no_tree(self):
        for project in ("-", "(GitHub root)", "(home)"):
            self.assertIsNone(cb.tree_path(project, "(primary)"))

    def test_primary_points_at_repos_root(self):
        self.assertEqual(cb.tree_path("leghorn", "(primary)"),
                         cb.REPOS_ROOT / "leghorn")

    def test_unscoped_and_blank_point_under_home(self):
        self.assertEqual(cb.tree_path("Claude", ""), cb.HOME / "Claude")
        self.assertEqual(cb.tree_path("Claude", "unscoped"), cb.HOME / "Claude")

    def test_named_worktree(self):
        self.assertEqual(
            cb.tree_path("leghorn", "feat-x"),
            cb.REPOS_ROOT / ".worktrees" / "leghorn" / "feat-x",
        )


class BuildContestedTest(unittest.TestCase):
    """Two live sessions in one real tree must mark contested; sharing ~/GitHub must not."""

    def _session(self, pid, name, sid, cwd):
        return {"pid": pid, "name": name, "sessionId": sid, "cwd": cwd}

    def test_two_sessions_in_one_repo_are_contested(self):
        repo = str(cb.HOME / "GitHub" / "leghorn")
        sessions = [
            self._session(1, "a", "sid-a", repo),
            self._session(2, "b", "sid-b", repo),
        ]
        with mock.patch.object(cb, "gather_git", return_value={}):
            rows = cb.build({}, {}, {}, sessions, use_git=False)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["contested"] for r in rows))

    def test_sharing_github_root_is_not_contested(self):
        root = str(cb.HOME / "GitHub")
        sessions = [
            self._session(1, "a", "sid-a", root),
            self._session(2, "b", "sid-b", root),
        ]
        rows = cb.build({}, {}, {}, sessions, use_git=False)
        self.assertTrue(all(not r["contested"] for r in rows))

    def test_files_modified_outrank_launch_cwd(self):
        # Launch cwd is ~/GitHub for nearly every session; written files are truth.
        launch = str(cb.HOME / "GitHub")
        real = str(cb.HOME / "GitHub" / "leghorn" / "ccboard.py")
        sessions = [self._session(11, "s", "sid-1", launch)]
        telemetry = {11: {"status": "Idle", "files_modified": {real: 1}}}
        rows = cb.build(telemetry, {}, {}, sessions, use_git=False)
        self.assertEqual(rows[0]["project"], "leghorn")
        self.assertEqual(rows[0]["located_by"], "files")


if __name__ == "__main__":
    unittest.main()
