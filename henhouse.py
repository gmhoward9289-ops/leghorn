#!/usr/bin/env python3
"""henhouse -- one table for every live Claude Code session: state and intent.

Claude Code writes a JSONL transcript per session, and every turn in it carries
the model and its token usage -- so status, context and burn are all derivable
without asking a third-party binary for them. What the transcript has no opinion
on is intent: ccwork's registry knows which worktree and branch a session holds
and what it claimed it was doing, but nothing about whether that session is
stuck, idle or eating context.

henhouse joins them. `~/.claude/sessions/<pid>.json` carries the sessionId, which
names both the transcript file and the key ccwork's registry uses, so pid is a
clean join key across all three sources.

A third source is git itself: what each tree has uncommitted and how far it has
drifted from origin. Sessions report what they mean to do; git reports what they
have actually done, which is the only one of the two that can be wrong.

    henhouse                  # one table, sorted by what needs attention
    henhouse -w               # redraw every 3s (the top/htop view)
    henhouse --log            # commit feed across every repo, newest first
    henhouse --log -w         # ...live
    henhouse --wide           # don't truncate the task text
    henhouse --no-git         # skip the git columns if they are ever slow
    henhouse --json           # joined records, for piping somewhere else

Read-only by construction: it reads transcripts and two state files, and gets
git facts from `git-roost --json`, which enforces read-only at the argument
level. It never writes, never claims, never touches a session, and never mutates
a tree. Transcripts are stdlib-only to read, so status and context need no
external binary and work on every platform. git-roost is optional -- without it
the git columns drop, as with --no-git.

This is leghorn's data layer first and a command second; leghorn imports it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration -- one block, one prefix.
#
# roost and leghorn grew their own env vars independently and ended up with
# four conventions between them (ROOST_*, LEGHORN_ROOT, CCWORK_*, and bare
# CLAUDE_PROJECTS_DIR / CURSOR_AGENT_HOME). Worse, the same two paths were
# hardcoded in one tool and overridable in the other, so the pair could be
# pointed at different data on one machine and silently disagree about what
# was running.
#
# Every override here is LEGBAR_*. The older names are still honoured, so an
# existing roost or leghorn install keeps whatever it already had configured;
# the new name wins when both are set.
# ---------------------------------------------------------------------------

HOME = Path.home()


def _env_path(name, default, *legacy):
    """First non-empty of LEGBAR_<name> then any legacy names, else default."""
    for key in (name,) + legacy:
        raw = os.environ.get(key)
        if raw:
            return Path(raw).expanduser()
    return Path(default).expanduser()


def _env_int(name, default, *legacy):
    for key in (name,) + legacy:
        raw = os.environ.get(key)
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass  # a malformed override falls through to the default
    return default


# Where the clones live. Everything under this directory with a .git is a repo:
# the commit feed walks them all, and the GitHub feed asks gh about the ones
# with a github.com origin.
REPOS_ROOT = _env_path("LEGBAR_REPOS_ROOT", HOME / "GitHub", "LEGHORN_ROOT")

# Claude Code's own on-disk state. SESSIONS_DIR holds one <pid>.json per live
# session; PROJECTS_DIR holds the JSONL transcripts keyed by mangled cwd.
SESSIONS_DIR = _env_path(
    "LEGBAR_SESSIONS_DIR", HOME / ".claude" / "sessions", "CCWORK_SESSIONS_DIR")
PROJECTS_DIR = _env_path(
    "LEGBAR_PROJECTS_DIR", HOME / ".claude" / "projects", "CLAUDE_PROJECTS_DIR")

STATE_DIR = _env_path(
    "LEGBAR_STATE_DIR", HOME / "Claude" / "worktrees", "CCWORK_STATE_DIR")
REGISTRY = STATE_DIR / "registry.json"

# Cursor's agent transcripts: ~/.cursor/projects/<slug>/agent-transcripts/.
# A separate writer from Claude Code, and the one that never announces itself
# -- Cursor writes no session marker and no claim, so a fleet view that only
# reads SESSIONS_DIR is blind to every Cursor agent on the machine.
CURSOR_HOME = _env_path("LEGBAR_CURSOR_HOME", HOME / ".cursor", "CURSOR_AGENT_HOME")
CURSOR_PROJECTS_DIR = CURSOR_HOME / "projects"

# Cursor transcripts are never deleted, so without a horizon every agent that
# ever ran shows up forever. A day is long enough to cover an overnight run.
CURSOR_MAX_IDLE_SECS = _env_int(
    "LEGBAR_CURSOR_MAX_IDLE_SECS", 86400, "ROOST_CURSOR_MAX_IDLE_SECS")

# Cursor's global SQLite index. composerHeaders is the only place Cursor
# records its own context meter (contextUsagePercent) and composer names --
# roost's recon measured 71/71 live transcripts carrying neither, so a
# transcript-only lane cannot fill the model or CTX cells (see roost's
# docs/cursor-on-disk.md).
def _cursor_state_db_default():
    if sys.platform == "win32":
        return HOME / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if sys.platform == "darwin":
        return (HOME / "Library" / "Application Support" / "Cursor" / "User"
                / "globalStorage" / "state.vscdb")
    return HOME / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"


CURSOR_STATE_DB = _env_path(
    "LEGBAR_CURSOR_STATE_DB", _cursor_state_db_default(), "ROOST_CURSOR_STATE_DB")

# Which discovery lanes run at all. Set to "claude" alone on a machine with no
# Cursor install to skip the directory walk entirely.
BACKENDS_DEFAULT = "claude,cursor"

ATTENTION = ("needsinput", "waiting", "error", "failed")


def backends():
    raw = os.environ.get("LEGBAR_BACKENDS") or os.environ.get("ROOST_BACKENDS") \
        or BACKENDS_DEFAULT
    return {b.strip().lower() for b in raw.split(",") if b.strip()}


# ---------------------------------------------------------------------------
# Transcript telemetry -- the native replacement for claudectl.
#
# claudectl is not a data source, it is a parser: everything it reports is
# derived from the JSONL transcripts Claude Code already writes to
# ~/.claude/projects/<mangled-cwd>/<sessionId>.jsonl. Reading them directly
# removes a third-party binary that ships no Windows build, and drops the
# estimate layer that produced the impossible context values ctx() has to
# annotate below.
#
# Only the tail of each transcript is read. These files reach several MB and
# the answer is always near the end; scanning whole files once per refresh
# would dominate the render loop.

# PROJECTS_DIR is declared once, in the configuration block at the top.

TAIL_BYTES = 256 * 1024

# Context window per model, longest prefix wins. Sourced from the Anthropic
# model reference rather than guessed -- a wrong denominator here is exactly
# the failure mode that made claudectl's percentages untrustworthy.
CONTEXT_WINDOWS = (
    ("claude-haiku-4-5", 200_000),
    ("claude-opus-5", 1_000_000),
    ("claude-sonnet-5", 1_000_000),
    ("claude-fable-5", 1_000_000),
    ("claude-opus-4", 1_000_000),
    ("claude-sonnet-4", 1_000_000),
)
DEFAULT_WINDOW = 200_000

# Tool names whose input carries a path the session wrote to. Reads are excluded
# on purpose: opening a file says nothing about which project a session is on.
WRITE_TOOLS = ("Write", "Edit", "NotebookEdit")

# A turn is "live" if the transcript was touched this recently.
WORKING_SECS = 90

# Subagent liveness. Both thresholds come from roost, and the gap between them
# is the point: a subagent has no pid of its own -- it runs inside its parent's
# process -- so mtime is the only evidence there is.
#
# ACTIVE is deliberately much tighter than WORKING_SECS. A parent session may
# legitimately sit quiet for a minute while the human reads; a subagent that has
# been handed a task is either writing to its transcript or it has finished, so
# half a minute of silence already means "not working right now".
AGENT_ACTIVE_SECS = 30
# RECENT is the horizon for a subagent that is no longer active but is still
# worth counting as part of this session's fan-out -- usually the run that just
# came back. Older than this and it is history, not the current picture.
AGENT_RECENT_SECS = 3600


def context_window(model):
    for prefix, size in CONTEXT_WINDOWS:
        if model and model.startswith(prefix):
            return size
    return DEFAULT_WINDOW


def transcript_index():
    """sessionId -> newest transcript path.

    Agent sidecars (agent-*.jsonl) are skipped: they are subagent traces, not
    sessions, and have no pid to join against.
    """
    index = {}
    if not PROJECTS_DIR.is_dir():
        return index
    for path in PROJECTS_DIR.glob("*/*.jsonl"):
        if path.name.startswith("agent-"):
            continue
        sid = path.stem
        prev = index.get(sid)
        try:
            if prev is None or path.stat().st_mtime > prev.stat().st_mtime:
                index[sid] = path
        except OSError:
            continue
    return index


def read_tail(path):
    """Whole JSON records from the last TAIL_BYTES of a file.

    The first line of the window is dropped unless the window starts at byte 0 --
    seeking to a fixed offset lands mid-record, and a half line is not JSON.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            start = max(0, size - TAIL_BYTES)
            fh.seek(start)
            blob = fh.read()
        lines = blob.decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    if start and lines:
        lines = lines[1:]
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def count_subagents(sid, now=None):
    """(active, recent) subagent transcripts for one session id.

    Subagents run inside their parent's process, so there is no pid to probe and
    nothing in SESSIONS_DIR to join against -- a fleet view built on the pid join
    alone is blind to every one of them, which is why this field read 0 forever.
    What they do leave is a transcript, one level deeper than the session's own:

        projects/<slug>/<parentSessionId>/subagents/agent-<id>.jsonl

    The parent session id names the directory, so the join is a path lookup
    rather than a search. mtime is then the only liveness evidence available:
    active means the file moved within AGENT_ACTIVE_SECS, recent means within
    AGENT_RECENT_SECS.

    roost additionally gates on whether the parent process is still alive -- a
    subagent whose parent has exited is an "orphan" however fresh its transcript
    looks, because nothing can still be writing to it. Here that gate is already
    closed upstream: load_sessions() only yields sessions whose pid is alive, so
    every sid reaching this function has a live parent by construction.

    The slug is unknown (it encodes the session's cwd, which can change), so
    every project directory is checked for a child named sid. That is one
    listdir plus a direct is_dir() probe -- no recursive walk.
    """
    if not sid or not PROJECTS_DIR.is_dir():
        return 0, 0
    now = time.time() if now is None else now
    active = recent = 0
    try:
        projects = list(PROJECTS_DIR.iterdir())
    except OSError:
        return 0, 0
    for proj in projects:
        subs = proj / sid / "subagents"
        if not subs.is_dir():
            continue
        try:
            entries = list(subs.iterdir())
        except OSError:
            continue
        for f in entries:
            if not (f.name.startswith("agent-") and f.suffix == ".jsonl"):
                continue
            try:
                age = now - f.stat().st_mtime
            except OSError:
                continue
            if age <= AGENT_ACTIVE_SECS:
                active += 1
            if age <= AGENT_RECENT_SECS:
                recent += 1
    return active, recent


