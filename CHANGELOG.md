# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Speed presets: `--speed` flag cycles refresh pace (ultra/fast/normal/slow) with `p` key at runtime
- Runtime commits toggle: `c` key hides/shows the commit feed; sessions and GitHub pane sit side by side when hidden
- Man page now documents all flags and keys, including `--speed`, `p`, and `c`
- README platform support table clarifying macOS/Linux/Windows installation paths
- README repository structure section mapping files and directories
- CHANGELOG documenting versions and changes
- Windows-specific test suite (`test_ccboard_windows.py`)

### Changed
- Man page version updated to 0.3
- Package version bumped to 0.3.0
- README rewritten to clarify Windows support: "macOS and Linux need nothing beyond the standard library. Windows needs `windows-curses` via pip."
- Install section now labels platforms: npm (macOS/Linux), winget (Windows), pipx/pip (all platforms)
- Man page options section expanded to document `--speed` with descriptions of each pace and its refresh intervals
- Man page keys section expanded to document `p` (cycle speed) and `c` (toggle commits)

### Fixed
- UI guard for hidden commits pane; `p` no longer sweeps GitHub when commits are toggled off
- Sort order normalization so sessions blocked on attention float correctly
- Filter and sort feedback is now instant when changed from the UI
- Detail overlay now works on commits pane
- Data age footer stays honest about which pane is refreshing

## [0.2.0] - 2026-08-02

### Added
- **Windows support** via `windows-curses` (conditional pip dependency for `sys_platform == 'win32'`)
- Windows CI matrix and test suite (`test_ccboard_windows.py`)
- PyInstaller Windows exe build: single-folder (--onedir) to avoid antivirus re-extraction issues
- Winget packaging pipeline; publish job updates winget-pkgs repository on release
- `bin/leghorn.js` now points forced Windows installs toward `winget install` or `pip install` with helpful error message
- Clean ImportError in leghorn.py when `windows-curses` is missing, with install hint
- `ccboard.py` frozen-exe detection: skips git-roost fallback under `sys.frozen` instead of re-launching exe as Python
- Repository marked public on GitHub (was private during development)

### Changed
- Release pipeline now publishes to npm, PyPI, Homebrew, apt, and winget from a single workflow
- Each publish job (npm, PyPI, winget, Homebrew, apt) fails independently without blocking the others
- npm stays POSIX-only via `package.json` `os` field; Windows gets winget/pip instead
- `bin/leghorn.js` comment clarified: npm exclusion is because npm cannot deliver pip dependencies, not because Windows cannot run curses

### Fixed
- Homebrew formula now points at a tracked release asset instead of a git reference
- CI badge restored in README

### Known Issues
- PDCurses (windows-curses backend) resize handling is incomplete (#9)
- PDCurses does not support `A_DIM` text attribute; Windows displays no dimmed text (#10)
- Session path markers in ccboard (INFRA paths) assume forward-slash only (#11)

## [0.1.0] - 2026-07-31

### Added
- Initial release: full-screen live dashboard for Claude Code sessions
- Three panes: SESSIONS (with git state), GITHUB (CI runs and PRs), COMMITS (commit feed)
- Data layer (`ccboard.py`): reads claudectl sessions, git state, GitHub via `gh`, never writes
- Install channels: Homebrew (macOS/Linux), npm (macOS/Linux), pipx/pip (macOS/Linux), apt (Debian/Ubuntu), manual deb download
- Man page with full option and key documentation
- `python3 -m ccboard` CLI for scripting and piping
- Session detail overlay showing git state, context, cost, and claim
- Help overlay explaining what each symbol and screen means
- Sorting: attention, context, dirty, name, commit age
- Filtering: all, contested, needs attention, uncommitted, claimed
- Read-only by design: never writes to trees, registries, or sessions
