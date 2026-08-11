#!/usr/bin/env python3
"""leghorn -- full-screen live view of every Claude Code session and its git state.

henhouse answers "what is the state right now" in one table, which is the right
shape for a hook, a pipe or a glance. This is the other thing: a window you leave
open on a second monitor while a dozen sessions work, where the eye catches a
change without reading. Same facts, different job -- so the data layer is
imported from henhouse rather than reimplemented, and this file is only ever a
renderer.

    leghorn                  # three panes, refreshing every 5s
    leghorn -i 2             # ...every 2s
    leghorn --no-git         # skip the per-tree git probes
    leghorn --no-commits     # hide the commit feed
    leghorn --no-github      # hide the GitHub pane (CI runs + open PRs)

Keys: q quit · r refresh now · s cycle sort · f cycle filter · j/k or arrows move
      · enter detail · tab cycle panes · g toggle git · ? help

Named for the Leghorn, the breed that spots everything first and is never quiet
about it. The obvious name, ccdash, is already three other people's Claude Code
dashboards -- one of them the same idea in Go.

Stdlib only, on purpose. This runs unattended from shells with a minimal PATH;
a venv or a pip dependency is one more thing that can be missing at 7am.
curses is stdlib on macOS and Linux; on Windows it comes from the
windows-curses wheel (declared in pyproject.toml, installed automatically
there, not needed anywhere else).

Read-only, like henhouse: it reads session transcripts, and runs gh and read-only
git plumbing. It never writes to a tree, a registry or a session.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import sys
import threading
import time
from pathlib import Path

try:
    import curses
except ImportError:  # pragma: no cover -- exercised only on a bare Windows box
    if sys.platform == "win32":
        raise ImportError(
            "leghorn needs the windows-curses package on Windows: pip install windows-curses"
        ) from None
    raise

__version__ = "0.4.4"


def load_data_layer():
    """Import henhouse, wherever this install put it.

    Three layouts exist. A checkout or a deb/brew libexec keeps henhouse.py
    beside this file; a bin directory may keep it extensionless beside a
    symlink (resolve() follows the link first); a pip/pipx install puts both
    on sys.path as ordinary modules. Sibling file first: when both exist, the
    one next to this file is the one this file was shipped with.
    """
    here = Path(__file__).resolve().parent
    for candidate in (here / "henhouse.py", here / "henhouse"):
        if candidate.is_file():
            loader = importlib.machinery.SourceFileLoader("henhouse", str(candidate))
            spec = importlib.util.spec_from_loader("henhouse", loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            return module
    import henhouse
    return henhouse


cb = load_data_layer()

NAME = "leghorn"
COMMIT_LIMIT = 40
# The commits pane scales with the terminal instead of sitting at a fixed width:
# subjects are the part worth reading, and on a wide display there is room for
# them. Clamped so it never starves the sessions pane or swallows a whole screen.
COMMITS_MIN = 50
COMMITS_MAX = 72
SESSIONS_MIN = 72
# Below this the two-pane split leaves neither pane usable, so commits are dropped.
MIN_SPLIT_W = COMMITS_MIN + SESSIONS_MIN


def commits_width(w, override=None):
    if override:
        return max(20, min(override, w - SESSIONS_MIN))
    return max(COMMITS_MIN, min(COMMITS_MAX, w * 2 // 5))
FRESH = 300  # a commit younger than this is "just happened" and gets highlighted
GITHUB_LIMIT = 40
GITHUB_INTERVAL = 75  # seconds between gh sweeps; the 5s loop never waits on it
GITHUB_MIN_H = 5      # below this the pane says nothing worth its border
GITHUB_MIN_W = 44     # narrower than this and a run line is all glyph, no name

# (name, session interval, gh interval), fastest first. The two clocks scale
# apart on purpose: a session collect is local git and costs milliseconds, but
# a gh sweep is ~3s of network across the fleet against a rate-limited API, so
# even "ultra" holds it to a minute. Anything under that is sweeps overlapping,
# not fresher data.
SPEEDS = (
    ("ultra", 1.0, 60.0),
    ("fast", 5.0, 75.0),
    ("normal", 8.0, 120.0),
    ("slow", 300.0, 21600.0),
)
DEFAULT_SPEED = "fast"


def speed_settings(name):
    for entry in SPEEDS:
        if entry[0] == name:
            return entry
    return speed_settings(DEFAULT_SPEED)

SORTS = ("attention", "context", "dirty", "name", "commit age")
FILTERS = ("all", "contested", "needs attention", "uncommitted", "claimed")

# Colour pair ids. Foreground on the terminal's own background, so leghorn
# inherits whatever theme the terminal already uses.
C_DIM = 1
C_GREEN = 2
C_YELLOW = 3
C_RED = 4
C_CYAN = 5
C_MAGENTA = 6
C_BLUE = 7
C_SEL = 8


def init_colors():
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    for pair, fg in (
        (C_DIM, curses.COLOR_WHITE),
        (C_GREEN, curses.COLOR_GREEN),
        (C_YELLOW, curses.COLOR_YELLOW),
        (C_RED, curses.COLOR_RED),
        (C_CYAN, curses.COLOR_CYAN),
        (C_MAGENTA, curses.COLOR_MAGENTA),
        (C_BLUE, curses.COLOR_BLUE),
    ):
        curses.init_pair(pair, fg, bg)
    curses.init_pair(C_SEL, curses.COLOR_BLACK, curses.COLOR_CYAN)


def cp(pair):
    return curses.color_pair(pair)


class Model:
    """Polls henhouse's data layer on a worker thread; the UI only ever reads.

    GitHub gets its own thread and its own, much slower clock: a gh sweep costs
    ~3s of network across the fleet, and the 5s session/git loop must never
    stand behind it. Both threads honour the same refresh_now().
    """

    def __init__(self, interval, use_git, want_commits, want_github,
                 github_interval=GITHUB_INTERVAL):
        self.interval = interval
        self.use_git = use_git
        self.want_commits = want_commits
        self.want_github = want_github
        self.github_interval = github_interval
        self.lock = threading.Lock()
        self.rows = []
        self.commits = []
        self.gh_events = []
        self.gh_warn = ""
        self.gh_updated = 0.0
        self.updated = 0.0
        self.warn = None
        self.error = ""
        self.loading = True
        self.busy = False
        self._wake = threading.Event()
        self._gh_wake = threading.Event()
        self._stop = threading.Event()

    def refresh_now(self):
        self._wake.set()
        self._gh_wake.set()

    def set_speed(self, interval, github_interval):
        """Retune both clocks. Waking the session thread is what makes the new
        interval take effect: it re-reads only after its current wait returns,
        and a wait started under 'slow' is six hours long.

        The gh thread is woken only when its wait is now longer than the new
        interval -- otherwise cycling p four times back to 'fast' would fire
        four network sweeps in a row, which is exactly the overlap the
        sixty-second floor in SPEEDS exists to prevent.
        """
        with self.lock:
            self.interval = interval
            sweep_sooner = github_interval < self.github_interval
            self.github_interval = github_interval
        self._wake.set()
        if sweep_sooner:
            self._gh_wake.set()

    def stop(self):
        self._stop.set()
        # Drain the sweep pools too: their worker threads are non-daemon, and
        # the interpreter joins them at exit -- without this a quit pressed
        # mid-sweep holds the shell prompt hostage for the rest of the sweep.
        cb.CANCEL.set()
        self._wake.set()
        self._gh_wake.set()

    def run(self):
        while not self._stop.is_set():
            self._collect()
            with self.lock:
                wait = self.interval
            self._wake.wait(wait)
            self._wake.clear()

    def run_github(self):
        while not self._stop.is_set():
            self._collect_github()
            with self.lock:
                wait = self.github_interval
            self._gh_wake.wait(wait)
            self._gh_wake.clear()

    def _collect_github(self):
        try:
            events, warn = cb.github_feed(GITHUB_LIMIT)
        except Exception as exc:
            events, warn = [], "%s: %s" % (type(exc).__name__, exc)
        with self.lock:
            self.gh_events, self.gh_warn = events, warn
            self.gh_updated = time.time()

    def _collect(self):
        with self.lock:
            self.busy = True
        rows, commits, warn, error = [], [], None, ""
        try:
            sessions = cb.load_sessions()
            telemetry, warn = cb.load_transcripts(sessions)
            claims, occupancy = cb.load_registry()
            rows = sorted(
                cb.build(telemetry, claims, occupancy, sessions,
                         use_git=self.use_git),
                key=cb.sort_key,
            )
            if self.want_commits:
                commits = cb.commit_feed(COMMIT_LIMIT)
        except Exception as exc:  # a dashboard that dies on one bad repo is useless
            error = "%s: %s" % (type(exc).__name__, exc)
        with self.lock:
            if not error:
                self.rows, self.commits, self.warn = rows, commits, warn
                self.updated = time.time()
            self.error = error
            self.loading = False
            self.busy = False

    def snapshot(self):
        with self.lock:
            return (list(self.rows), list(self.commits), self.updated,
                    self.warn, self.error, self.loading, self.busy)

    def snapshot_github(self):
        with self.lock:
            return list(self.gh_events), self.gh_warn, self.gh_updated


def status_color(status):
    s = str(status).lower().replace(" ", "")
    if s in ("error", "failed"):
        return C_RED
    if s in ("needsinput", "waiting"):
        return C_YELLOW
    if s == "processing":
        return C_GREEN
    return C_DIM


def ctx_color(pct):
    if not isinstance(pct, (int, float)):
        return C_DIM
    if pct > 100:
        return C_RED
    if pct >= 70:
        return C_YELLOW
    return C_DIM


def matches(row, filt):
    if filt == "contested":
        return row["contested"]
    if filt == "needs attention":
        return str(row["status"]).lower().replace(" ", "") in cb.ATTENTION
    if filt == "uncommitted":
        return bool(cb.uncommitted(row))
    if filt == "claimed":
        return bool(row["task"])
    return True


def apply_sort(rows, mode):
    if mode == "context":
        key = lambda r: -(r["context_pct"] if isinstance(r["context_pct"], (int, float)) else -1)
    elif mode == "dirty":
        key = lambda r: (-cb.uncommitted(r), -((r.get("git") or {}).get("behind") or 0))
    elif mode == "name":
        key = lambda r: r["name"]
    elif mode == "commit age":
        # Never-committed trees sort last rather than pretending to be brand new.
        key = lambda r: -((r.get("git") or {}).get("last_age") or -1)
    else:
        key = cb.sort_key
    return sorted(rows, key=key)


class Pane:
    """A bordered box that clips everything written into it."""

    def __init__(self, win, y, x, h, w, title, focused=False):
        self.win, self.y, self.x, self.h, self.w = win, y, x, h, w
        self.title, self.focused = title, focused

    def frame(self):
        attr = cp(C_CYAN) | (curses.A_BOLD if self.focused else curses.A_DIM)
        try:
            self.win.attron(attr)
            self.win.addstr(self.y, self.x, "╭" + "─" * (self.w - 2) + "╮")
            for row in range(1, self.h - 1):
                self.win.addstr(self.y + row, self.x, "│")
                self.win.addstr(self.y + row, self.x + self.w - 1, "│")
            self.win.addstr(self.y + self.h - 1, self.x,
                            "╰" + "─" * (self.w - 2) + "╯")
            self.win.attroff(attr)
            label = " %s " % self.title
            self.win.addstr(self.y, self.x + 2, label[: self.w - 4],
                            cp(C_CYAN) | curses.A_BOLD)
        except curses.error:
            pass

    def put(self, line, col, text, attr=0):
        """Write inside the border, clipped. Out-of-box writes are dropped."""
        if not (0 <= line < self.h - 2):
            return 0
        avail = self.w - 2 - col
        if avail <= 0:
            return 0
        text = text[:avail]
        try:
            self.win.addstr(self.y + 1 + line, self.x + 1 + col, text, attr)
        except curses.error:
            pass
        return len(text)


GAP = 2
# Screen order of the session columns, and the width each wants.
COL_ORDER = ("dot", "name", "project", "branch", "status", "ctx", "git", "age", "task")
COL_WIDTH = {"dot": 1, "name": 11, "project": 19, "branch": 14,
             "status": 11, "ctx": 5, "git": 9, "age": 4, "task": 10}


def elide(text, width):
    """Truncate to width, keeping both ends of a repo/worktree pair.

    Plain truncation turns every worktree of one repo into the same string --
    counting-chicken-wings and counting-chicken-wings/chore-deploy-env both
    render as "counting-chicken", which is exactly the distinction this column
    exists to make.
    """
    if len(text) <= width:
        return text
    if "/" in text and width >= 9:
        head = (width - 1) // 2
        return text[:head] + "…" + text[-(width - 1 - head):]
    return text[:width]
# Dropped last-first when the terminal is too narrow. "Which repo" outranks the
# task text: at 80 columns the task is a stub anyway, and a row you cannot place
# in a repo is not worth the width it costs.
COL_CORE = ("dot", "name", "project", "status", "ctx", "git")
# Added only once the task column has room to say something. Branch is here
# rather than in the core because project already carries the worktree name,
# and a ccwork worktree and its branch are nearly always the same string.
COL_OPTIONAL = ("branch", "age")
TASK_COMFORT = 24


def session_layout(inner_w, use_git):
    """key -> width for one session row, fitted to the space actually available."""
    chosen, used = {}, 0

    def take(key, width):
        nonlocal used
        cost = width + (GAP if chosen else 0)
        if used + cost > inner_w:
            return False
        chosen[key] = width
        used += cost
        return True

    for key in COL_CORE:
        if key == "git" and not use_git:
            continue
        take(key, COL_WIDTH[key])
    # Claim a readable task column before the optional ones bid for the same
    # space: a 12-character claim is noise, and branch is the cheaper thing to lose.
    take("task", TASK_COMFORT) or take("task", COL_WIDTH["task"])
    for key in COL_OPTIONAL:
        take(key, COL_WIDTH[key])
    # Leftovers go to the task -- the only column never complete at any width.
    if "task" in chosen:
        chosen["task"] += inner_w - used
    return chosen


def session_cells(r, layout, use_git):
    """key -> (text, colour) for the columns this layout kept."""
    g = r.get("git") or {}
    loc = r["project"]
    if r["tree"] and r["tree"] not in ("(primary)", ""):
        loc += "/" + r["tree"]

    ctx = r["context_pct"]
    ctx_text = "%.0f%%%s" % (ctx, "?" if ctx > 100 else "") if isinstance(
        ctx, (int, float)) else "-"

    if not use_git or not g:
        git_text, git_color = "", C_DIM
    else:
        dirt = cb.uncommitted(r)
        bits = []
        if dirt:
            bits.append("~%d" % dirt)
        if g["ahead"]:
            bits.append("↑%d" % g["ahead"])
        if g["behind"]:
            bits.append("↓%d" % g["behind"])
        git_text = " ".join(bits) if bits else "clean"
        git_color = C_YELLOW if dirt else (C_MAGENTA if g["behind"] else C_DIM)

    return {
        "dot": ("◉" if r["contested"] else "●", status_color(r["status"])),
        "name": (r["name"], C_CYAN),
        "project": (loc, C_BLUE),
        "branch": (r["branch"], C_DIM),
        "status": (str(r["status"]), status_color(r["status"])),
        "ctx": (ctx_text, ctx_color(ctx)),
        "git": (git_text, git_color),
        "age": (cb.ago(g["last_age"]) if g else "", C_DIM),
        "task": (r["task"], C_DIM),
    }


def draw_sessions(pane, rows, sel, scroll, use_git):
    """One line per session, roost-style: it has to survive an 80-column window."""
    body = pane.h - 2
    inner = pane.w - 2
    layout = session_layout(inner, use_git)

    for i in range(scroll, len(rows)):
        line = i - scroll
        if line >= body:
            break
        r = rows[i]
        selected = i == sel
        base = cp(C_SEL) if selected else 0
        if selected:
            pane.put(line, 0, " " * inner, base)

        cells = session_cells(r, layout, use_git)
        col = 0
        for key in COL_ORDER:
            if key not in layout:
                continue
            width = layout[key]
            text, color = cells[key]
            text = elide(text, width) if key in ("project", "branch") else text[:width]
            attr = base if selected else cp(color)
            if key in ("name", "dot"):
                attr |= curses.A_BOLD
            elif key in ("branch", "task", "age"):
                attr |= curses.A_DIM
            pane.put(line, col, text, attr)
            col += width + GAP


def draw_commits(pane, commits, sel, scroll, focused, compact=False):
    """Two lines per commit in the side pane; one line when stacked underneath."""
    body = pane.h - 2
    inner = pane.w - 2
    if not commits:
        pane.put(0, 1, "no commits found", cp(C_DIM) | curses.A_DIM)
        return
    now = time.time()
    step = 1 if compact else 2
    for i in range(scroll, len(commits)):
        top = (i - scroll) * step
        if top + step > body:
            break
        c = commits[i]
        selected = focused and i == sel
        base = cp(C_SEL) if selected else 0
        if selected:
            for line in range(step):
                pane.put(top + line, 0, " " * inner, base)
        age = now - c["ts"]
        age_attr = base if selected else (
            cp(C_GREEN) | curses.A_BOLD if age < FRESH else cp(C_DIM) | curses.A_DIM)
        col = pane.put(top, 1, cb.ago(age).rjust(4) + "  ", age_attr)
        ref = c["refs"].split(", ")[0] if c["refs"] else ""
        if compact:
            # One line, so everything competes with the subject -- which is the
            # only part that says what actually happened. Repo gets a fixed
            # column, and the branch yields entirely unless the window is wide.
            col += pane.put(top, col, c["repo"][:16].ljust(min(16, inner - col)),
                            base if selected else cp(C_BLUE))
            if ref and inner - col > 60:
                col += pane.put(top, col + 2, ref[:14],
                                base if selected else cp(C_DIM) | curses.A_DIM) + 2
            pane.put(top, col + 2, c["subject"],
                     base if selected else cp(C_DIM) | curses.A_DIM)
            continue
        col += pane.put(top, col, c["repo"], base if selected else cp(C_BLUE))
        if ref:
            pane.put(top, col, "  " + ref, base if selected else cp(C_DIM) | curses.A_DIM)
        pane.put(top + 1, 7, c["subject"],
                 base if selected else cp(C_DIM) | curses.A_DIM)


RUN_GLYPH = {"in_progress": ("●", C_GREEN), "queued": ("○", C_GREEN),
             "failed": ("✗", C_RED), "stuck": ("◍", C_YELLOW),
             "success": ("✓", C_DIM)}
CHECKS_GLYPH = {"red": ("✗", C_RED), "pending": ("○", C_YELLOW),
                "green": ("✓", C_GREEN), "none": ("·", C_DIM)}


def github_cells(e):
    """(glyph, glyph_color, text, text_color) for one feed event."""
    if e["kind"] == "run":
        glyph, color = RUN_GLYPH.get(e["state"], ("·", C_DIM))
        text = "%s (%s)" % (e["workflow"], e["branch"])
        text_color = color if e["state"] in ("failed", "stuck") else C_DIM
        return glyph, color, text, text_color
    glyph, color = CHECKS_GLYPH.get(e["checks"], ("·", C_DIM))
    review = (e.get("review") or "").lower()
    bits = ["#%s %s" % (e["number"], e["title"])]
    if e.get("draft"):
        bits.append("[draft]")
    if e.get("red"):
        bits.append("← " + ", ".join(e["red"][:2]))
    elif review == "approved":
        bits.append("APPROVED")
    text_color = C_RED if e["checks"] == "red" else (
        C_GREEN if review == "approved" else C_DIM)
    return glyph, color, "  ".join(bits), text_color


def draw_github(pane, events, warn, sel, scroll, focused):
    """One line per CI run or open PR, problems pinned at the top."""
    inner = pane.w - 2
    if warn:
        pane.put(0, 1, "cannot see github: %s" % warn[: inner - 20],
                 cp(C_YELLOW) | curses.A_DIM)
        return
    if not events:
        pane.put(0, 1, "no CI runs or open PRs", cp(C_DIM) | curses.A_DIM)
        return
    now = time.time()
    body = pane.h - 2
    for i in range(scroll, len(events)):
        line = i - scroll
        if line >= body:
            break
        e = events[i]
        selected = focused and i == sel
        base = cp(C_SEL) if selected else 0
        if selected:
            pane.put(line, 0, " " * inner, base)
        age = (now - e["ts"]) if e.get("ts") else None
        age_attr = base if selected else (
            cp(C_GREEN) | curses.A_BOLD if age is not None and age < FRESH
            else cp(C_DIM) | curses.A_DIM)
        col = pane.put(line, 0, cb.ago(age).rjust(4), age_attr) + 1
        glyph, gcolor, text, tcolor = github_cells(e)
        col += pane.put(line, col, glyph, base if selected else cp(gcolor) | curses.A_BOLD) + 1
        col += pane.put(line, col, elide(e["repo"], 17).ljust(min(17, max(0, inner - col))),
                        base if selected else cp(C_BLUE)) + 1
        pane.put(line, col, text, base if selected else cp(tcolor))
    hidden = len(events) - (scroll + body)
    if hidden > 0:
        pane.put(body - 1, 0, " " * inner)
        pane.put(body - 1, 1, "… %d more below" % hidden, cp(C_YELLOW) | curses.A_DIM)


def github_detail(e):
    common = [("repo", e["repo"]), ("branch", e.get("branch") or "-")]
    if e["kind"] == "run":
        lines = [("workflow", e["workflow"]), ("state", e["state"])] + common
        if e.get("title"):
            lines.append(("commit", e["title"]))
    else:
        lines = [("pull request", "#%s" % e["number"]), ("title", e["title"])] + common
        lines.append(("checks", e["checks"] +
                      (": " + ", ".join(e["red"]) if e.get("red") else "")))
        lines.append(("review", (e.get("review") or "none").lower().replace("_", " ")))
        if e.get("draft"):
            lines.append(("draft", "yes"))
        if e.get("created_ts"):
            lines.append(("open for", cb.ago(time.time() - e["created_ts"])))
    lines.append(("updated", cb.ago(time.time() - e["ts"]) + " ago" if e.get("ts") else "-"))
    lines.append(("url", e.get("url") or "-"))
    return lines


def commit_detail(c):
    return [
        ("repo", c["repo"]),
        ("commit", c["sha"]),
        ("author", c["author"]),
        ("branch", c["refs"] or "-"),
        ("subject", c["subject"]),
        ("age", cb.ago(time.time() - c["ts"]) + " ago"),
    ]


def draw_header(win, w, rows, total, updated, busy, sort_mode, filt, gh_events=(),
                speed=DEFAULT_SPEED):
    contested = sum(1 for r in rows if r["contested"])
    dirty = len({r["git_dir"] for r in rows if cb.uncommitted(r)})
    behind = len({r["git_dir"] for r in rows if (r.get("git") or {}).get("behind")})
    ci_red = sum(1 for e in gh_events
                 if (e["kind"] == "run" and e.get("state") == "failed")
                 or (e["kind"] == "pr" and e.get("checks") == "red"))
    ci_live = sum(1 for e in gh_events
                  if e["kind"] == "run" and e.get("state") in ("in_progress", "queued"))

    try:
        win.addstr(0, 1, NAME, cp(C_CYAN) | curses.A_BOLD)
        col = len(NAME) + 3
        stats = [("%d sessions" % total, C_DIM)]
        if len(rows) != total:
            stats.append(("%d shown" % len(rows), C_CYAN))
        if contested:
            stats.append(("%d shared" % contested, C_MAGENTA))
        if dirty:
            stats.append(("%d uncommitted" % dirty, C_YELLOW))
        if behind:
            stats.append(("%d behind" % behind, C_MAGENTA))
        if ci_red:
            stats.append(("%d ci red" % ci_red, C_RED))
        if ci_live:
            stats.append(("%d ci running" % ci_live, C_GREEN))
        # Stop before the last column rather than at it: addstr writing into the
        # final cell wraps onto row 1, which is the SESSIONS pane's top border,
        # and the stray character sticks there until the header shrinks again.
        for i, (text, color) in enumerate(stats):
            need = (2 if i else 0) + len(text)
            if col + need >= w - 1:
                break
            if i:
                win.addstr(0, col, "· ", cp(C_DIM) | curses.A_DIM)
                col += 2
            win.addstr(0, col, text, cp(color))
            col += len(text) + 1

        clock = time.strftime("%H:%M:%S", time.localtime(updated)) if updated else "--:--:--"
        clock += " ●" if busy else "  "
        # Narrow terminals lose the mode labels before they lose the clock --
        # "when did this last update" is the one thing a wall display must keep.
        for right in ("sort:%s  filter:%s  speed:%s  %s"
                      % (sort_mode, filt, speed, clock),
                      "sort:%s  filter:%s  %s" % (sort_mode, filt, clock),
                      clock):
            if w - len(right) - 2 > col:
                win.addstr(0, w - len(right) - 1, right, cp(C_DIM) | curses.A_DIM)
                break
    except curses.error:
        pass


def draw_footer(win, h, w, message, updated=0.0, gh_updated=0.0):
    keys = ("q quit  r refresh  s sort  f filter  p speed  c commits  g git  "
            "tab pane  enter detail  ? help")
    try:
        left = (message or keys)[: w - 2]
        win.addstr(h - 1, 1, left,
                   cp(C_YELLOW) if message else cp(C_DIM) | curses.A_DIM)
        # Data ages, bottom right: sorting and filtering are instant and local,
        # so the only honest question is how old the data itself is.
        ages = []
        if updated:
            ages.append("updated %s ago" % cb.ago(time.time() - updated))
        if gh_updated:
            ages.append("gh %s" % cb.ago(time.time() - gh_updated))
        right = " · ".join(ages)
        if right and w - len(right) - 2 > len(left) + 2:
            win.addstr(h - 1, w - len(right) - 2, right, cp(C_DIM) | curses.A_DIM)
    except curses.error:
        pass


def detail_lines(r):
    g = r.get("git") or {}
    lines = [
        ("session", "%s  (pid %d)" % (r["name"], r["pid"])),
        ("status", "%s%s" % (r["status"],
                             "  · %d subagent(s)" % r["subagents"] if r["subagents"] else "")),
        ("context", "%s" % (("%.0f%%" % r["context_pct"]) if isinstance(
            r["context_pct"], (int, float)) else "-")),
        ("cost", ("$%.2f" % r["cost_usd"]) if isinstance(r["cost_usd"], (int, float)) else "-"),
        ("project", "%s / %s" % (r["project"], r["tree"] or "-")),
        ("branch", r["branch"] or "-"),
        ("directory", r["git_dir"] or r["dir"] or "-"),
        ("located by", r["located_by"]),
    ]
    if g:
        lines += [
            ("uncommitted", "%d staged, %d modified, %d untracked"
             % (g["staged"], g["dirty"], g["untracked"])),
            ("vs %s" % (g["base"] or "base"),
             "-" if g["ahead"] is None else "%d ahead, %d behind" % (g["ahead"], g["behind"])),
            ("last commit", "%s  (%s)" % (g["last"] or "-", cb.ago(g["last_age"]))),
        ]
    else:
        lines.append(("git", "no repository at this path"))
    lines.append(("claim", r["task"] or "(none)"))
    return lines


# What each screen MEANS comes first; the keys are the easy part. A keybinding
# list is not help -- the question a first-time viewer actually has is "what is
# this symbol telling me", and before 2026-08-01 nothing on screen answered it.
HELP = [
    ("SESSIONS", "every live session, joined to its worktree and real git state"),
    ("◉", "contested -- another live session is active in the same tree"),
    ("ctx", "context burned; yellow from 70%, red past 100% (? = estimate)"),
    ("~2 ↑1 ↓3", "uncommitted files · commits ahead · commits behind base"),
    ("GITHUB", "CI runs and open PRs across every clone, via gh"),
    ("● ○ ✗ ◍", "run: running · queued · failed · stuck in queue >2h"),
    ("✗ on a PR", "red checks -- failing names shown after the arrow"),
    ("pinned", "live and red stay at the top; they must not scroll away"),
    ("COMMITS", "every commit on every branch, newest first; green = just landed"),
    ("filters", ", ".join(FILTERS[1:])),
    ("sorts", ", ".join(SORTS)),
    ("", ""),
    ("q / esc", "quit"),
    ("r", "refresh everything now, including the gh sweep"),
    ("s / f", "cycle sort / filter"),
    ("p", "cycle speed: " + " · ".join(
        "%s %s/%s" % (n, cb.ago(i), cb.ago(g)) for n, i, g in SPEEDS)),
    ("c", "toggle the commit feed (off = sessions and github side by side)"),
    ("g", "toggle the per-tree git probes"),
    ("tab", "cycle focus: sessions, github, commits"),
    ("j / k, arrows", "move selection · 0 / G top / bottom"),
    ("enter", "detail for the selected row"),
]


def overlay(win, h, w, title, lines, footer):
    """A centred modal. Lines are (label, value) pairs."""
    label_w = max((len(a) for a, _ in lines), default=0)
    inner = max(len(title) + 4,
                max((label_w + len(b) + 3 for _, b in lines), default=20),
                len(footer))
    box_w = min(w - 4, max(40, inner + 4))
    box_h = min(h - 2, len(lines) + 4)
    top, left = (h - box_h) // 2, (w - box_w) // 2
    pane = Pane(win, top, left, box_h, box_w, title, focused=True)
    for row in range(box_h - 2):
        pane.put(row, 0, " " * (box_w - 2))
    pane.frame()
    for i, (label, value) in enumerate(lines):
        if i >= box_h - 3:
            break
        pane.put(i + 1, 1, label.rjust(label_w), cp(C_CYAN))
        pane.put(i + 1, label_w + 3, str(value))
    pane.put(box_h - 3, 1, footer, cp(C_DIM) | curses.A_DIM)


def clamp_scroll(sel, scroll, visible_rows):
    """Keep the selected row fully inside the pane."""
    if visible_rows <= 0:
        return 0
    if sel < scroll:
        return sel
    if sel >= scroll + visible_rows:
        return sel - visible_rows + 1
    return scroll


def loop(stdscr, model, args):
    try:
        _loop(stdscr, model, args)
    finally:
        # curses.wrapper's endwin does NOT undo curs_set(0) everywhere:
        # PDCurses (windows-curses) sets console cursor visibility directly
        # and leaves it as-is, so without this the prompt comes back with an
        # invisible cursor on Windows. Guarded because a quit triggered by a
        # dying terminal can make curs_set fail too.
        try:
            curses.curs_set(1)
        except curses.error:
            pass


def _loop(stdscr, model, args):
    try:
        curses.curs_set(0)
    except curses.error:
        pass  # some terminals cannot hide the cursor; run visible
    init_colors()
    stdscr.timeout(250)

    sel = commit_sel = gh_sel = scroll = commit_scroll = gh_scroll = 0
    focus = "sessions"
    sort_mode, filt = SORTS[0], FILTERS[0]
    modal = None  # None | "help" | "detail" | "ghdetail" | "commitdetail"
    message = ""
    want_commits = args.commits
    # An explicit -i or --github-interval means the running clocks are not the
    # preset's, so naming one in the header would be a lie -- and this chrome
    # exists to tell the truth about refresh state. Pressing p adopts a real
    # preset and the label becomes honest again.
    speed = "custom" if (args.interval is not None
                         or args.github_interval is not None) else args.speed

    while True:
        rows_all, commits, updated, warn, error, loading, busy = model.snapshot()
        gh_events, gh_warn, _gh_updated = model.snapshot_github()
        rows = apply_sort([r for r in rows_all if matches(r, filt)], sort_mode)

        h, w = stdscr.getmaxyx()
        stdscr.erase()

        # Three layouts, by how much room there is. Commits move below the
        # sessions rather than disappearing: a narrow window is the common case,
        # not a degraded one, and the feed is half the reason to leave this open.
        body_h = max(3, h - 2)
        stacked = False
        if not want_commits:
            show_commits, commits_w = False, 0
        elif w >= MIN_SPLIT_W:
            show_commits, commits_w = True, commits_width(w, args.commits_width)
        elif body_h >= 12:
            show_commits, commits_w, stacked = True, 0, True
        else:
            show_commits, commits_w = False, 0

        commits_h = min(9, body_h // 3 + 2) if stacked else 0
        left_h = body_h - commits_h
        sessions_w = w - commits_w - (1 if commits_w else 0)

        # The GitHub pane fills what used to be dead space: SESSIONS only ever
        # needs a row per live session, and the rest of the left column sat
        # blank. Sessions get what they need up to two thirds of the column;
        # a warning still earns the pane, because "cannot see github" and
        # "nothing is happening" must not render identically.
        have_github = args.github and (gh_events or gh_warn)

        # With commits off there is a whole column free, so the two remaining
        # panes go side by side rather than stacking and leaving the right half
        # blank. Each then gets full height, which is the point: both are lists
        # that were previously fighting over one column's worth of rows.
        # Two columns need width for both AND enough height for the github pane
        # to say something; a one-row pane under a border is the "cannot see
        # github" ambiguity the stacked path already refuses to create.
        side_by_side = (have_github and not show_commits
                        and w >= SESSIONS_MIN + GITHUB_MIN_W + 1
                        and left_h >= GITHUB_MIN_H)
        if side_by_side:
            # Halve it, but never below what the sessions columns need nor so
            # wide that github is left a stub. The gate above guarantees these
            # two bounds cannot cross.
            sessions_w = min(max(w // 2, SESSIONS_MIN), w - GITHUB_MIN_W - 1)
            show_github, sessions_h, github_h = True, left_h, left_h
        else:
            show_github = have_github and left_h >= 5 + GITHUB_MIN_H
            if show_github:
                need = len(rows) + 3 if rows else 4
                sessions_h = max(5, min(need, left_h - GITHUB_MIN_H, left_h * 2 // 3))
                github_h = left_h - sessions_h
            else:
                sessions_h, github_h = left_h, 0

        sessions = Pane(stdscr, 1, 0, sessions_h, max(20, sessions_w),
                        "SESSIONS", focus == "sessions")
        sessions.frame()

        sel = max(0, min(sel, len(rows) - 1)) if rows else 0
        visible = max(1, sessions.h - 2)
        scroll = clamp_scroll(sel, scroll, visible)

        if loading:
            sessions.put(0, 1, "collecting...", cp(C_DIM) | curses.A_DIM)
        elif not rows:
            sessions.put(0, 1, "no sessions match filter '%s'" % filt,
                         cp(C_DIM) | curses.A_DIM)
        else:
            draw_sessions(sessions, rows, sel, scroll, model.use_git)
            # Silent truncation reads exactly like a screen that is up to date.
            hidden = len(rows) - (scroll + visible)
            if hidden > 0:
                sessions.put(visible - 1, 0, " " * (sessions.w - 2))
                sessions.put(visible - 1, 1, "… %d more below" % hidden,
                             cp(C_YELLOW) | curses.A_DIM)

        if show_github:
            if side_by_side:
                gh_pane = Pane(stdscr, 1, sessions.w + 1, github_h,
                               max(20, w - sessions.w - 1),
                               "GITHUB", focus == "github")
            else:
                gh_pane = Pane(stdscr, 1 + sessions_h, 0, github_h, sessions.w,
                               "GITHUB", focus == "github")
            gh_pane.frame()
            gh_visible = max(1, gh_pane.h - 2)
            gh_sel = max(0, min(gh_sel, len(gh_events) - 1)) if gh_events else 0
            gh_scroll = clamp_scroll(gh_sel, gh_scroll, gh_visible)
            draw_github(gh_pane, gh_events, gh_warn, gh_sel, gh_scroll,
                        focus == "github")
        elif focus == "github":
            focus = "sessions"

        # Clamp outside the draw block: a resize can hide the pane while focus
        # and commit_sel are still pointing into it, and the detail overlay
        # indexes commits directly. A stale index plus a shrunken feed is an
        # IndexError that takes the whole dashboard down.
        commit_sel = max(0, min(commit_sel, len(commits) - 1)) if commits else 0

        if show_commits:
            if stacked:
                pane = Pane(stdscr, 1 + left_h, 0, commits_h, sessions.w,
                            "COMMITS", focus == "commits")
            else:
                pane = Pane(stdscr, 1, sessions.w + 1, body_h, commits_w,
                            "COMMITS", focus == "commits")
            pane.frame()
            cstep = 1 if stacked else 2
            cvisible = max(1, (pane.h - 2) // cstep)
            commit_scroll = clamp_scroll(commit_sel, commit_scroll, cvisible)
            draw_commits(pane, commits, commit_sel, commit_scroll,
                         focus == "commits", compact=stacked)
        elif focus == "commits":
            # Same guard the github pane has: never leave focus on a pane the
            # user cannot see, or j/k/enter drive an invisible list.
            focus = "sessions"

        draw_header(stdscr, w, rows, len(rows_all), updated, busy, sort_mode, filt,
                    gh_events, speed)
        note = message or error or (
            "%s -- status and context unavailable" % warn if warn else "")
        draw_footer(stdscr, h, w, note, updated, _gh_updated)

        if modal == "help":
            overlay(stdscr, h, w, "HELP", HELP, "any key to close")
        elif modal == "detail" and rows:
            overlay(stdscr, h, w, "SESSION", detail_lines(rows[sel]), "any key to close")
        elif modal == "ghdetail" and gh_events:
            overlay(stdscr, h, w, "GITHUB", github_detail(gh_events[gh_sel]),
                    "any key to close")
        elif modal == "commitdetail" and commits:
            overlay(stdscr, h, w, "COMMIT", commit_detail(commits[commit_sel]),
                    "any key to close")

        stdscr.refresh()

        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return
        if key == -1:
            continue
        message = ""

        if modal:
            modal = None
            continue

        if key in (ord("q"), 27):
            return
        if key == curses.KEY_RESIZE:
            # Python's curses does not resize its notion of the screen on
            # SIGWINCH by itself, so on some ncurses builds getmaxyx() keeps
            # reporting the old size and the layout never reflows. Absent
            # before 3.5 and on some platforms, hence the guard.
            try:
                curses.update_lines_cols()
            except (AttributeError, curses.error):
                pass
            continue
        if key == ord("?"):
            modal = "help"
        elif key == ord("r"):
            model.refresh_now()
            message = "refreshing..."
        elif key == ord("s"):
            sort_mode = SORTS[(SORTS.index(sort_mode) + 1) % len(SORTS)]
            sel = scroll = 0
            message = "sort: %s (applied, no refresh needed)" % sort_mode
        elif key == ord("f"):
            filt = FILTERS[(FILTERS.index(filt) + 1) % len(FILTERS)]
            sel = scroll = 0
            message = "filter: %s (applied, no refresh needed)" % filt
        elif key in (ord("G"), curses.KEY_END):
            if focus == "sessions":
                sel = max(0, len(rows) - 1)
            elif focus == "github":
                gh_sel = max(0, len(gh_events) - 1)
            else:
                commit_sel = max(0, len(commits) - 1)
        elif key in (ord("0"), curses.KEY_HOME):
            sel = commit_sel = gh_sel = 0
        elif key == ord("g"):
            model.use_git = not model.use_git
            model.refresh_now()
            message = "git probes %s" % ("on" if model.use_git else "off")
        elif key == ord("p"):
            names = [s[0] for s in SPEEDS]
            # "custom" is not in the ladder, so p steps onto it at the top
            # rather than raising.
            here = names.index(speed) + 1 if speed in names else 0
            speed = names[here % len(names)]
            _, iv, ghiv = speed_settings(speed)
            model.set_speed(iv, ghiv)
            message = "speed: %s (%s sessions, %s github)" % (
                speed, cb.ago(iv), cb.ago(ghiv))
        elif key == ord("c"):
            want_commits = not want_commits
            model.want_commits = want_commits
            if want_commits:
                model.refresh_now()
            elif focus == "commits":
                focus = "sessions"
            message = "commits %s" % ("on" if want_commits else "off")
        elif key == ord("\t"):
            order = ["sessions"] + (["github"] if show_github else []) \
                + (["commits"] if show_commits else [])
            focus = order[(order.index(focus) + 1) % len(order)]
        elif key in (curses.KEY_ENTER, 10, 13):
            if focus == "sessions" and rows:
                modal = "detail"
            elif focus == "github" and gh_events:
                modal = "ghdetail"
            elif focus == "commits" and commits:
                modal = "commitdetail"
        elif key in (curses.KEY_DOWN, ord("j")):
            if focus == "sessions":
                sel = min(sel + 1, max(0, len(rows) - 1))
            elif focus == "github":
                gh_sel = min(gh_sel + 1, max(0, len(gh_events) - 1))
            else:
                commit_sel = min(commit_sel + 1, max(0, len(commits) - 1))
        elif key in (curses.KEY_UP, ord("k")):
            if focus == "sessions":
                sel = max(sel - 1, 0)
            elif focus == "github":
                gh_sel = max(gh_sel - 1, 0)
            else:
                commit_sel = max(commit_sel - 1, 0)
        elif key in (curses.KEY_NPAGE, 6):
            if focus == "sessions":
                sel = min(sel + visible, max(0, len(rows) - 1))
            elif focus == "github":
                gh_sel = min(gh_sel + 5, max(0, len(gh_events) - 1))
            else:
                commit_sel = min(commit_sel + 5, max(0, len(commits) - 1))
        elif key in (curses.KEY_PPAGE, 2):
            if focus == "sessions":
                sel = max(sel - visible, 0)
            elif focus == "github":
                gh_sel = max(gh_sel - 5, 0)
            else:
                commit_sel = max(commit_sel - 5, 0)


def main():
    ap = argparse.ArgumentParser(
        description="Full-screen live view of Claude Code sessions and git state.")
    ap.add_argument("-i", "--interval", type=float, default=None, metavar="SECS",
                    help="seconds between refreshes (overrides --speed)")
    ap.add_argument("--speed", choices=[s[0] for s in SPEEDS], default=DEFAULT_SPEED,
                    help="refresh pace: " + ", ".join(
                        "%s (%.0fs/%.0fs)" % (n, i, g) for n, i, g in SPEEDS)
                         + " (default %s); cycle with p" % DEFAULT_SPEED)
    ap.add_argument("--no-git", dest="git", action="store_false",
                    help="skip the per-tree git probes")
    ap.add_argument("--no-commits", dest="commits", action="store_false",
                    help="hide the commit pane; sessions and github go side by side")
    ap.add_argument("--no-github", dest="github", action="store_false",
                    help="hide the GitHub pane (no gh calls at all)")
    ap.add_argument("--github-interval", type=float, default=None, metavar="SECS",
                    help="seconds between gh sweeps (overrides --speed)")
    ap.add_argument("--commits-width", type=int, metavar="COLS",
                    help="fix the commit pane at COLS wide instead of scaling it")
    ap.add_argument("--version", action="version", version="leghorn " + __version__)
    args = ap.parse_args()

    # An explicit interval always wins over the preset: someone who typed a
    # number meant it, and p can still retune both from there.
    _, interval, github_interval = speed_settings(args.speed)
    if args.interval is not None:
        interval = args.interval
    if args.github_interval is not None:
        github_interval = args.github_interval

    model = Model(interval, args.git, args.commits, args.github,
                  github_interval)
    worker = threading.Thread(target=model.run, daemon=True)
    worker.start()
    if args.github:
        gh_worker = threading.Thread(target=model.run_github, daemon=True)
        gh_worker.start()
    try:
        curses.wrapper(loop, model, args)
    except KeyboardInterrupt:
        pass
    finally:
        model.stop()
        # Join briefly so a worker mid-sweep finishes (or drains via CANCEL)
        # before the terminal is handed back -- a daemon thread that dies
        # during interpreter teardown prints "Exception ignored" straight onto
        # the restored shell prompt.
        worker.join(timeout=5)
        if args.github:
            gh_worker.join(timeout=5)


if __name__ == "__main__":
    main()