# agentId -> the short `description` an Agent tool call was launched with.
# Module-level and never pruned: a finished agent's description is still
# worth showing while its row lingers in AGENT_RECENT_SECS, and re-fetching
# it from a parent transcript that may have scrolled the record out of any
# tail window is not something a fresh call can redo.
_AGENT_DESCRIPTIONS = {}
# Parent transcript path (str) -> bytes already scanned for descriptions.
_HARVEST_POS = {}


def harvest_agent_descriptions(parent_path):
    """Pull each subagent's short description out of the parent's own
    transcript, the way roost does.

    A subagent's own transcript never states what it was asked to do in
    short form -- only the parent's `toolUseResult` carries the `description`
    the Agent tool call was given, keyed by agentId. Incremental: only bytes
    appended since the last scan of this parent are read, so a curses view
    polling every few seconds doesn't re-read a growing transcript from byte
    0 on every tick.
    """
    key = str(parent_path)
    pos = _HARVEST_POS.get(key, 0)
    try:
        size = parent_path.stat().st_size
        if size < pos:
            pos = 0  # truncated or replaced -- start over
        if size == pos:
            return
        with parent_path.open("rb") as fh:
            fh.seek(pos)
            data = fh.read()
    except OSError:
        return
    # Consume whole lines only; a half-written trailing line waits for the
    # next pass instead of being parsed as garbage and skipped forever.
    cut = data.rfind(b"\n") + 1
    _HARVEST_POS[key] = pos + cut
    for line in data[:cut].decode("utf-8", "replace").splitlines():
        if '"agentId"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        result = rec.get("toolUseResult")
        if not isinstance(result, dict):
            continue
        agent_id = result.get("agentId")
        description = result.get("description")
        if agent_id and description:
            _AGENT_DESCRIPTIONS[agent_id] = description


