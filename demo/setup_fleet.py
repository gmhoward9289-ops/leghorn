#!/usr/bin/env python3
"""Stage a demo fleet for the leghorn GIFs.

Creates a fake HOME (LEGHORN_DEMO_HOME, default /tmp/leghorn-demo-home) with
~/.claude session files backed by live pids, transcripts with realistic usage,
a ccwork-style registry (claims + occupancy) so the TASK column and contested
rows have something to say, real git repos under ~/GitHub with commit
history and a couple of them made ahead/behind/dirty, and a stub `gh` on
PATH so the GITHUB pane has CI runs and open PRs to show without ever
touching the network.

Everything is synthetic; leghorn itself runs unmodified against this state.
Recording (from this directory, with vhs + ffmpeg + ttyd on PATH):
    python setup_fleet.py &
    vhs hero.tape
    vhs loop.tape

Windows note: Path.home() reads USERPROFILE, not HOME -- the tapes export
USERPROFILE (not HOME) so leghorn resolves ~ to the staged fleet. macOS/Linux
recordings would export HOME instead.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

DEMO_HOME = Path(os.environ.get("LEGHORN_DEMO_HOME")
                 or (os.environ.get("USERPROFILE") if os.name == "nt" else None)
                 or os.environ.get("HOME") or "/tmp/leghorn-demo-home")
CLAUDE = DEMO_HOME / ".claude"
SESSIONS = CLAUDE / "sessions"
PROJECTS = CLAUDE / "projects"
GITHUB_ROOT = DEMO_HOME / "GitHub"
STATE_DIR = DEMO_HOME / "Claude" / "worktrees"
REGISTRY = STATE_DIR / "registry.json"
BIN = DEMO_HOME / "bin"

NOW = time.time()

REAL_GIT_ROOST = Path(os.environ.get("REAL_GIT_ROOST_PY")
                      or (Path.home() / "GitHub" / "git-roost" / "git_roost.py"))


def spawn_sleeper():
    p = subprocess.Popen(["sleep", "3600"], stdout=subprocess.DEVNULL)
    return p.pid


def usage_line(model, tokens):
    return json.dumps({
        "type": "assistant",
        "message": {"model": model, "usage": {
            "input_tokens": 40,
            "cache_read_input_tokens": tokens - 240,
            "cache_creation_input_tokens": 200,
        }},
    }) + "\n"


def make_session(name, cwd, pid, sid, started_ago):
    SESSIONS.mkdir(parents=True, exist_ok=True)
    (SESSIONS / ("%d.json" % pid)).write_text(json.dumps({
        "pid": pid, "sessionId": sid, "cwd": str(cwd),
        "name": name, "startedAt": int((NOW - started_ago) * 1000),
    }))


def make_transcript(sid, model, tokens, idle_secs, files=()):
    slug = PROJECTS / "-home-g-GitHub"
    slug.mkdir(parents=True, exist_ok=True)
    path = slug / (sid + ".jsonl")
    with open(path, "w") as fh:
        fh.write(json.dumps({"type": "user", "message": {"role": "user"}}) + "\n")
        for f in files:
            fh.write(json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": f}}]},
            }) + "\n")
        fh.write(usage_line(model, tokens))
    t = NOW - idle_secs
    os.utime(path, (t, t))
    return path


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo)] + list(args), check=True,
                    capture_output=True, text=True,
                    env={**os.environ, "GIT_AUTHOR_NAME": "demo",
                         "GIT_AUTHOR_EMAIL": "demo@example.com",
                         "GIT_COMMITTER_NAME": "demo",
                         "GIT_COMMITTER_EMAIL": "demo@example.com"})


def make_repo(name, commits, remote="origin", github=True, ahead=0, dirty_file=None):
    """A real git repo under ~/GitHub, with a bare 'remote' so ahead/behind is real."""
    repo = GITHUB_ROOT / name
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")

    bare = GITHUB_ROOT / (".remotes" / (name + ".git")) if False else GITHUB_ROOT.parent / "remotes" / (name + ".git")
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    url = ("https://github.com/demo/%s.git" % name) if github else str(bare)
    git(repo, "remote", "add", "origin", url)
    git(repo, "remote", "add", "pushmirror", str(bare))

    for i, (subject, ts_ago) in enumerate(commits):
        (repo / "NOTES.md").write_text("commit %d\n" % i)
        git(repo, "add", "-A")
        env_ts = str(int(NOW - ts_ago))
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", subject,
                        "--date", env_ts],
                       check=True, capture_output=True, text=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "demo",
                            "GIT_AUTHOR_EMAIL": "demo@example.com",
                            "GIT_COMMITTER_NAME": "demo",
                            "GIT_COMMITTER_EMAIL": "demo@example.com",
                            "GIT_COMMITTER_DATE": env_ts,
                            "GIT_AUTHOR_DATE": env_ts})
    git(repo, "push", "-q", "pushmirror", "main")
    git(repo, "branch", "-q", "--set-upstream-to=pushmirror/main", "main")

    if ahead:
        for i in range(ahead):
            (repo / ("LOCAL_%d.md" % i)).write_text("local only\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "local: work not pushed yet #%d" % i)

    if dirty_file:
        target = repo / dirty_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("still editing this\n")

    return repo


def write_gh_stub():
    """A `gh` on PATH that answers `auth status`, `run list` and `pr list`
    with canned JSON -- no network, same shape leghorn actually parses."""
    BIN.mkdir(parents=True, exist_ok=True)
    now_iso = lambda ago: time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - ago))

    runs = {
        "web-frontend": [
            {"status": "in_progress", "conclusion": None, "name": "ci",
             "displayTitle": "Automate publishing on release",
             "createdAt": now_iso(90), "updatedAt": now_iso(5),
             "url": "https://github.com/demo/web-frontend/actions/runs/1",
             "headBranch": "ci/npm-and-tap-publish"},
        ],
        "tools": [
            {"status": "completed", "conclusion": "failure", "name": "Graph Update",
             "displayTitle": "Required PR Sections", "createdAt": now_iso(3600),
             "updatedAt": now_iso(200), "url": "https://github.com/demo/tools/actions/runs/2",
             "headBranch": "fix/fields"},
        ],
    }
    prs = {
        "web-frontend": [
            {"number": 18, "title": "Automate publishing on release",
             "createdAt": now_iso(7200), "updatedAt": now_iso(1440),
             "reviewDecision": "APPROVED", "headRefName": "ci/npm-and-tap-publish",
             "isDraft": False, "url": "https://github.com/demo/web-frontend/pull/18",
             "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS",
                                    "completedAt": now_iso(1440)}]},
        ],
        "tools": [
            {"number": 593, "title": "Add field selection to the graph query",
             "createdAt": now_iso(61200), "updatedAt": now_iso(61200),
             "reviewDecision": "", "headRefName": "fix/fields", "isDraft": False,
             "url": "https://github.com/demo/tools/pull/593",
             "statusCheckRollup": [{"name": "Required PR Sections",
                                    "conclusion": "FAILURE", "completedAt": now_iso(61200)}]},
        ],
    }

    script = BIN / ("gh.cmd" if os.name == "nt" else "gh")
    body = r'''#!/usr/bin/env python3
import json, os, sys
RUNS = json.loads(%r)
PRS = json.loads(%r)
def repo_name():
    return os.path.basename(os.getcwd())
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    print("logged in to github.com as demo")
    sys.exit(0)
if args[:2] == ["run", "list"]:
    print(json.dumps(RUNS.get(repo_name(), [])))
    sys.exit(0)
if args[:2] == ["pr", "list"]:
    print(json.dumps(PRS.get(repo_name(), [])))
    sys.exit(0)
print("[]")
sys.exit(0)
''' % (json.dumps(runs), json.dumps(prs))

    py_path = BIN / "gh_stub.py"
    py_path.write_text(body)
    if os.name == "nt":
        script.write_text('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, py_path))
    else:
        script.write_text("#!/usr/bin/env bash\nexec \"%s\" \"%s\" \"$@\"\n" % (
            sys.executable, py_path))
        # 0755: script is a POSIX shell shim other processes exec directly, so
        # it needs the execute bit; 0644 (the rule's suggested fix) would make
        # it non-runnable.
        os.chmod(script, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions


def write_git_roost_fallback():
    """henhouse.py falls back to <LEGHORN_ROOT>/git-roost/git_roost.py when
    git-roost is not on PATH -- drop the real one there unmodified."""
    dest = GITHUB_ROOT / "git-roost" / "git_roost.py"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(REAL_GIT_ROOST.read_text())


def write_registry(claims, occupancy):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps({"claims": claims, "occupancy": occupancy}))


def main():
    if DEMO_HOME.exists():
        subprocess.run(["rm", "-rf", str(DEMO_HOME)])
    DEMO_HOME.mkdir(parents=True)

    write_gh_stub()

    # Repos: web-frontend (contested: two live sessions in the same tree,
    # ahead of origin, one dirty file), tools (clean, has an open red PR),
    # api-refactor (ahead + dirty), a quiet handful for volume.
    web = make_repo("web-frontend", [
        ("Automate publishing on release", 1500),
        ("Wire up the billing webhooks", 3600),
        ("Add a retry to the flaky upload step", 9000),
    ], ahead=1, dirty_file="src/webhooks.py")
    tools = make_repo("tools", [
        ("Add field selection to the graph query", 2500),
        ("Graph Update", 61300),
    ])
    api = make_repo("api-refactor", [
        ("Add an in-program help panel", 1740),
        ("teach release_check to diff against the right base", 3900),
    ], ahead=1)
    for nm in ("docs-site", "infra-scripts", "notes"):
        make_repo(nm, [("chore: routine sync", 30000)], github=False)

    write_git_roost_fallback()

    sid_a = "sid-web-a1"
    sid_d = "sid-web-d8"
    sid_tools = "sid-tools-1"
    sid_api = "sid-api-1"

    pid_a = spawn_sleeper()
    make_session("session-a3", web, pid_a, sid_a, 40 * 60)
    make_transcript(sid_a, "claude-opus-5", 152000, 4,
                    files=(str(web / "src" / "webhooks.py"),))

    pid_d = spawn_sleeper()
    make_session("session-d8", web, pid_d, sid_d, 65 * 60)
    make_transcript(sid_d, "claude-fable-5", 88000, 6,
                    files=(str(web / "src" / "webhooks.py"),))

    pid_tools = spawn_sleeper()
    make_session("leghorn-fea", tools, pid_tools, sid_tools, 20 * 60)
    make_transcript(sid_tools, "claude-fable-5", 44000, 3,
                    files=(str(tools / "graph.py"),))

    pid_api = spawn_sleeper()
    make_session("api-refactor", api, pid_api, sid_api, 3 * 3600)
    make_transcript(sid_api, "claude-sonnet-5", 61000, 5 * 60,
                    files=(str(api / "release_check.py"),))

    write_registry(
        claims={
            sid_a: {"task": "wire up billing webhooks", "branch": "ci/npm-and-tap-publish",
                    "cwd": str(web)},
            sid_d: {"task": "wire up billing webhooks", "branch": "ci/npm-and-tap-publish",
                    "cwd": str(web)},
            sid_tools: {"task": "add field selection to graph query", "branch": "fix/fields",
                        "cwd": str(tools)},
            sid_api: {"task": "in-program help panel", "branch": "main",
                      "cwd": str(api)},
        },
        occupancy={
            str(web): {
                sid_a: {"branch": "ci/npm-and-tap-publish"},
                sid_d: {"branch": "ci/npm-and-tap-publish"},
            },
            str(tools): {sid_tools: {"branch": "fix/fields"}},
            str(api): {sid_api: {"branch": "main"}},
        },
    )

    marker = Path("/tmp/leghorn-go")
    if marker.exists():
        marker.unlink()

    def updater():
        toks = {"a": 152000, "d": 88000, "t": 44000, "p": 61000}
        t0 = time.time()
        while time.time() - t0 < 280:
            time.sleep(2.0)
            toks["a"] = min(toks["a"] + 900, 178000)
            toks["t"] = min(toks["t"] + 700, 92000)
            toks["p"] = min(toks["p"] + 500, 96000)
            with open(PROJECTS / "-home-g-GitHub" / (sid_a + ".jsonl"), "a") as fh:
                fh.write(usage_line("claude-opus-5", toks["a"]))
            with open(PROJECTS / "-home-g-GitHub" / (sid_tools + ".jsonl"), "a") as fh:
                fh.write(usage_line("claude-fable-5", toks["t"]))
            with open(PROJECTS / "-home-g-GitHub" / (sid_api + ".jsonl"), "a") as fh:
                fh.write(usage_line("claude-sonnet-5", toks["p"]))

    threading.Thread(target=updater, daemon=True).start()
    print("fleet staged at", DEMO_HOME)
    time.sleep(300)


if __name__ == "__main__":
    main()
