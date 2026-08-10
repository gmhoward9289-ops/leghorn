# Notes for AI agents working on leghorn

Two files, stdlib only, Python 3.9+. `leghorn.py` is the curses renderer;
`henhouse.py` is the data layer and a working CLI in its own right. Read-only by
construction: it reads session transcripts, shells out to `git` and `gh`, and
must never write to a tree, a registry or a session. Every data source is optional and degrades
to a labelled gap — "cannot see github" and "nothing is happening" must never
render identically.

Below are invariants that are easy to break and expensive to notice, each one
learned from a real bug in this repo. They are not style preferences.

## Panes: every way in needs the matching way out

A pane is reachable (`tab` focus) and indexable (`sel`/`gh_sel`/`commit_sel`)
and drawable — and those three go stale independently. Whenever you add one,
add all three, or you have built half a hazard:

1. **If the pane is not drawn this frame, bounce focus off it.** Layout is
   recomputed every frame from the terminal size, so a resize or a toggle can
   hide a pane while `focus` still names it. `j`/`k`/`enter` then drive a list
   the user cannot see. Each `if show_<pane>:` needs its
   `elif focus == "<pane>": focus = "sessions"`.
2. **Clamp the selection index outside the draw block, every frame.** A clamp
   that lives inside `if show_pane:` stops running the moment the pane hides,
   while the underlying list keeps changing underneath it.
3. **Modal overlays index the list directly.** Guarding with `and <list>`
   only proves the list is non-empty, not that a stale index is still inside
   it. A shrinking feed plus a stale index is an `IndexError` that tears down
   the whole dashboard — curses restores the terminal and the user loses the
   screen, so a crash here is worse than a wrong pixel.

This is exactly how the COMMITS detail overlay shipped broken: SESSIONS and
GITHUB had guard 1, the new pane did not, and the overlay added the indexing
that turned a cosmetic problem into a crash.

## Writing to the last column wraps into the next row

`curses.addstr` into the final cell of a line wraps onto the row below, which
here is a pane's top border, and the stray character persists across redraws
until the text shrinks again. Anything drawn on row 0 needs an explicit width
guard that stops *before* `w - 1`. `Pane.put` already clips; the header and
footer are drawn on the raw window and do not. Test at 40 columns, not 80.

Related: the header sheds its labels in a deliberate order (mode labels go
before the clock — a wall display must always answer "when did this last
update"). Preserve that fallback chain when adding fields.

## The two clocks are not interchangeable

A session collect is local git and costs milliseconds. A gh sweep is seconds of
network across every clone against a rate-limited API. That is why `SPEEDS`
holds gh at 60s even on `ultra`: below that, sweeps overlap rather than
arriving sooner, and nothing gets fresher. Any new path that wakes the gh
thread must justify it — waking it on every keypress silently defeats the
floor. `refresh_now()` (the `r` key) sweeps both because the user explicitly
asked; `set_speed` wakes gh only when the new interval is *shorter*.

Worker threads re-read their interval only after the current `wait()` returns,
so retuning a clock without waking its thread does nothing for up to six hours
under `slow`.

## Status strings are spaced; the constants are not

`henhouse.ATTENTION` holds `"needsinput"`, but a live status renders as
`"Needs Input"`. Every comparison needs
`str(status).lower().replace(" ", "")`. Missing it in `sort_key` made the
default sort silently no-op for a year of screenshots while the filter, which
normalized correctly, kept working — so it read as a UI bug, not a data one.

## Chrome that reports state must not lie

The header and footer exist to say what the dashboard is doing. If an explicit
`-i`/`--github-interval` overrides a preset, the header says `speed:custom`
rather than naming a preset whose numbers are not in effect. Prefer showing
nothing to showing a comfortable wrong answer.

## Verifying a change

`python3 -m unittest discover -s tests` is fast and headless. It cannot see
rendering, so for anything touching layout or keys, drive the real TUI in a
pty: fork one, run a **continuous drain thread** on the master fd (otherwise
the buffer fills and the app stalls), strip ANSI escapes before asserting, and
remember curses splits writes across reads — a naive substring check on the
raw stream produces false negatives. Test small terminals (24x80, 10x40) and
the layout thresholds, not just a comfortable window.

`packaging/check-version-consistency.sh` must pass before any release-shaped
change; version strings live in six places.