def subagent_task(path):
    """Fallback label: the opening line of the task prompt, from the
    subagent's own transcript.

    Used when harvest_agent_descriptions() has no `description` for this
    agent yet -- the parent's toolUseResult record is written when the Agent
    call is made, but a caller can race it, and a sibling-fanned-out prompt
    ("Finish a stranded work stream...") is often identical across several
    agents, so this is a weaker label than the harvested one, not a
    replacement for it. The task prompt an Agent tool call hands a subagent
    is always its first user turn -- everything after is the subagent's own
    work, not the brief it was given -- so a single readline() gets the
    whole answer without parsing the rest of a transcript that can run to
    megabytes.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return ""
    if not first.strip():
        return ""
    try:
        rec = json.loads(first)
    except ValueError:
        return ""
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        content = " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str) or not content.strip():
        return ""
    return content.strip().splitlines()[0].strip()


SESSION_TOPIC_SCAN_LINES = 50


def session_topic(path):
    """First real thing the human said in a top-level session, one line.

    henhouse's "task" field comes from the claims registry, which is nearly
    always empty in practice -- most sessions never write a claim, so
    everywhere that field is shown ends up saying nothing more specific than
    a repo name. Claude Code and Cursor both title a session (or its own
    task-list row) from what the human actually asked for, so this is the
    fallback that lets a fleet view say the same thing -- "which one is
    this" answered the way the tool the human already has open answers it.

    Unlike a subagent transcript, whose first record is always its task
    prompt, a top-level transcript leads with a handful of queue-operation
    and attachment records before the human's own first message -- so this
    scans forward instead of trusting line 1, but only up to
    SESSION_TOPIC_SCAN_LINES: that preamble is short and bounded, and a
    transcript that still hasn't shown a real user turn by then isn't going
    to on this pass. isMeta records are hook- or system-injected text (e.g.
    a <system-reminder>), not something the human said, and are skipped for
    the same reason a stray <system-reminder> without isMeta is skipped too.
    """
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return ""
    try:
        with fh:
            for i, line in enumerate(fh):
                if i >= SESSION_TOPIC_SCAN_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "user" or rec.get("isMeta"):
                    continue
                content = (rec.get("message") or {}).get("content")
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
                if not isinstance(content, str):
                    continue
                content = content.strip()
                if not content or content.startswith("<system-reminder"):
                    continue
                return content.splitlines()[0].strip()
    except OSError:
        return ""
    return ""


def list_subagents(session_ids, now=None):
    """Live-ish subagent rows for the SUBAGENTS pane, keyed by parent sid.

    Same transcript join as count_subagents, but returns one dict per agent so
    the renderer can show roost's panel rather than a bare count. session_ids
    is an iterable of parent sids that are still alive -- orphans are excluded
    by construction.
    """
    if not PROJECTS_DIR.is_dir():
        return []
    now = time.time() if now is None else now
    wanted = {s for s in session_ids if s}
    if not wanted:
        return []
    try:
        projects = list(PROJECTS_DIR.iterdir())
    except OSError:
        return []
    rows = []
    for proj in projects:
        for sid in wanted:
            subs = proj / sid / "subagents"
            if not subs.is_dir():
                continue
            try:
                entries = list(subs.iterdir())
            except OSError:
                continue
            # The parent transcript sits beside the subagents/ dir it owns --
            # harvest before reading agent files so a freshly-completed
            # agent's description is already in _AGENT_DESCRIPTIONS below.
            harvest_agent_descriptions(proj / ("%s.jsonl" % sid))
            for f in entries:
                if not (f.name.startswith("agent-") and f.suffix == ".jsonl"):
                    continue
                try:
                    age = now - f.stat().st_mtime
                except OSError:
                    continue
                if age > AGENT_RECENT_SECS:
                    continue
                agent_id = f.stem
                if agent_id.startswith("agent-"):
                    agent_id = agent_id[6:]
                rows.append({
                    "parent_sid": sid,
                    "agent_id": agent_id,
                    "task": _AGENT_DESCRIPTIONS.get(agent_id) or subagent_task(f),
                    "idle_secs": int(age),
                    "state": "working" if age <= AGENT_ACTIVE_SECS else "idle",
                })
    rows.sort(key=lambda r: (0 if r["state"] == "working" else 1, r["idle_secs"]))
    return rows


def summarize(records, mtime, sid=None, now=None):
    """One transcript's records -> the telemetry fields build() consumes.

    sid is the session's own id, used only to find its subagents; without it the
    subagent counts are 0, which is what a caller with no sid should get rather
    than a guess.
    """
    now = time.time() if now is None else now
    model = None
    context_tokens = None
    burn = 0
    files = {}
    last_role = None
    last_had_tool = False

    for rec in records:
        kind = rec.get("type")
        msg = rec.get("message") or {}
        if kind in ("user", "assistant"):
            last_role = kind
        if kind != "assistant":
            continue
        last_had_tool = False
        usage = msg.get("usage") or {}
        if usage:
            model = msg.get("model") or model
            total = 0
            for field in ("input_tokens", "cache_read_input_tokens",
                          "cache_creation_input_tokens"):
                value = usage.get(field)
                if isinstance(value, int):
                    total += value
            # Cache reads dominate and are not cumulative across turns -- the
            # last turn's total IS the live context size, so take it rather
            # than summing.
            if total:
                context_tokens = total
            out = usage.get("output_tokens")
            if isinstance(out, int):
                burn += out
        for block in msg.get("content") or ():
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            last_had_tool = True
            if block.get("name") not in WRITE_TOOLS:
                continue
            target = (block.get("input") or {}).get("file_path")
            if isinstance(target, str) and target:
                # split_path and the INFRA filter both expect POSIX separators.
                files[target.replace("\\", "/")] = True

    idle = max(0.0, now - mtime)
    if idle < WORKING_SECS:
        status = "working" if (last_role == "user" or last_had_tool) else "needsinput"
    elif last_role == "assistant" and not last_had_tool:
        status = "needsinput"
    else:
        status = "idle"

    pct = None
    if context_tokens:
        pct = 100.0 * context_tokens / context_window(model)

    active_agents, recent_agents = count_subagents(sid, now)

    return {
        "status": status,
        # Seconds since the transcript last moved. status says WHICH WAY a
        # conversation is pending -- needsinput means it is waiting on the
        # human, working means it is waiting on the model -- and this says how
        # long it has been that way. Without it a reply that has been sat
        # unanswered for an hour looks exactly like one from ten seconds ago.
        "idle_secs": idle,
        "context_pct": pct,
        "context_tokens": context_tokens,
        "model": model,
        "burn_tokens": burn,
        "files_modified": files,
        # cost_usd stays unset on purpose. claudectl reported an API list-price
        # equivalent derived from token counts, which is unrelated to a flat
        # subscription -- a number that reads as money but never was.
        "cost_usd": None,
        # Was hardcoded 0, which made a session running five subagents look
        # identical to one running none -- and the fan-out is usually the whole
        # reason the session's context is climbing.
        "active_subagents": active_agents,
        # Fan-out that has come back but not yet aged out. Kept alongside the
        # active count so "0 working, 3 finished in the last hour" and "nothing
        # ever ran here" stay distinguishable.
        "recent_subagents": recent_agents,
        "estimate": {"verified": True},
    }


def load_transcripts(sessions):
    """pid -> telemetry, read from the sessions' own transcripts."""
    index = transcript_index()
    telemetry = {}
    missing = 0
    for s in sessions:
        pid, sid = s.get("pid"), s.get("sessionId")
        path = index.get(sid) if sid else None
        if not isinstance(pid, int) or path is None:
            missing += 1
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            missing += 1
            continue
        telemetry[pid] = summarize(read_tail(path), mtime, sid)
    warn = None
    if missing and not telemetry:
        warn = "no transcripts found for %d live session(s)" % missing
    elif missing:
        warn = "%d session(s) without a transcript" % missing
    return telemetry, warn


def load_registry():
    try:
        reg = json.loads(REGISTRY.read_text())
    except (OSError, ValueError):
        return {}, {}
    return reg.get("claims") or {}, reg.get("occupancy") or {}


def alive(pid):
    """True if the process exists. os.kill(pid, 0) is POSIX-only.

    On Windows it is not merely unsupported, it is a loaded gun: CPython routes
    any signal other than CTRL_C_EVENT/CTRL_BREAK_EVENT to TerminateProcess, so
    the "probe" would kill every session it asked about.

    The Windows answer used to be one `tasklist` subprocess per pid per refresh,
    with a 10s timeout each. That is a process spawn and a full process-table
    scan to answer a question the kernel can answer with a handle open/close, and
    at a dozen sessions it is what put a floor under the refresh interval.
    OpenProcess(SYNCHRONIZE) is the same question asked directly -- microseconds,
    no subprocess, no timeout to tune.

    Two semantics worth knowing, both inherited from the handle model rather
    than chosen here:
      * A process that has exited but still has an open handle held somewhere
        (its parent has not reaped it) can still be opened, so it reads alive
        until the last handle closes. tasklist would already call it gone.
      * A process owned by another user can fail to open for want of rights,
        and reads as gone. Sessions in SESSIONS_DIR are this user's own, so
        neither case arises on the path that matters.
    """
    if sys.platform == "win32":
        import ctypes

        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def load_sessions():
    """Live sessions only. A stale <pid>.json outlives its process."""
    sessions = []
    if not SESSIONS_DIR.is_dir():
        return sessions
    for path in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            s = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        pid = s.get("pid")
        if isinstance(pid, int) and alive(pid):
            sessions.append(s)
    return sessions


