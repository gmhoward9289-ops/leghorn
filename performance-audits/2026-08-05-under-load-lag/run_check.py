#!/usr/bin/env python3
"""Leghorn under-load lag check — observational measurement, not a product change.

Stages a heavy synthetic fleet, puts the box under CPU load, then:
  1. Samples a live Model worker (same refresh loops as the TUI) for ~150s
  2. One-shot times sessions+git / commit_feed / github_feed
  3. A/B: full features vs --no-commits --no-github --no-git
  4. Estimates Windows tasklist cost (N subprocesses per refresh)

Writes JSON + markdown verdict under /opt/cursor/artifacts/leghorn-load-check/.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path("/workspace")
OUT = Path("/opt/cursor/artifacts/leghorn-load-check")
DEMO_HOME = Path("/tmp/leghorn-load-home")
N_REPOS = 40
N_SESSIONS = 24
SAMPLE_SECS = 150.0
LOAD_PROCS = 4  # one busy loop per core
INTERVAL = 5.0
GH_INTERVAL = 75.0

sys.path.insert(0, str(ROOT))


def write_git_roost_stub(github_root: Path) -> None:
    """Minimal git-roost that answers --json for named --root dirs.

    Real git-roost is optional; without it gather_git returns {}. A stub that
    shells out to `git status` once per root still exercises the subprocess
    path that matters under load.
    """
    dest = github_root / "git-roost" / "git_roost.py"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        r'''#!/usr/bin/env python3
import json, os, subprocess, sys, time
roots, depth, as_json = [], 1, False
args = sys.argv[1:]
i = 0
while i < len(args):
    if args[i] == "--root" and i + 1 < len(args):
        roots.append(args[i + 1]); i += 2
    elif args[i] == "--depth" and i + 1 < len(args):
        depth = int(args[i + 1]); i += 2
    elif args[i] == "--json":
        as_json = True; i += 1
    else:
        i += 1
records = []
for root in roots:
    if not os.path.isdir(os.path.join(root, ".git")) and not os.path.isfile(os.path.join(root, ".git")):
        continue
    try:
        top = subprocess.check_output(
            ["git", "-C", root, "rev-parse", "--show-toplevel"],
            text=True, stderr=subprocess.DEVNULL).strip()
        st = subprocess.check_output(
            ["git", "-C", root, "status", "--porcelain"],
            text=True, stderr=subprocess.DEVNULL)
        log = subprocess.check_output(
            ["git", "-C", root, "log", "-1", "--format=%ct%x00%s"],
            text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        continue
    last_ts, last_subj = None, ""
    if "\x00" in log:
        a, b = log.split("\x00", 1)
        if a.isdigit():
            last_ts, last_subj = int(a), b
    unstaged = sum(1 for ln in st.splitlines() if ln and ln[0] == " ")
    staged = sum(1 for ln in st.splitlines() if ln and ln[0] not in " ?")
    untracked = sum(1 for ln in st.splitlines() if ln.startswith("??"))
    records.append({
        "toplevel": top, "staged": staged, "unstaged": unstaged,
        "untracked": untracked, "ahead": 0, "behind": 0, "base": "",
        "operation": "", "last_subject": last_subj, "last_ts": last_ts,
    })
if as_json:
    print(json.dumps(records))
else:
    for r in records:
        print(r["toplevel"])
'''
    )


def stage_fleet() -> dict:
    """Heavy synthetic HOME: many clones + live session pids + stub gh."""
    if DEMO_HOME.exists():
        shutil.rmtree(DEMO_HOME)
    DEMO_HOME.mkdir(parents=True)
    github = DEMO_HOME / "GitHub"
    sessions = DEMO_HOME / ".claude" / "sessions"
    projects = DEMO_HOME / ".claude" / "projects" / "-load-check"
    state = DEMO_HOME / "Claude" / "worktrees"
    bin_dir = DEMO_HOME / "bin"
    for d in (github, sessions, projects, state, bin_dir):
        d.mkdir(parents=True)

    now = time.time()
    sleepers = []

    def make_repo(name: str, n_commits: int = 8, github_remote: bool = True) -> Path:
        repo = github / name
        repo.mkdir(parents=True)
        bare = DEMO_HOME / "remotes" / (name + ".git")
        bare.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "load",
            "GIT_AUTHOR_EMAIL": "load@example.com",
            "GIT_COMMITTER_NAME": "load",
            "GIT_COMMITTER_EMAIL": "load@example.com",
        }
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        url = ("https://github.com/demo/%s.git" % name) if github_remote else str(bare)
        subprocess.run(["git", "remote", "add", "origin", url], cwd=repo, check=True)
        subprocess.run(["git", "remote", "add", "pushmirror", str(bare)],
                       cwd=repo, check=True)
        for i in range(n_commits):
            (repo / "NOTES.md").write_text("commit %d in %s\n" % (i, name))
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True,
                           capture_output=True)
            ts = str(int(now - (i + 1) * 600))
            subprocess.run(
                ["git", "commit", "-q", "-m", "chore(%s): step %d" % (name, i),
                 "--date", ts],
                cwd=repo, check=True, capture_output=True,
                env={**env, "GIT_AUTHOR_DATE": ts, "GIT_COMMITTER_DATE": ts},
            )
        subprocess.run(["git", "push", "-q", "pushmirror", "main"],
                       cwd=repo, check=True, capture_output=True)
        return repo

    print("staging %d repos..." % N_REPOS, flush=True)
    repos = []
    for i in range(N_REPOS):
        # First half look like github.com origins so github_feed fans out.
        repos.append(make_repo("repo-%02d" % i, github_remote=(i < N_REPOS // 2)))

    write_git_roost_stub(github)

    # Stub gh: auth ok, empty runs/prs (still pays spawn + auth + N*2 calls).
    gh_py = bin_dir / "gh_stub.py"
    gh_py.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "args = sys.argv[1:]\n"
        "time.sleep(0.05)  # pretend a little network\n"
        "if args[:2] == ['auth', 'status']:\n"
        "    print('logged in to github.com as loadcheck'); sys.exit(0)\n"
        "print('[]')\n"
    )
    gh = bin_dir / "gh"
    gh.write_text("#!/usr/bin/env bash\nexec python3 %s \"$@\"\n" % gh_py)
    os.chmod(gh, 0o755)

    claims, occupancy = {}, {}
    print("staging %d sessions..." % N_SESSIONS, flush=True)
    for i in range(N_SESSIONS):
        repo = repos[i % len(repos)]
        p = subprocess.Popen(["sleep", "7200"], stdout=subprocess.DEVNULL)
        sleepers.append(p)
        sid = "sid-load-%02d" % i
        (sessions / ("%d.json" % p.pid)).write_text(json.dumps({
            "pid": p.pid,
            "sessionId": sid,
            "cwd": str(repo),
            "name": "sess-%02d" % i,
            "startedAt": int((now - 600 * (i + 1)) * 1000),
        }))
        # Transcript with enough bytes that the tail read is real work.
        lines = [
            json.dumps({"type": "user", "message": {"role": "user"}}),
            json.dumps({
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-5",
                    "usage": {
                        "input_tokens": 40,
                        "cache_read_input_tokens": 40000 + i * 1000,
                        "cache_creation_input_tokens": 200,
                    },
                },
            }),
        ]
        # Pad so TAIL_BYTES path is exercised.
        pad = json.dumps({"type": "user", "message": {"role": "user",
                         "content": "x" * 200}}) + "\n"
        body = (pad * 80) + "\n".join(lines) + "\n"
        (projects / (sid + ".jsonl")).write_text(body)
        claims[sid] = {"task": "load-check work %d" % i, "branch": "main",
                       "cwd": str(repo)}
        occupancy.setdefault(str(repo), {})[sid] = {"branch": "main"}

    (state / "registry.json").write_text(json.dumps({
        "claims": claims, "occupancy": occupancy,
    }))

    return {
        "home": str(DEMO_HOME),
        "repos": N_REPOS,
        "github_repos": N_REPOS // 2,
        "sessions": N_SESSIONS,
        "sleeper_pids": [p.pid for p in sleepers],
        "sleepers": sleepers,
    }


def env_for_fleet(bin_first: bool = True) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(DEMO_HOME)
    env["USERPROFILE"] = str(DEMO_HOME)
    env["LEGHORN_ROOT"] = str(DEMO_HOME / "GitHub")
    env["CCWORK_SESSIONS_DIR"] = str(DEMO_HOME / ".claude" / "sessions")
    env["CCWORK_STATE_DIR"] = str(DEMO_HOME / "Claude" / "worktrees")
    env["CLAUDE_PROJECTS_DIR"] = str(DEMO_HOME / ".claude" / "projects")
    path = str(DEMO_HOME / "bin") + os.pathsep + env.get("PATH", "")
    env["PATH"] = path
    return env


def start_cpu_load(n: int = LOAD_PROCS):
    """Burn CPU with tight loops (no disk) so Leghorn contends for cores."""
    procs = []
    for _ in range(n):
        procs.append(subprocess.Popen(
            [sys.executable, "-c",
             "while True:\n"
             "    x = 0\n"
             "    for i in range(100000):\n"
             "        x += i * i\n"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
    return procs


def stop_procs(procs):
    for p in procs:
        try:
            p.send_signal(signal.SIGTERM)
        except OSError:
            pass
    for p in procs:
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()


def read_proc_stat(pid: int):
    """Return (utime+stime jiffies, rss_kb) or None if gone."""
    try:
        with open("/proc/%d/stat" % pid) as fh:
            fields = fh.read().split()
        # utime=13, stime=14 (0-indexed after comm handling — fields[13], [14]
        # after splitting where comm is field 1 in parentheses).
        # Safer: read from /proc/pid/stat with the known layout after ')'.
        with open("/proc/%d/stat" % pid) as fh:
            raw = fh.read()
        rest = raw[raw.rfind(")") + 2 :].split()
        utime = int(rest[11])
        stime = int(rest[12])
        with open("/proc/%d/status" % pid) as fh:
            rss_kb = 0
            for line in fh:
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                    break
        return utime + stime, rss_kb
    except (OSError, ValueError, IndexError):
        return None


def count_children_kinds(pid: int) -> dict:
    """Count live children by basename (git/gh/python/sleep/...)."""
    counts = {}
    try:
        for name in os.listdir("/proc/%d/task" % pid):
            pass
    except OSError:
        return counts
    # Walk /proc for children whose PPid == pid
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % entry) as fh:
                raw = fh.read()
            rest = raw[raw.rfind(")") + 2 :].split()
            ppid = int(rest[1])
            if ppid != pid:
                # also count grandchildren of worker threads? threads share pid.
                continue
            with open("/proc/%s/comm" % entry) as fh:
                comm = fh.read().strip()
            counts[comm] = counts.get(comm, 0) + 1
        except (OSError, ValueError, IndexError):
            continue
    return counts


def sample_model_worker(duration: float, use_git: bool, want_commits: bool,
                        want_github: bool, label: str) -> dict:
    """Run Model in-process under the fleet env; sample self CPU / children."""
    # Apply fleet env before importing path-sensitive coop constants? coop
    # already imported REPOS_ROOT at import time — re-bind after env set.
    env = env_for_fleet()
    for k, v in env.items():
        os.environ[k] = v

    import importlib
    import coop as cb
    importlib.reload(cb)
    import leghorn as lh
    importlib.reload(lh)

    model = lh.Model(INTERVAL, use_git, want_commits, want_github, GH_INTERVAL)
    worker = threading.Thread(target=model.run, daemon=True)
    worker.start()
    gh_worker = None
    if want_github:
        gh_worker = threading.Thread(target=model.run_github, daemon=True)
        gh_worker.start()

    pid = os.getpid()
    hz = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    t0 = time.time()
    prev = read_proc_stat(pid)
    samples = []
    child_peaks = {}
    # Also track transient children via /proc sampling at high frequency.
    while time.time() - t0 < duration:
        time.sleep(0.5)
        now = time.time()
        cur = read_proc_stat(pid)
        if prev and cur:
            dj = cur[0] - prev[0]
            dt = 0.5
            cpu_pct = 100.0 * (dj / hz) / dt
            samples.append({
                "t": round(now - t0, 2),
                "cpu_pct": round(cpu_pct, 2),
                "rss_mb": round(cur[1] / 1024.0, 2),
                "busy": model.busy,
            })
        prev = cur
        kids = count_children_kinds(pid)
        for k, v in kids.items():
            child_peaks[k] = max(child_peaks.get(k, 0), v)

    model.stop()
    time.sleep(0.3)
    rows, commits, updated, warn, error, loading, busy = model.snapshot()
    gh_events, gh_warn, gh_updated = model.snapshot_github()

    cpu_vals = [s["cpu_pct"] for s in samples] or [0.0]
    rss_vals = [s["rss_mb"] for s in samples] or [0.0]
    return {
        "label": label,
        "duration_s": duration,
        "use_git": use_git,
        "want_commits": want_commits,
        "want_github": want_github,
        "cpu_avg_pct": round(statistics.mean(cpu_vals), 2),
        "cpu_p95_pct": round(sorted(cpu_vals)[max(0, int(len(cpu_vals) * 0.95) - 1)], 2),
        "cpu_max_pct": round(max(cpu_vals), 2),
        "rss_avg_mb": round(statistics.mean(rss_vals), 2),
        "rss_max_mb": round(max(rss_vals), 2),
        "child_peaks": child_peaks,
        "n_rows": len(rows),
        "n_commits": len(commits),
        "n_gh_events": len(gh_events),
        "warn": warn,
        "gh_warn": gh_warn,
        "error": error,
        "samples_n": len(samples),
        # Keep a thin sample trail for the report, not every half-second.
        "sample_trail": samples[:: max(1, len(samples) // 20)],
    }


def time_paths() -> dict:
    env = env_for_fleet()
    for k, v in env.items():
        os.environ[k] = v
    import importlib
    import coop as cb
    importlib.reload(cb)

    out = {"repos_on_disk": 0, "github_repos": 0, "sessions": 0}
    root = cb.REPOS_ROOT
    if root.is_dir():
        repos = [p for p in root.iterdir() if (p / ".git").exists()]
        out["repos_on_disk"] = len(repos)
        out["github_repos"] = len(cb.github_repos())
    out["sessions"] = len(cb.load_sessions())

    def timed(name, fn):
        t0 = time.perf_counter()
        result = fn()
        elapsed = time.perf_counter() - t0
        return name, elapsed, result

    results = {}

    def sessions_git():
        sessions = cb.load_sessions()
        telemetry, warn = cb.load_transcripts(sessions)
        claims, occupancy = cb.load_registry()
        rows = cb.build(telemetry, claims, occupancy, sessions, use_git=True)
        return len(rows), warn

    name, elapsed, pair = timed("sessions_plus_git", sessions_git)
    n, warn = pair
    results[name] = {"seconds": round(elapsed, 3), "n_rows": n, "warn": warn}

    name, elapsed, commits = timed("commit_feed", lambda: cb.commit_feed(40))
    results[name] = {"seconds": round(elapsed, 3), "n": len(commits)}

    name, elapsed, pair = timed("github_feed", lambda: cb.github_feed(40))
    events, warn = pair
    results[name] = {"seconds": round(elapsed, 3), "n": len(events), "warn": warn}

    # Light path (A/B stripped).
    def sessions_only():
        sessions = cb.load_sessions()
        telemetry, warn = cb.load_transcripts(sessions)
        claims, occupancy = cb.load_registry()
        rows = cb.build(telemetry, claims, occupancy, sessions, use_git=False)
        return len(rows)

    name, elapsed, n = timed("sessions_no_git", sessions_only)
    results[name] = {"seconds": round(elapsed, 3), "n_rows": n}

    # Windows tasklist cost estimate: spawn N short subprocesses like alive().
    def fake_tasklist_cost():
        n = out["sessions"] or N_SESSIONS
        t0 = time.perf_counter()
        for _ in range(n):
            subprocess.run(
                ["true"], capture_output=True, text=True, timeout=10,
            )
        return time.perf_counter() - t0

    # More realistic: each call is `ps -p PID` which is closer to tasklist cost
    # than `true` (still cheaper than Windows tasklist, but a lower bound).
    def posix_alive_cost():
        sessions = cb.load_sessions()
        # load_sessions already called alive; time only the probes.
        pids = []
        for path in sorted(cb.SESSIONS_DIR.glob("*.json")):
            try:
                s = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(s.get("pid"), int):
                pids.append(s["pid"])
        t0 = time.perf_counter()
        for pid in pids:
            cb.alive(pid)
        return time.perf_counter() - t0, len(pids)

    # Simulate Windows: N × `ps -p` (one process list filter each) — better
    # proxy than os.kill(0).
    def windows_style_alive_proxy():
        pids = []
        for path in sorted(cb.SESSIONS_DIR.glob("*.json")):
            try:
                s = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(s.get("pid"), int):
                pids.append(s["pid"])
        t0 = time.perf_counter()
        for pid in pids:
            subprocess.run(
                ["ps", "-p", str(pid), "-o", "pid="],
                capture_output=True, text=True, timeout=10,
            )
        return time.perf_counter() - t0, len(pids)

    elapsed, n = posix_alive_cost()
    results["alive_posix_os_kill"] = {"seconds": round(elapsed, 3), "n": n}
    elapsed, n = windows_style_alive_proxy()
    results["alive_windows_style_proxy_ps"] = {
        "seconds": round(elapsed, 3), "n": n,
        "note": "N× `ps -p PID` per refresh — lower bound vs Windows tasklist",
    }

    out["timings"] = results
    out["interval_s"] = INTERVAL
    out["github_interval_s"] = GH_INTERVAL
    return out


def machine_snapshot() -> dict:
    load1, load5, load15 = os.getloadavg()
    mem = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith(("MemTotal:", "MemAvailable:", "MemFree:")):
                    k, v, *_ = line.split()
                    mem[k[:-1]] = int(v)
    except OSError:
        pass
    return {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "cpus": os.cpu_count(),
        "loadavg": [load1, load5, load15],
        "mem_kb": mem,
    }


def write_report(report: dict) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    json_path = OUT / "results.json"
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n")

    full = report["sample_full"]
    light = report["sample_light"]
    timings = report["path_timings"]["timings"]
    verdict = report["verdict"]

    lines = []
    lines.append("# Leghorn under-load lag check")
    lines.append("")
    lines.append("**Verdict: %s**" % verdict["label"])
    lines.append("")
    lines.append(verdict["summary"])
    lines.append("")
    lines.append("## Conditions")
    lines.append("")
    lines.append("- Platform: %s, %d CPUs, Python %s" % (
        report["machine"]["platform"], report["machine"]["cpus"],
        report["machine"]["python"]))
    lines.append("- Synthetic fleet: **%d repos** (%d github.com), **%d live sessions**" % (
        report["fleet"]["repos"], report["fleet"]["github_repos"],
        report["fleet"]["sessions"]))
    lines.append("- CPU load: %d busy-loop processes during sampling" % LOAD_PROCS)
    lines.append("- Model clocks: session/git/commits every %.0fs, github every %.0fs" % (
        INTERVAL, GH_INTERVAL))
    lines.append("- Loadavg during check: %s" % (
        ", ".join("%.2f" % x for x in report["machine_under_load"]["loadavg"])))
    lines.append("")
    lines.append("## 1. Live worker sampling (~%.0fs each)" % SAMPLE_SECS)
    lines.append("")
    lines.append("| Mode | CPU avg | CPU p95 | CPU max | RSS avg | RSS max | rows | commits | gh |")
    lines.append("|------|---------|---------|---------|---------|---------|------|---------|-----|")
    for s in (full, light):
        lines.append(
            "| %s | %.1f%% | %.1f%% | %.1f%% | %.1f MB | %.1f MB | %d | %d | %d |" % (
                s["label"], s["cpu_avg_pct"], s["cpu_p95_pct"], s["cpu_max_pct"],
                s["rss_avg_mb"], s["rss_max_mb"], s["n_rows"], s["n_commits"],
                s["n_gh_events"],
            )
        )
    lines.append("")
    lines.append("Child-process peaks seen during full mode: `%s`" % (
        json.dumps(full.get("child_peaks") or {})))
    lines.append("")
    lines.append("## 2. One-shot path timings (under same CPU load)")
    lines.append("")
    lines.append("| Path | Seconds | Notes |")
    lines.append("|------|---------|-------|")
    for key, meta in timings.items():
        note = meta.get("note") or ("n=%s" % meta.get("n", meta.get("n_rows", "?")))
        if "warn" in meta and meta["warn"]:
            note += "; warn=%s" % meta["warn"]
        lines.append("| `%s` | **%.3f** | %s |" % (key, meta["seconds"], note))
    lines.append("")
    lines.append("Refresh budgets: fast loop %.0fs, gh loop %.0fs." % (
        INTERVAL, GH_INTERVAL))
    lines.append("")
    lines.append("## 3. A/B: full vs stripped")
    lines.append("")
    delta = full["cpu_avg_pct"] - light["cpu_avg_pct"]
    lines.append(
        "Full features vs `--no-commits --no-github --no-git`: "
        "CPU avg **%+.1f pp** (full %.1f%% → light %.1f%%)." % (
            delta, full["cpu_avg_pct"], light["cpu_avg_pct"])
    )
    lines.append("")
    lines.append("## 4. Windows-specific note")
    lines.append("")
    win = timings.get("alive_windows_style_proxy_ps", {})
    posix = timings.get("alive_posix_os_kill", {})
    lines.append(
        "On Linux, `alive()` is `os.kill(pid, 0)` (%.3fs for %s pids). "
        "Windows uses one `tasklist` subprocess per pid. A `ps -p` proxy "
        "for the same N cost **%.3fs** — real `tasklist` is typically slower. "
        "At %d sessions every %ds this is the main Windows-only risk; "
        "it was not exercised as native tasklist in this Linux environment." % (
            posix.get("seconds", 0), posix.get("n", "?"),
            win.get("seconds", 0),
            report["fleet"]["sessions"], int(INTERVAL),
        )
    )
    lines.append("")
    lines.append("## Verdict detail")
    lines.append("")
    for bullet in verdict["bullets"]:
        lines.append("- %s" % bullet)
    lines.append("")
    lines.append("Raw data: `results.json`")
    lines.append("")

    md_path = OUT / "VERDICT.md"
    md_path.write_text("\n".join(lines))
    return md_path


def decide_verdict(full, light, path_timings) -> dict:
    timings = path_timings["timings"]
    fast_budget = INTERVAL
    slow_budget = GH_INTERVAL
    sessions_git = timings["sessions_plus_git"]["seconds"]
    commits = timings["commit_feed"]["seconds"]
    github = timings["github_feed"]["seconds"]
    fast_total = sessions_git + commits  # mirrors Model._collect

    bullets = []
    # Heuristics from the plan.
    low_cpu = full["cpu_avg_pct"] < 8.0
    brief_spikes = full["cpu_p95_pct"] < 40.0
    memory_flat = (full["rss_max_mb"] - full["rss_avg_mb"]) < 30.0
    under_budget = fast_total < fast_budget and github < slow_budget
    ab_delta = full["cpu_avg_pct"] - light["cpu_avg_pct"]

    bullets.append(
        "Full-mode CPU avg %.1f%% (p95 %.1f%%, max %.1f%%) over %.0fs under "
        "machine loadavg %s." % (
            full["cpu_avg_pct"], full["cpu_p95_pct"], full["cpu_max_pct"],
            SAMPLE_SECS,
            ", ".join("%.2f" % x for x in os.getloadavg()),
        )
    )
    bullets.append(
        "Fast collect wall time %.3fs (sessions+git %.3fs + commit_feed %.3fs) "
        "vs %.0fs interval; github_feed %.3fs vs %.0fs interval." % (
            fast_total, sessions_git, commits, fast_budget,
            github, slow_budget,
        )
    )
    bullets.append(
        "A/B CPU delta full→light: %+.1f percentage points "
        "(light avg %.1f%%)." % (ab_delta, light["cpu_avg_pct"])
    )
    bullets.append(
        "RSS stayed ~%.0f–%.0f MB (no leak signal in the sample window)." % (
            full["rss_avg_mb"], full["rss_max_mb"])
    )

    if low_cpu and under_budget and ab_delta < 5.0:
        label = "ruled out"
        summary = (
            "Leghorn is **not** a meaningful lag contributor under this load. "
            "Average CPU stayed low-single-digit, refresh work finished inside "
            "its intervals, and stripping commits/github/git barely moved CPU. "
            "Machine lag under similar conditions is elsewhere."
        )
    elif under_budget and full["cpu_avg_pct"] < 20.0:
        label = "minor contributor"
        summary = (
            "Leghorn adds measurable but small cost under load — brief refresh "
            "spikes and some child-process churn — but stays within its poll "
            "budgets. Unlikely to be the primary cause of system lag; only "
            "worth a light-mode flag or longer intervals if the box is already "
            "CPU-starved."
        )
        bullets.append(
            "Consider `leghorn --no-commits` or a slower speed preset if the "
            "machine is already pegged."
        )
    else:
        label = "worth fixing"
        summary = (
            "Leghorn's refresh work overlaps its interval or holds "
            "non-trivial CPU under load. Investigate the dominant path "
            "(see timings) before leaving the dashboard up on a busy box."
        )
        if not under_budget:
            if fast_total >= fast_budget:
                bullets.append(
                    "Fast loop overran its interval — commit_feed or "
                    "sessions+git is the suspect."
                )
            if github >= slow_budget:
                bullets.append(
                    "github_feed overran its interval — gh fan-out is the suspect."
                )

    if not (low_cpu and brief_spikes and memory_flat):
        bullets.append(
            "Pass-heuristic check: low_cpu=%s brief_spikes=%s memory_flat=%s." % (
                low_cpu, brief_spikes, memory_flat)
        )

    return {"label": label, "summary": summary, "bullets": bullets,
            "heuristics": {
                "low_cpu": low_cpu,
                "brief_spikes": brief_spikes,
                "memory_flat": memory_flat,
                "under_budget": under_budget,
                "ab_delta_pp": round(ab_delta, 2),
                "fast_total_s": round(fast_total, 3),
            }}


def sample_live_tui(duration: float, extra_args=None, label="live-tui") -> dict:
    """Run real leghorn.py in a pty under the fleet env; sample that PID."""
    import pty
    env = env_for_fleet()
    env["TERM"] = "xterm-256color"
    env["COLUMNS"] = "120"
    env["LINES"] = "40"
    args = [sys.executable, str(ROOT / "leghorn.py"), "-i", str(INTERVAL)]
    if extra_args:
        args.extend(extra_args)

    master, slave = pty.openpty()
    proc = subprocess.Popen(
        args, stdin=slave, stdout=slave, stderr=slave,
        env=env, close_fds=True,
    )
    os.close(slave)

    # Drain pty in background so the buffer never fills and stalls curses.
    def drain():
        try:
            while True:
                data = os.read(master, 8192)
                if not data:
                    break
        except OSError:
            pass

    threading.Thread(target=drain, daemon=True).start()

    # Wait until the process is alive and has had one collect cycle.
    time.sleep(min(3.0, INTERVAL + 1.0))
    pid = proc.pid
    hz = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    t0 = time.time()
    prev = read_proc_stat(pid)
    samples = []
    child_peaks = {}
    while time.time() - t0 < duration:
        time.sleep(0.5)
        if proc.poll() is not None:
            break
        cur = read_proc_stat(pid)
        if prev and cur:
            dj = cur[0] - prev[0]
            cpu_pct = 100.0 * (dj / hz) / 0.5
            samples.append({
                "t": round(time.time() - t0, 2),
                "cpu_pct": round(cpu_pct, 2),
                "rss_mb": round(cur[1] / 1024.0, 2),
            })
        prev = cur
        for k, v in count_children_kinds(pid).items():
            child_peaks[k] = max(child_peaks.get(k, 0), v)

    try:
        os.write(master, b"q")
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        os.close(master)
    except OSError:
        pass

    cpu_vals = [s["cpu_pct"] for s in samples] or [0.0]
    rss_vals = [s["rss_mb"] for s in samples] or [0.0]
    return {
        "label": label,
        "duration_s": round(time.time() - t0, 2),
        "exit_code": proc.returncode,
        "cpu_avg_pct": round(statistics.mean(cpu_vals), 2),
        "cpu_p95_pct": round(sorted(cpu_vals)[max(0, int(len(cpu_vals) * 0.95) - 1)], 2),
        "cpu_max_pct": round(max(cpu_vals), 2),
        "rss_avg_mb": round(statistics.mean(rss_vals), 2),
        "rss_max_mb": round(max(rss_vals), 2),
        "child_peaks": child_peaks,
        "samples_n": len(samples),
        "sample_trail": samples[:: max(1, len(samples) // 20)],
        "args": args[2:],
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print("=== machine baseline ===", flush=True)
    baseline = machine_snapshot()
    print(json.dumps(baseline, indent=2), flush=True)

    print("=== stage fleet ===", flush=True)
    fleet = stage_fleet()
    # Drop the Popen objects from the serialisable copy.
    fleet_meta = {k: v for k, v in fleet.items() if k != "sleepers"}

    print("=== start CPU load (%d procs) ===" % LOAD_PROCS, flush=True)
    loaders = start_cpu_load(LOAD_PROCS)
    time.sleep(2)
    under = machine_snapshot()
    print("loadavg under stress:", under["loadavg"], flush=True)

    try:
        print("=== path timings (under load) ===", flush=True)
        path_timings = time_paths()
        print(json.dumps(path_timings, indent=2), flush=True)

        print("=== sample LIVE TUI full %.0fs ===" % SAMPLE_SECS, flush=True)
        full = sample_live_tui(SAMPLE_SECS, extra_args=None, label="live-tui-full")
        print("full cpu_avg=%.1f%% p95=%.1f%% max=%.1f%% rss=%.1fMB exit=%s" % (
            full["cpu_avg_pct"], full["cpu_p95_pct"], full["cpu_max_pct"],
            full["rss_avg_mb"], full["exit_code"]), flush=True)

        print("=== sample LIVE TUI light %.0fs ===" % SAMPLE_SECS, flush=True)
        light = sample_live_tui(
            SAMPLE_SECS,
            extra_args=["--no-commits", "--no-github", "--no-git"],
            label="live-tui-light (--no-commits --no-github --no-git)",
        )
        print("light cpu_avg=%.1f%% p95=%.1f%% max=%.1f%% rss=%.1fMB exit=%s" % (
            light["cpu_avg_pct"], light["cpu_p95_pct"], light["cpu_max_pct"],
            light["rss_avg_mb"], light["exit_code"]), flush=True)

        # Also keep in-process Model samples as a secondary signal (no curses).
        print("=== sample Model worker full (60s) ===", flush=True)
        model_full = sample_model_worker(
            60.0, use_git=True, want_commits=True, want_github=True,
            label="model-full-60s",
        )
        print("=== sample Model worker light (60s) ===", flush=True)
        model_light = sample_model_worker(
            60.0, use_git=False, want_commits=False, want_github=False,
            label="model-light-60s",
        )
    finally:
        print("=== stop CPU load ===", flush=True)
        stop_procs(loaders)
        stop_procs(fleet["sleepers"])

    # Prefer live-TUI numbers for the verdict; fall back if TUI failed to sample.
    verdict_full = full if full.get("samples_n", 0) >= 10 else model_full
    verdict_light = light if light.get("samples_n", 0) >= 10 else model_light
    # Attach row counts from model sample when TUI has none.
    if "n_rows" not in verdict_full:
        verdict_full = dict(verdict_full)
        verdict_full["n_rows"] = model_full.get("n_rows", 0)
        verdict_full["n_commits"] = model_full.get("n_commits", 0)
        verdict_full["n_gh_events"] = model_full.get("n_gh_events", 0)
    if "n_rows" not in verdict_light:
        verdict_light = dict(verdict_light)
        verdict_light["n_rows"] = model_light.get("n_rows", 0)
        verdict_light["n_commits"] = model_light.get("n_commits", 0)
        verdict_light["n_gh_events"] = model_light.get("n_gh_events", 0)

    verdict = decide_verdict(verdict_full, verdict_light, path_timings)
    report = {
        "machine": baseline,
        "machine_under_load": under,
        "fleet": fleet_meta,
        "path_timings": path_timings,
        "sample_full": verdict_full,
        "sample_light": verdict_light,
        "sample_live_tui_full_raw": full,
        "sample_live_tui_light_raw": light,
        "sample_model_full": model_full,
        "sample_model_light": model_light,
        "verdict": verdict,
    }
    md = write_report(report)
    print("=== VERDICT: %s ===" % verdict["label"], flush=True)
    print(verdict["summary"], flush=True)
    print("wrote", md, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
