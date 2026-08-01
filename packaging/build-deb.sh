#!/bin/sh
# Build a .deb for leghorn. Usage: packaging/build-deb.sh [version]
#
# Deliberately a plain dpkg-deb tree rather than a debian/ source package:
# leghorn is two architecture-independent scripts with no build step and no
# dependencies beyond python3 itself, so debhelper would add ceremony and no
# correctness. The .github/workflows/release.yml apt-repo job publishes this
# same .deb into a real signed apt repo; it is also attached to the GitHub
# release as-is for `sudo apt install ./leghorn_<version>_all.deb`.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VERSION=${1:-$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/leghorn.py")}
[ -n "$VERSION" ] || { echo "could not determine version" >&2; exit 1; }

BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT
PKG="$BUILD/leghorn_${VERSION}_all"

mkdir -p "$PKG/DEBIAN" "$PKG/usr/bin" "$PKG/usr/lib/leghorn" \
         "$PKG/usr/share/man/man1" "$PKG/usr/share/doc/leghorn"

# leghorn.py and ccboard.py stay together in /usr/lib/leghorn -- the renderer
# resolves its data layer as a sibling of its own resolved path, so /usr/bin
# carries only a symlink. resolve() follows it into the lib dir.
install -m 0755 "$ROOT/leghorn.py" "$PKG/usr/lib/leghorn/leghorn.py"
install -m 0644 "$ROOT/ccboard.py" "$PKG/usr/lib/leghorn/ccboard.py"
ln -s ../lib/leghorn/leghorn.py "$PKG/usr/bin/leghorn"

gzip -9nc "$ROOT/leghorn.1" > "$PKG/usr/share/man/man1/leghorn.1.gz"
chmod 0644 "$PKG/usr/share/man/man1/leghorn.1.gz"
install -m 0644 "$ROOT/LICENSE" "$PKG/usr/share/doc/leghorn/copyright"

cat > "$PKG/DEBIAN/control" <<EOF
Package: leghorn
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.9)
Recommends: gh, git
Maintainer: George M. Howard <dev@swamplink.com>
Homepage: https://github.com/gmhoward9289-ops/leghorn
Description: live dashboard for Claude Code sessions, git state, and GitHub CI
 A full-screen curses view you leave open on a second monitor while many
 Claude Code sessions work: every live session joined to its worktree and
 real git state, a commit feed across every repo, and a GitHub pane of CI
 runs and open PRs with failures pinned until they go green.
 .
 Reads only local Claude Code state and runs read-only git and gh. It never
 writes to a tree, a registry or a session.
EOF

dpkg-deb --build --root-owner-group "$PKG" > /dev/null
mkdir -p "$ROOT/dist"
mv "$BUILD/leghorn_${VERSION}_all.deb" "$ROOT/dist/"
echo "dist/leghorn_${VERSION}_all.deb"
