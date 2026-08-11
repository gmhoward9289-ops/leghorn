#!/usr/bin/env bash
# One command that answers "is leghorn actually listed everywhere, and if not,
# what exactly is left to do?" Safe to run any time: read-only against every
# channel, prints one PASS/PENDING line per channel and the exact command or
# URL for anything pending.
#
# The one-time setups it checks for (npm trusted publisher, PyPI pending
# publisher, the tap PAT) live in web UIs and cannot be scripted; this script
# exists so nobody has to remember which of them happened.
set -u

OWNER=gmhoward9289-ops
REPO=$OWNER/leghorn
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$(dirname "$0")/../leghorn.py")
PAGES=https://$OWNER.github.io/leghorn
fail=0

say()  { printf '  %-9s %-14s %s\n' "$1" "$2" "$3"; }
pend() { say PENDING "$1" "$2"; fail=1; }

echo "leghorn publish doctor -- version $VERSION"

# --- GitHub release ----------------------------------------------------------
assets=$(gh release view "v$VERSION" --repo "$REPO" --json assets --jq '.assets[].name' 2>/dev/null)
case "$assets" in
  *whl*) say PASS "gh release" "v$VERSION with $(wc -w <<<"$assets" | tr -d ' ') assets" ;;
  *) pend "gh release" "cut the tag: git tag -a v$VERSION && git push github v$VERSION" ;;
esac

# --- Homebrew ----------------------------------------------------------------
# Read the formula through the API, not raw.githubusercontent.com. raw is
# served from a CDN that caches for minutes, so straight after a release it
# hands back the PREVIOUS version and this check reports a stale tap that is
# not stale -- verified 2026-08-11, when raw said 0.4.12 while the tap's HEAD
# commit was already "leghorn v0.4.13". The API reflects the commit
# immediately. Third false alarm found in this script; a monitor that cries
# wolf is worse than no monitor.
rb=$(gh api "repos/$OWNER/homebrew-tap/contents/Formula/leghorn.rb" --jq '.content' 2>/dev/null \
     | python3 -c 'import sys,base64; print(base64.b64decode(sys.stdin.read()).decode())' 2>/dev/null)
# Match the release-asset URL, not GitHub's auto-generated archive/refs/tags/
# one. The formula moved to releases/download/ deliberately (see leghorn.rb's
# header and #8) so `brew install` counts toward the release download stats --
# and this check kept grepping for the old shape, reporting a stale formula on
# every release since. A monitor that cries wolf is worse than no monitor: it
# trains you to skim past the one time it is right.
if grep -q "download/v$VERSION/" <<<"$rb"; then
  say PASS brew "brew install $OWNER/tap/leghorn"
else
  pend brew "formula missing or stale in the tap; set TAP_PUSH_TOKEN and rerun release, or copy packaging/leghorn.rb by hand"
fi

# --- npm ---------------------------------------------------------------------
npm_ver=$(curl -sf https://registry.npmjs.org/leghorn | python3 -c 'import json,sys; print(json.load(sys.stdin)["dist-tags"]["latest"])' 2>/dev/null)
if [ "${npm_ver:-}" = "$VERSION.0" ] || [ "${npm_ver:-}" = "$VERSION" ]; then
  say PASS npm "npm i -g leghorn ($npm_ver)"
elif [ -n "${npm_ver:-}" ]; then
  pend npm "registry has $npm_ver, want $VERSION.0 -- npm publish from the repo"
else
  pend npm "first publish is manual: cd repo && npm publish (browser auth); then register the Trusted Publisher at https://www.npmjs.com/package/leghorn/access"
fi

# --- PyPI --------------------------------------------------------------------
pypi_ver=$(curl -sf https://pypi.org/pypi/leghorn/json | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null)
if [ "${pypi_ver:-}" = "$VERSION" ]; then
  say PASS pypi "pipx install leghorn ($pypi_ver)"
else
  pend pypi "register the pending publisher at https://pypi.org/manage/account/publishing/ (project leghorn, repo $REPO, workflow release.yml, environment pypi), then rerun the release's pypi job"
fi

# --- apt ---------------------------------------------------------------------
if curl -sf "$PAGES/dists/stable/InRelease" | grep -q "Origin: leghorn"; then
  ver_in_pool=$(curl -sf "$PAGES/dists/stable/main/binary-all/Packages" | sed -n 's/^Version: //p' | sort -V | tail -1)
  if [ "$ver_in_pool" = "$VERSION" ]; then
    say PASS apt "signed repo serving $ver_in_pool"
  else
    pend apt "repo serves ${ver_in_pool:-nothing}, want $VERSION -- rerun the release's apt-repo job"
  fi
else
  pend apt "no signed InRelease at $PAGES -- set LEGHORN_APT_GPG_PRIVATE_KEY and rerun the release's apt-repo job"
fi

# --- repo secrets the automation depends on ----------------------------------
# Listing secrets needs admin, and GITHUB_TOKEN does not have it -- in CI this
# can only ever report a false PENDING, which is how this script's first
# scheduled run went red while every channel was actually live. A missing
# secret already announces itself as a failed publish job, so CI skips it and
# the local run keeps the check.
if [ -n "${GITHUB_ACTIONS:-}" ]; then
  say SKIP "secret" "needs admin to list; run this locally to check secrets"
else
  secrets=$(gh secret list --repo "$REPO" 2>/dev/null)
  for s in LEGHORN_APT_GPG_PRIVATE_KEY TAP_PUSH_TOKEN; do
    if grep -q "^$s" <<<"$secrets"; then
      say PASS "secret" "$s"
    else
      pend "secret" "$s missing: gh secret set $s --repo $REPO"
    fi
  done
fi

echo
if [ "$fail" = 0 ]; then
  echo "all channels live for $VERSION"
else
  echo "PENDING items above; rerun failed release jobs afterwards with:"
  echo "  gh run rerun \$(gh run list --repo $REPO --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId') --failed --repo $REPO"
fi
exit $fail
