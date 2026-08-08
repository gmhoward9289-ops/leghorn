#!/usr/bin/env bash
# Bump the patch component of __version__ in leghorn.py and sync every other
# version-bearing artifact. Called by the daily-release workflow (and usable
# by hand: packaging/bump-patch.sh).
#
# 0.4   -> 0.4.1   (first patch after a two-component alpha bump)
# 0.4.0 -> 0.4.1
# 0.4.1 -> 0.4.2

set -euo pipefail
cd "$(dirname "$0")/.."

CURRENT=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' leghorn.py)
if [ -z "$CURRENT" ]; then
  echo "FATAL: could not read __version__ from leghorn.py" >&2
  exit 2
fi

NEXT=$(python3 - <<PY
import sys
parts = "$CURRENT".split(".")
while len(parts) < 3:
    parts.append("0")
major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
print(f"{major}.{minor}.{patch + 1}")
PY
)

echo "bumping $CURRENT -> $NEXT"

sed -i "s/^__version__ = \".*\"/__version__ = \"$NEXT\"/" leghorn.py

MONTH=$(date +%B)
YEAR=$(date +%Y)
sed -i "1s/.*/.TH LEGHORN 1 \"$MONTH $YEAR\" \"leghorn $NEXT\" \"User Commands\"/" leghorn.1

case $NEXT in
  *.*.*) NPM=$NEXT ;;
  *.*)   NPM=$NEXT.0 ;;
  *)     NPM=$NEXT.0.0 ;;
esac
sed -i "s/^\([[:space:]]*\"version\"[[:space:]]*:[[:space:]]*\)\"[^\"]*\"/\1\"$NPM\"/" package.json

sed -i "s#releases/download/v[^/]*/leghorn-[^/]*\.tar\.gz#releases/download/v$NEXT/leghorn-$NEXT.tar.gz#g" packaging/leghorn.rb
sed -i "s/^  version \".*\"/  version \"$NEXT\"/" packaging/leghorn.rb

packaging/check-version-consistency.sh
echo "ready to commit and tag v$NEXT"
