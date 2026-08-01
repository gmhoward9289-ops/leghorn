#!/usr/bin/env bash
# Assert every version-bearing artifact agrees with __version__ in leghorn.py.
#
# leghorn.py is the single source of truth, and most consumers already derive
# from it: build-deb.sh seds it, hatch reads it via [tool.hatch.version]. The
# man page, the Homebrew formula and package.json embed the version as literal
# text, and roost's history shows exactly how that drifts: a formula pinned to
# the previous tarball passes its own version assertion, because Homebrew
# derives `version` from the same stale URL it fetches. Checked in ci.yml so
# the drift fails on the bump PR rather than at release time.

set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' leghorn.py)
if [ -z "$VERSION" ]; then
  echo "FATAL: could not read __version__ from leghorn.py" >&2
  exit 2
fi

echo "leghorn.py __version__ = $VERSION"
fail=0

report() { # <artifact> <found> <want>
  if [ "$2" = "$3" ]; then
    printf '  ok    %-22s %s\n' "$1" "$2"
  else
    printf '  DRIFT %-22s found %-10s want %s\n' "$1" "${2:-<unparseable>}" "$3" >&2
    fail=1
  fi
}

# --- man page: .TH LEGHORN 1 "<date>" "leghorn <version>" "User Commands" -----
man_version=$(sed -n '1s/.*"leghorn \([^"]*\)".*/\1/p' leghorn.1)
report "leghorn.1 .TH header" "$man_version" "$VERSION"

# --- Homebrew formula: the tag in the source URL ------------------------------
# This is the value Homebrew turns into `version`, so it is the one that
# decides what `brew install` actually fetches.
rb_version=$(sed -n 's#.*url "https://github.com/[^"]*/archive/refs/tags/v\([^"]*\)\.tar\.gz".*#\1#p' \
             packaging/leghorn.rb)
report "leghorn.rb url tag" "$rb_version" "$VERSION"

# The refresh-checksum comment above it should point at the same tag, or the
# next person recomputes the wrong tarball's hash and "fixes" it wrongly.
rb_hint=$(sed -n 's#.*curl -sL https://github.com/[^ ]*/archive/refs/tags/v\([0-9][^ ]*\)\.tar\.gz.*#\1#p' \
          packaging/leghorn.rb | head -1)
report "leghorn.rb curl comment" "$rb_hint" "$VERSION"

# --- pyproject: version must be sourced from leghorn.py, not restated ---------
if grep -qE '^\s*version\s*=\s*"' pyproject.toml; then
  echo "  DRIFT pyproject.toml         has a literal version=; it must stay dynamic" >&2
  echo "        (keep [tool.hatch.version] path = \"leghorn.py\" as the only source)" >&2
  fail=1
else
  printf '  ok    %-22s dynamic (from leghorn.py)\n' "pyproject.toml"
fi

# --- npm package: a literal version, and the one artifact that cannot match ---
# npm rejects a two-component version, so package.json carries 0.1.0 where
# leghorn.py says 0.1. Compare against the padded form rather than exempting it.
case $VERSION in
  *.*.*) NPM_WANT=$VERSION ;;
  *.*)   NPM_WANT=$VERSION.0 ;;
  *)     NPM_WANT=$VERSION.0.0 ;;
esac
npm_version=$(sed -n 's/^[[:space:]]*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
              package.json | head -1)
report "package.json version" "$npm_version" "$NPM_WANT"

# --- --version output ---------------------------------------------------------
cli_version=$(python3 leghorn.py --version 2>&1 | sed -n 's/^leghorn\(\.py\)\{0,1\} \(.*\)$/\2/p')
report "leghorn --version" "$cli_version" "$VERSION"

if [ "$fail" -ne 0 ]; then
  cat >&2 <<EOF

Version drift. Every artifact above must say $VERSION.

  leghorn.1            .TH LEGHORN 1 "<date>" "leghorn $VERSION" "User Commands"
  package.json         "version": "$NPM_WANT"   (npm needs three components)
  packaging/leghorn.rb url ...archive/refs/tags/v$VERSION.tar.gz
                       and refresh sha256:
                         curl -sL https://github.com/gmhoward9289-ops/leghorn/archive/refs/tags/v$VERSION.tar.gz | shasum -a 256

A stale formula does not fail loudly: Homebrew derives \`version\` from the
URL, so its built-in version assertion checks the wrong number against a
tarball that agrees with it, and passes.
EOF
  exit 1
fi

echo "all version-bearing artifacts agree on $VERSION"