def split_path(raw):
    """A directory -> (project, tree). Worktrees report their parent repo."""
    if not raw:
        return "-", ""
    p = Path(raw)
    parts = p.parts
    # Only the worktree name is wanted, never the subdirectory a file happens to
    # sit in -- "feat-seasonality", not "feat-seasonality/src/counting_chicken_wings".
    if ".worktrees" in parts:
        i = parts.index(".worktrees")
        project = parts[i + 1] if len(parts) > i + 1 else "-"
        tree = parts[i + 2] if len(parts) > i + 2 else "(primary)"
        return project, tree
    try:
        rel = p.relative_to(HOME).parts
    except ValueError:
        return p.name or str(p), ""
    if not rel:
        return "(home)", ""
    # ~/GitHub is a container, not a project -- the repo below it is the project.
    if rel[0] == "GitHub":
        if len(rel) == 1:
            return "(GitHub root)", "unscoped"
        return rel[1], "(primary)"
    return rel[0], ""


# Every session edits memory files and scratchpads. Those say nothing about which
# project it is on, so they must not outvote the two real source files it touched.
INFRA = ("/.claude/", "/private/tmp/", "/tmp/", "/Claude/bin/")


def project_from_files(paths):
    """Majority project/tree among real files a session edited, else None.

    Sessions are nearly all launched from ~/GitHub, so launch cwd is useless as a
    label. What a session actually wrote is not.
    """
    votes = {}
    for raw in paths or ():
        if any(marker in raw for marker in INFRA):
            continue
        key = split_path(str(Path(raw).parent))
        votes[key] = votes.get(key, 0) + 1
    if not votes:
        return None
    return max(votes.items(), key=lambda kv: kv[1])[0]


PSEUDO_PROJECTS = ("-", "(GitHub root)", "(home)")


def tree_path(project, tree):
    """Inverse of split_path: the directory a (project, tree) label points at.

    Needed because a session's recorded cwd is ~/GitHub for nearly all of them,
    which is not a repo -- so probing cwd reports "no git state" for sessions
    that are demonstrably working in a tree. The label, inferred from the files
    they actually wrote, is the better address.
    """
    if project in PSEUDO_PROJECTS:
        return None
    if tree == "(primary)":
        return REPOS_ROOT / project
    if tree in ("", "unscoped"):
        return HOME / project
    return REPOS_ROOT / ".worktrees" / project / tree


GIT_TIMEOUT = 5
# Set by the embedding UI on quit. Executor worker threads are non-daemon and
# the interpreter joins them at exit, so without this a quit pressed mid-sweep
# waits out every queued git/gh call -- the prompt comes back seconds late.
# With it, queued work drains as instant no-ops; only the one call already in
# flight is waited for, bounded by its own subprocess timeout.
CANCEL = threading.Event()

GIT_WORKERS = 8
# Field separator for git --format. NUL would be the obvious choice, but it
# cannot survive argv -- exec rejects an argument with an embedded null byte.
SEP = "\x1f"


def git(dirpath, *args):
    """Read-only git in dirpath. None if it is not a repo, fails, or hangs."""
    if CANCEL.is_set():
        return None
    try:
        out = subprocess.run(
            ("git", "-C", str(dirpath)) + args,
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.rstrip("\n")


GIT_ROOST_TIMEOUT = 15
GIT_ROOST_FALLBACK = REPOS_ROOT / "git-roost" / "git_roost.py"


def find_git_roost():
    """The git-roost command, as an argv prefix, or None if it is not here."""
    found = shutil.which("git-roost")
    if found:
        return [found]
    # In a frozen build (PyInstaller) sys.executable is leghorn.exe itself, not
    # a Python interpreter -- [sys.executable, git_roost.py] would hand the
    # script path to leghorn.exe as if it were one of its own arguments rather
    # than running it. Only a source checkout can use this fallback.
    if getattr(sys, "frozen", False):
        return None
    if GIT_ROOST_FALLBACK.is_file():
        return [sys.executable, str(GIT_ROOST_FALLBACK)]
    return None


def gather_git(dirs):
    """directory -> git state, one `git-roost --json` scan for all of them.

    git-roost owns the hard parts: read-only enforcement at the argument level,
    and the baseline chain -- upstream, then origin/HEAD, then a single remote
    by whatever name it has. An earlier version of this file reimplemented drift
    with an origin-only fallback and reported a tree nine commits behind its
    `deploy` remote as clean, which is exactly why it doesn't anymore.

    Each directory is passed as its own --depth 0 root, so git-roost probes the
    named trees and nothing else. A dir that is no repo gets None, as before.
    """
    unique = sorted({d for d in dirs if d})
    cmd = find_git_roost()
    if not unique or not cmd:
        return {}
    for d in unique:
        cmd += ["--root", d]
    cmd += ["--json", "--depth", "0"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=GIT_ROOST_TIMEOUT)
        records = json.loads(out.stdout) if out.returncode == 0 else []
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return {}

    now = time.time()
    by_top = {}
    for st in records:
        # git reports toplevel with forward slashes even on Windows, while the
        # dirs we look it up with are native. normcase folds both to one
        # spelling (and is a no-op on POSIX), so the join is not
        # separator-sensitive.
        by_top[os.path.normcase(os.path.normpath(st["toplevel"]))] = {
            # Legacy shape, so leghorn and fmt() need no changes. "dirty" always
            # meant the worktree side; git-roost calls that "unstaged".
            "staged": st.get("staged", 0),
            "dirty": st.get("unstaged", st.get("tracked", 0)),
            "untracked": st.get("untracked", 0),
            "ahead": st.get("ahead"),
            "behind": st.get("behind"),
            "base": st.get("base") or "",
            "operation": st.get("operation") or "",
            "last": st.get("last_subject") or "",
            "last_age": (now - st["last_ts"]) if st.get("last_ts") else None,
        }

    states = {}
    for d in unique:
        key = os.path.normcase(os.path.normpath(d))
        hit = by_top.get(key)
        if hit is None:
            # The dir may sit inside a tree probed under another root.
            for top, st in by_top.items():
                if key.startswith(top.rstrip(os.sep) + os.sep):
                    hit = st
                    break
        states[d] = hit
    return states


def ago(seconds):
    if seconds is None:
        return "-"
    s = int(seconds)
    for limit, unit, div in ((60, "s", 1), (3600, "m", 60), (86400, "h", 3600)):
        if s < limit:
            return "%d%s" % (s // div, unit)
    return "%dd" % (s // 86400)


def commit_feed(limit):
    """Recent commits across every repo under ~/GitHub, newest first.

    Worktrees share their repo's object store, so one --all walk per primary repo
    already covers every ccwork branch -- no need to visit the worktrees.
    """
    root = REPOS_ROOT
    if not root.is_dir():
        return []
    repos = [p for p in sorted(root.iterdir()) if (p / ".git").exists()]

    def walk(repo):
        return git(repo, "log", "--all", "--date-order", "-n", str(limit),
                   "--format=" + SEP.join(("%ct", "%h", "%an", "%D", "%s")))

    commits = []
    # Not a with-block: __exit__ waits for every queued task, which on quit is
    # exactly the wait CANCEL exists to skip.
    pool = ThreadPoolExecutor(max_workers=GIT_WORKERS)
    try:
        for repo, raw in zip(repos, pool.map(walk, repos)):
            for line in (raw or "").splitlines():
                parts = line.split(SEP)
                if len(parts) != 5 or not parts[0].isdigit():
                    continue
                ts, sha, author, refs, subject = parts
                commits.append({
                    "repo": repo.name,
                    "ts": int(ts),
                    "sha": sha,
                    "author": author,
                    "refs": refs.replace("HEAD -> ", "").strip(),
                    "subject": subject,
                })
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    commits.sort(key=lambda c: -c["ts"])
    return commits[:limit]


def fmt_feed(commits, wide, width):
    if not commits:
        return ["no commits found under ~/GitHub"]
    cols = [
        ("AGE", lambda c: ago(time.time() - c["ts"])),
        ("REPO", lambda c: c["repo"]),
        ("SHA", lambda c: c["sha"]),
        ("REFS", lambda c: c["refs"].split(", ")[0] if c["refs"] else ""),
        ("AUTHOR", lambda c: c["author"]),
    ]
    table = [[h for h, _ in cols]] + [[f(c) for _, f in cols] for c in commits]
    widths = [max(len(row[i]) for row in table) for i in range(len(cols))]
    subject_w = max(20, width - sum(widths) - 2 * len(widths) - 2)

    lines = []
    for i, row in enumerate(table):
        cells = [row[j].ljust(widths[j]) for j in range(len(cols))]
        subject = "SUBJECT" if i == 0 else commits[i - 1]["subject"]
        if not wide and len(subject) > subject_w:
            subject = subject[: subject_w - 3] + "..."
        lines.append(("  ".join(cells) + "  " + subject).rstrip())
    lines.append("")
    lines.append("%d commit(s) across %d repo(s)"
                 % (len(commits), len({c["repo"] for c in commits})))
    return lines


# ---- GitHub feed: CI runs and open PRs, via gh ------------------------------

GH_TIMEOUT = 20
GH_WORKERS = 6
GH_RUN_LIMIT = 20   # per repo, before the superseded-red collapse
# Homebrew is absent from a non-login shell's PATH, which is how hooks and cron
# invoke things. Look there explicitly rather than failing with "not found".
GH_FALLBACKS = ("/opt/homebrew/bin/gh", "/usr/local/bin/gh")

RED_STATES = ("failure", "timed_out", "startup_failure", "error", "action_required")
# A run still queued/running after this long is not "live", it is stuck -- and
# ranking it with the live runs pins it to the top of the feed forever. The
# first real fleet scan had two dependabot graph runs QUEUED for 17 hours.
STUCK_RUN_SECS = 2 * 3600


def find_gh():
    found = shutil.which("gh")
    if found:
        return found
    for candidate in GH_FALLBACKS:
        if Path(candidate).is_file():
            return candidate
    return None


def gh_preflight():
    """Empty string if gh can answer, else why not.

    Lifted from pipeline-check, and kept for the same reason: gh_json() folds
    every error into None, which is right for one flaky call and catastrophic
    as a global condition. On 2026-07-31 an expired token 401'd every GraphQL
    call while REST kept working anonymously, and the result read as good news.
    A feed that under-reports must instead say it cannot see.
    """
    gh = find_gh()
    if not gh:
        return "gh is not on PATH"
    try:
        r = subprocess.run([gh, "auth", "status"], capture_output=True,
                           text=True, timeout=GH_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return "gh auth status did not answer"
    if r.returncode:
        # `gh auth status` reports EVERY configured account; pick the line that
        # names the problem, not a healthy account's scopes.
        lines = [l.strip(" -\t") for l in (r.stdout + r.stderr).splitlines() if l.strip()]
        markers = ("failed to log in", "invalid", "not logged in",
                   "requires authentication", "expired")
        for l in lines:
            if any(m in l.lower() for m in markers):
                return l
        return lines[-1] if lines else "gh is not authenticated"
    return ""


def gh_json(gh, args, cwd):
    """One gh call parsed as JSON, or None. None, not [] -- a caller merging
    per-repo results must be able to tell 'no PRs' from 'could not ask'."""
    if CANCEL.is_set():
        return None
    try:
        out = subprocess.run([gh] + args, cwd=str(cwd), capture_output=True,
                             text=True, timeout=GH_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def _gh_epoch(ts):
    """GitHub timestamps are RFC3339 with a trailing Z, which fromisoformat
    only learned to read in 3.11. None on any surprise -- an event with no age
    beats a feed that died on one."""
    if not ts:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def pr_check_state(rollup):
    """(overall, failing names) from a PR's statusCheckRollup.

    THE ROLLUP IS NOT DEDUPED, and taking it at face value is wrong: a workflow
    that re-runs on the same head commit leaves BOTH check runs under one name,
    so a PR read as red sixteen minutes after its fix while `gh pr checks`
    showed green. Collapse to the newest run per name first, exactly as
    `gh pr checks` does, and judge only that. CANCELLED is excluded
    deliberately -- a cancelled run is usually somebody superseding it on
    purpose, and calling that a failure trains the eye to ignore red.
    """
    bad = {s.upper() for s in RED_STATES}
    latest = {}
    for c in rollup or []:
        # CheckRun carries name/conclusion; StatusContext carries context/state.
        name = c.get("name") or c.get("context") or "check"
        state = (c.get("conclusion") or c.get("state") or "").upper()
        when = c.get("completedAt") or c.get("startedAt") or c.get("createdAt") or ""
        if name not in latest or when >= latest[name][0]:
            latest[name] = (when, state)
    if not latest:
        return "none", []
    red = sorted(n for n, (_, s) in latest.items() if s in bad)
    if red:
        return "red", red
    if any(s in ("", "PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED")
           for _, s in latest.values()):
        return "pending", []
    return "green", []


def _repo_events(gh, repo):
    """CI runs and open PRs for one repo, as feed events. None if gh failed."""
    runs = gh_json(gh, ["run", "list", "--limit", str(GH_RUN_LIMIT), "--json",
                        "status,conclusion,name,displayTitle,createdAt,"
                        "updatedAt,url,headBranch"], cwd=repo)
    prs = gh_json(gh, ["pr", "list", "--state", "open", "--json",
                       "number,title,createdAt,updatedAt,reviewDecision,"
                       "headRefName,isDraft,url,statusCheckRollup"], cwd=repo)
    if runs is None and prs is None:
        return None

    events = []
    seen = set()
    for run in runs or []:
        status, concl = run.get("status"), (run.get("conclusion") or "").lower()
        key = (run.get("name"), run.get("headBranch"))
        if status == "completed":
            # Only the newest completed run per workflow+branch speaks -- gh
            # returns runs newest first, so first seen wins. A failure a later
            # run already fixed is history, and an alert that stays red after
            # the fix is an alert people learn to scroll past.
            if key in seen:
                continue
            seen.add(key)
            state = "failed" if concl in RED_STATES else concl or "done"
        else:
            state = status or "queued"   # in_progress / queued, still live
            started = _gh_epoch(run.get("createdAt"))
            if started and time.time() - started > STUCK_RUN_SECS:
                state = "stuck"
        events.append({
            "kind": "run", "repo": repo.name,
            "ts": _gh_epoch(run.get("updatedAt") or run.get("createdAt")),
            "branch": run.get("headBranch") or "",
            "workflow": run.get("name") or "?",
            "state": state,
            "title": run.get("displayTitle") or "",
            "url": run.get("url") or "",
        })
    for pr in prs or []:
        checks, red = pr_check_state(pr.get("statusCheckRollup"))
        events.append({
            "kind": "pr", "repo": repo.name,
            "ts": _gh_epoch(pr.get("updatedAt") or pr.get("createdAt")),
            "created_ts": _gh_epoch(pr.get("createdAt")),
            "number": pr.get("number"),
            "branch": pr.get("headRefName") or "",
            "title": pr.get("title") or "",
            "draft": bool(pr.get("isDraft")),
            "checks": checks,
            "red": red,
            "review": pr.get("reviewDecision") or "",
            "url": pr.get("url") or "",
        })
    return events


def github_repos():
    """One clone per distinct GitHub origin, under REPOS_ROOT.

    Swamplink remotes are skipped -- they have no Actions or PRs, so asking gh
    about them only burns time.

    Deduplicated by origin, which is the whole point: worktrees of one repo sit
    beside it as sibling directories (roost, roost-wt-advice, roost-wt-trend)
    and every one of them reports the same origin. Grouping by directory asked
    gh about the same repository three times and rendered every run and every
    pull request three times, so a pane showing 24 red CI events was showing
    about 8 real ones. It also tripled the sweep's wall-clock for nothing.
    """
    root = REPOS_ROOT
    if not root.is_dir():
        return []
    seen = {}
    for p in sorted(root.iterdir()):
        if not (p / ".git").exists():
            continue
        url = git(p, "remote", "get-url", "origin") or ""
        if "github.com" not in url:
            continue
        # Normalize so git@github.com:o/r.git and https://github.com/o/r
        # collapse to the same key.
        slug = url.strip()
        for prefix in ("git@github.com:", "https://github.com/",
                       "ssh://git@github.com/", "http://github.com/"):
            if slug.startswith(prefix):
                slug = slug[len(prefix):]
                break
        slug = slug[:-4] if slug.endswith(".git") else slug
        slug = slug.rstrip("/").lower()
        # First clone wins: sorted() puts the plain checkout ahead of its
        # `-wt-*` worktree siblings, so the row is labelled with the repo's
        # own name rather than a worktree's.
        seen.setdefault(slug, p)
    return list(seen.values())


def github_feed(limit=40):
    """(events, warn): CI runs and open PRs across every GitHub clone.

    warn is truthy when gh cannot answer at all -- show it, because an empty
    feed and a feed that cannot see are different facts. A single repo failing
    while the rest answer is folded silently; one flaky call is not a global
    condition.
    """
    broken = gh_preflight()
    if broken:
        return [], broken
    gh = find_gh()
    repos = github_repos()
    if not repos:
        return [], ""

    events = []
    # Same shape as commit_feed: shutdown must not wait for queued tasks.
    pool = ThreadPoolExecutor(max_workers=GH_WORKERS)
    try:
        for per_repo in pool.map(lambda r: _repo_events(gh, r), repos):
            events.extend(per_repo or [])
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    # Live runs first, then red (a failure must not scroll away), then stuck,
    # then everything else by freshness.
    rank = {"in_progress": 0, "queued": 0, "failed": 1, "stuck": 2}
    events.sort(key=lambda e: (rank.get(e.get("state"), 3) if e["kind"] == "run"
                               else (1 if e.get("checks") == "red" else 3),
                               -(e.get("ts") or 0)))
    return events[:limit], ""


def fmt_github(events, warn, width):
    if warn:
        return ["github feed unavailable: %s" % warn]
    if not events:
        return ["no CI runs or open PRs across GitHub clones"]
    lines = []
    for e in events:
        age = ago(time.time() - e["ts"]) if e.get("ts") else "-"
        if e["kind"] == "run":
            what = "%s %s (%s)" % (e["state"].upper(), e["workflow"], e["branch"])
        else:
            bits = ["#%s" % e["number"], e["title"]]
            if e["draft"]:
                bits.append("[draft]")
            bits.append("checks:%s" % e["checks"])
            if e["red"]:
                bits.append("failing: " + ", ".join(e["red"][:3]))
            if e["review"]:
                bits.append(e["review"].lower().replace("_", " "))
            what = " ".join(bits)
        line = "%s  %-4s %-22s %s" % (age.rjust(4), e["kind"].upper(),
                                      e["repo"][:22], what)
        lines.append(line[:width].rstrip())
    return lines


def build(telemetry, claims, occupancy, sessions, use_git=True):
    # sessionId -> the directory ccwork last saw it occupying.
    held = {}
    for directory, holders in occupancy.items():
        for sid, info in (holders or {}).items():
            held[sid] = (directory, (info or {}).get("branch") or "")

    rows = []
    for s in sessions:
        pid = s["pid"]
        sid = s.get("sessionId") or ""
        claim = claims.get(sid) or {}
        occ_dir, occ_branch = held.get(sid, (None, ""))

        t = telemetry.get(pid) or {}

        # Files written beat every recorded location: ccwork's registry only holds
        # sessions that ran `ccwork new` or `claim`, and launch cwd is ~/GitHub for
        # almost all of them. Fall back through occupancy, claim, then cwd.
        directory = occ_dir or claim.get("cwd") or s.get("cwd") or ""
        project, tree = split_path(directory)
        from_files = project_from_files(t.get("files_modified") or {})
        if from_files:
            project, tree = from_files
            src = "files"
        elif occ_dir:
            src = "ccwork"
        else:
            src = "cwd"
        branch = occ_branch or claim.get("branch") or ""

        rows.append({
            "pid": pid,
            "name": s.get("name") or "-",
            "session_id": sid,
            "dir": directory,
            "project": project,
            "tree": tree,
            "located_by": src,
            "branch": branch,
            "task": (claim.get("task") or "").strip(),
            "status": t.get("status") or "-",
            "context_pct": t.get("context_pct"),
            "cost_usd": t.get("cost_usd"),
            "subagents": t.get("active_subagents") or 0,
            "subagents_recent": t.get("recent_subagents") or 0,
            "verified": ((t.get("estimate") or {}).get("verified")),
            "contested": False,
            "git": None,
            "git_dir": "",
        })

    if use_git:
        for r in rows:
            guess = tree_path(r["project"], r["tree"])
            r["git_dir"] = str(guess) if guess and guess.is_dir() else r["dir"]
        states = gather_git(r["git_dir"] for r in rows)
        for r in rows:
            r["git"] = states.get(r["git_dir"])

    # Two live sessions in one working tree is the collision ccwork guards against.
    # Sharing ~/GitHub is not a collision -- it is just where they were launched --
    # so the container pseudo-project never counts as contested.
    seen = {}
    for r in rows:
        if r["project"] in ("(GitHub root)", "-", "(home)"):
            continue
        seen.setdefault((r["project"], r["tree"]), []).append(r)
    for _, group in seen.items():
        if len(group) > 1:
            for r in group:
                r["contested"] = True
    return rows


def uncommitted(r):
    g = r.get("git") or {}
    return g.get("staged", 0) + g.get("dirty", 0) + g.get("untracked", 0)


def sort_key(r):
    ctx = r["context_pct"] if isinstance(r["context_pct"], (int, float)) else -1
    return (
        not r["contested"],
        str(r["status"]).lower().replace(" ", "") not in ATTENTION,
        not uncommitted(r),
        -ctx,
    )


def fmt(rows, wide, width):
    if not rows:
        return ["no live Claude Code sessions"]

    def ctx(r):
        v = r["context_pct"]
        if not isinstance(v, (int, float)):
            return "-"
        # The "?" is a tripwire, not decoration: >100% means the model matched
        # no entry in CONTEXT_WINDOWS and fell through to DEFAULT_WINDOW. Show
        # the number, but never let a stale window table read as truth.
        return "%.0f%%%s" % (v, "?" if v > 100 else "")

    def sub(r):
        return str(r["subagents"]) if r["subagents"] else ""

    def dirt(r):
        g = r.get("git")
        if not g:
            return "-"
        parts = [sigil + str(g[key])
                 for sigil, key in (("+", "staged"), ("~", "dirty"), ("?", "untracked"))
                 if g[key]]
        return "".join(parts) or "clean"

    def drift(r):
        g = r.get("git")
        if not g or g["ahead"] is None:
            return "-"
        return ("^%d" % g["ahead"] if g["ahead"] else "") + \
               ("v%d" % g["behind"] if g["behind"] else "") or "="

    def commit_age(r):
        g = r.get("git")
        return ago(g["last_age"]) if g else "-"

    cols = [
        ("", lambda r: "!" if r["contested"] else ""),
        ("NAME", lambda r: r["name"]),
        ("PID", lambda r: str(r["pid"])),
        ("STATUS", lambda r: str(r["status"])),
        ("CTX", ctx),
        ("SUB", sub),
        ("PROJECT", lambda r: r["project"]),
        ("TREE", lambda r: r["tree"]),
        ("BRANCH", lambda r: r["branch"]),
    ]
    # Under --no-git nothing was probed, so the three columns would be a wall of
    # dashes claiming every tree is clean. Drop them instead.
    if any(r["git_dir"] for r in rows):
        cols += [("UNCOMMITTED", dirt), ("DRIFT", drift), ("LASTCOMMIT", commit_age)]
    table = [[h for h, _ in cols]] + [[f(r) for _, f in cols] for r in rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(cols))]

    # TASK gets whatever the terminal has left; it is the most useful column and
    # the only one worth truncating.
    used = sum(widths) + 2 * len(widths)
    task_w = max(20, width - used - 2)

    lines = []
    for i, row in enumerate(table):
        cells = [row[j].ljust(widths[j]) for j in range(len(cols))]
        task = "TASK" if i == 0 else rows[i - 1]["task"]
        if not wide and len(task) > task_w:
            task = task[: task_w - 3] + "..."
        lines.append(("  ".join(cells) + "  " + task).rstrip())

    n_contested = sum(1 for r in rows if r["contested"])
    lines.append("")
    summary = "%d session(s)" % len(rows)
    if n_contested:
        summary += "  |  ! %d sharing a directory" % n_contested
    unclaimed = sum(1 for r in rows if not r["task"])
    if unclaimed:
        summary += "  |  %d with no ccwork claim" % unclaimed
    # Count trees, not sessions -- several sessions in one dirty tree is one
    # pile of uncommitted work, and counting it twice overstates the mess.
    dirty = {r["git_dir"] for r in rows if uncommitted(r)}
    if dirty:
        summary += "  |  %d tree(s) uncommitted" % len(dirty)
    behind = {r["git_dir"] for r in rows if (r.get("git") or {}).get("behind")}
    if behind:
        summary += "  |  %d behind base" % len(behind)
    lines.append(summary)
    return lines


def render(args, width):
    if args.github:
        events, warn = github_feed(args.github)
        if args.json:
            return [json.dumps({"events": events, "warn": warn}, indent=2)]
        return fmt_github(events, warn, width)

    if args.log:
        commits = commit_feed(args.log)
        if args.json:
            return [json.dumps(commits, indent=2)]
        return fmt_feed(commits, args.wide, width)

    sessions = load_sessions()
    telemetry, warn = load_transcripts(sessions)
    claims, occupancy = load_registry()
    rows = sorted(
        build(telemetry, claims, occupancy, sessions, use_git=not args.no_git),
        key=sort_key,
    )
    if args.json:
        return [json.dumps(rows, indent=2)]
    lines = fmt(rows, args.wide, width)
    if warn:
        lines.append("note: %s -- status and context unavailable" % warn)
    return lines


# ---------------------------------------------------------------------------
# The Cursor lane.
#
# Cursor is a second agent writer on the same machine and the only one that
# never announces itself: it writes no <pid>.json marker, so the pid-join that
# finds every live Claude session finds nothing here. What it does write is
# ~/.cursor/projects/<slug>/agent-transcripts/<agent-id>/*.jsonl, and <slug>
# encodes the absolute cwd -- which is enough to answer "what is Cursor doing,
# and where" without reading its SQLite state at all.
#
# Liveness is therefore inferred from transcript mtime rather than a process
# probe. That is weaker than the Claude lane's os.kill(pid, 0) and is labelled
# as such: a Cursor row means "this transcript moved recently", not "this
# process exists". CURSOR_MAX_IDLE_SECS is the horizon; transcripts are never
# cleaned up, so without it every agent that ever ran would show up forever.
# ---------------------------------------------------------------------------


def cursor_slug_to_cwd(slug):
    """Decode a Cursor project-slug back to an absolute path.

    The encoding is lossy: it joins path parts with '-', and '-' is legal
    inside a directory name, so `c-Users-gmhow-dev-heron-ops` is equally
    readable as `dev/heron/ops` and `dev/heron-ops`. Splitting naively gets
    every hyphenated repo in this estate wrong -- heron-ops, swamp-ops,
    open-vanity, counting-chicken-wings.

    The filesystem is the only thing that can settle it, so each segment is
    resolved greedily longest-first against what actually exists on disk. A
    slug that resolves nowhere (another machine's path, a deleted checkout)
    falls back to the naive split, which is wrong in the same way it was
    before but never raises.
    """
    parts = [p for p in str(slug).split("-") if p]
    if not parts:
        return ""
    if len(parts[0]) == 1 and parts[0].isalpha():
        base, rest, sep = parts[0].lower() + ":\\", parts[1:], "\\"
    else:
        base, rest, sep = "/", parts, "/"

    resolved, cur, i = [], base, 0
    while i < len(rest):
        # Longest first: prefer 'heron-ops' over 'heron' when both could match.
        for k in range(len(rest), i, -1):
            cand = "-".join(rest[i:k])
            if os.path.isdir(os.path.join(cur, cand)):
                resolved.append(cand)
                cur = os.path.join(cur, cand)
                i = k
                break
        else:
            return base + sep.join(rest)   # nothing on disk -- naive split
    return base + sep.join(resolved)


def _cursor_ms_to_epoch(ms):
    """composerHeaders timestamps are ms since epoch; tolerate seconds too."""
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return ms / 1000.0 if ms > 1e12 else float(ms)


def read_cursor_composer_headers(db_path=None):
    """Non-archived parent composers from Cursor's state.vscdb, keyed by
    composer id: name, subtitle, ctx_pct (Cursor's own meter), last_write.

    Empty on any error -- a missing DB is normal on a machine that has never
    run Cursor, and the view must not care. Opened read-only via URI: Cursor
    keeps this file open while it runs, and a write-mode open can fail or
    corrupt.
    """
    path = Path(db_path) if db_path else CURSOR_STATE_DB
    if path is None or not path.is_file():
        return {}
    headers = {}
    try:
        uri = "file:%s?mode=ro" % path.resolve().as_posix()
        con = sqlite3.connect(uri, uri=True, timeout=0.5)
        try:
            cur = con.execute(
                "SELECT composerId, lastUpdatedAt, createdAt, value "
                "FROM composerHeaders "
                "WHERE COALESCE(isSubagent, 0) = 0 "
                "AND COALESCE(isArchived, 0) = 0")
            for cid, lu, created, val in cur:
                try:
                    h = json.loads(val) if val else {}
                except ValueError:
                    h = {}
                if not isinstance(h, dict):
                    h = {}
                # Draft empty-state and ephemeral chat composers are noise.
                if h.get("isDraft") or cid == "empty-state-draft":
                    continue
                if h.get("unifiedMode") == "chat" and h.get("isEphemeral"):
                    continue
                pct = h.get("contextUsagePercent")
                headers[cid] = {
                    "name": (h.get("name") or "").strip() or None,
                    "subtitle": (h.get("subtitle") or "").strip() or None,
                    "ctx_pct": float(pct) if isinstance(pct, (int, float)) else None,
                    "last_write": _cursor_ms_to_epoch(
                        h.get("lastUpdatedAt") or lu
                        or h.get("createdAt") or created),
                }
        finally:
            con.close()
    except (sqlite3.Error, OSError, ValueError):
        return {}
    return headers


def _cursor_scan_transcript(path):
    """Newest user_query and best-effort model from a Cursor transcript.

    Tail-read like Claude's. The model is rare on purpose: parent records
    almost never carry message.model, so the usual source is a spawned Task
    tool_use whose input names the model -- the only place roost's recon
    found one across 71 live files.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > TAIL_BYTES:
                fh.seek(size - TAIL_BYTES)
                fh.readline()          # drop the partial line the seek landed in
            blob = fh.read().decode("utf-8", "replace")
    except OSError:
        return "", None
    task, model = "", None
    for line in blob.splitlines():
        if '"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue                   # a torn last line is normal, not an error
        if not isinstance(rec, dict):
            continue
        text = rec.get("text") or rec.get("content")
        if isinstance(text, str):
            m = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.DOTALL)
            if m:
                task = " ".join(m.group(1).split())
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        found = msg.get("model") or rec.get("model")
        if not found:
            content = msg.get("content") or rec.get("content")
            if isinstance(content, list):
                for part in content:
                    if (isinstance(part, dict)
                            and part.get("type") == "tool_use"
                            and part.get("name") == "Task"
                            and isinstance(part.get("input"), dict)
                            and part["input"].get("model")):
                        found = part["input"]["model"]
        if found:
            model = str(found)         # forward scan: the newest mention wins
    return task, model


def load_cursor_sessions(now=None):
    """Recently-active Cursor agents, shaped like the Claude session rows.

    Returns dicts with the same keys the renderer reads for a Claude row, plus
    ``source="cursor"``. ``pid`` is None on purpose: there is no process to
    join against, and a fake one would make a weaker signal look like the
    stronger one.

    Rows are enriched from composerHeaders when state.vscdb is readable:
    ``ctx_pct`` is Cursor's own meter, the composer name backs up a transcript
    with no user_query, and lastUpdatedAt can refresh liveness when the header
    is newer than the transcript. Without the DB the lane degrades to exactly
    what it was -- mtime and task text -- rather than failing.
    """
    if "cursor" not in backends():
        return []
    root = CURSOR_PROJECTS_DIR
    if not root.is_dir():
        return []
    now = time.time() if now is None else now
    headers = read_cursor_composer_headers()
    rows = []
    try:
        projects = list(root.iterdir())
    except OSError:
        return []
    for proj in projects:
        transcripts = proj / "agent-transcripts"
        if not transcripts.is_dir():
            continue
        cwd = cursor_slug_to_cwd(proj.name)
        try:
            agents = list(transcripts.iterdir())
        except OSError:
            continue
        for agent in agents:
            if not agent.is_dir():
                continue
            files = [f for f in agent.glob("*.jsonl") if f.is_file()]
            if not files:
                continue
            newest = max(files, key=lambda f: f.stat().st_mtime)
            idle = now - newest.stat().st_mtime
            h = headers.get(agent.name) or {}
            # The header's lastUpdatedAt can move without the transcript (a
            # turn that wrote no JSONL); take whichever says fresher.
            if h.get("last_write"):
                idle = min(idle, now - h["last_write"])
            if idle > CURSOR_MAX_IDLE_SECS:
                continue
            task, model = _cursor_scan_transcript(newest)
            rows.append({
                "source": "cursor",
                "pid": None,
                "name": agent.name[:8],
                "sessionId": agent.name,
                "cwd": cwd,
                "idle_secs": int(idle),
                "status": "working" if idle < WORKING_SECS else "idle",
                "task": task or h.get("name") or h.get("subtitle") or "",
                # Cursor's own meter, not a transcript inference -- measured,
                # the transcripts carry no usage at all.
                "ctx_pct": h.get("ctx_pct"),
                "model": model,
            })
    rows.sort(key=lambda r: r["idle_secs"])
    return rows


def main():
    ap = argparse.ArgumentParser(
        description="Live Claude Code sessions: transcript telemetry joined to "
                    "ccwork intent and real git state.")
    ap.add_argument("-w", "--watch", nargs="?", const=3.0, type=float, metavar="SECS",
                    help="redraw every SECS seconds (default 3)")
    ap.add_argument("--log", nargs="?", const=25, type=int, metavar="N",
                    help="show the last N commits across every repo (default 25)")
    ap.add_argument("--github", nargs="?", const=40, type=int, metavar="N",
                    help="show CI runs and open PRs across every GitHub clone")
    ap.add_argument("--wide", action="store_true", help="do not truncate the task text")
    ap.add_argument("--no-git", action="store_true",
                    help="skip the git columns (no per-tree git calls)")
    ap.add_argument("--json", action="store_true", help="emit joined records as JSON")
    args = ap.parse_args()

    if not args.watch:
        width = shutil.get_terminal_size((160, 24)).columns
        print("\n".join(render(args, width)))
        return

    try:
        while True:
            width = shutil.get_terminal_size((160, 24)).columns
            body = render(args, width)
            sys.stdout.write("\033[H\033[2J")
            sys.stdout.write(time.strftime("henhouse  %H:%M:%S") + "\n\n")
            sys.stdout.write("\n".join(body) + "\n")
            sys.stdout.flush()
            time.sleep(args.watch)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
