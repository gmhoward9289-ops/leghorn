# Homebrew formula for leghorn.
#
# This is the master copy; the release workflow copies it to Formula/leghorn.rb
# in the tap repo (gmhoward9289-ops/homebrew-tap) with a freshly computed
# sha256, which is what `brew install` reads. It lives here so the formula is
# versioned alongside the code it builds.
#
# homebrew-core is not an option yet -- it requires notability thresholds
# (stars/forks/watchers) that this project has not met.
#
# After tagging a release, refresh the checksum with:
#   curl -sL https://github.com/gmhoward9289-ops/leghorn/archive/refs/tags/v0.3.tar.gz | shasum -a 256
class Leghorn < Formula
  include Language::Python::Shebang

  desc "Live dashboard for Claude Code sessions, git state, and GitHub CI"
  homepage "https://github.com/gmhoward9289-ops/leghorn"
  url "https://github.com/gmhoward9289-ops/leghorn/archive/refs/tags/v0.3.tar.gz"
  sha256 "PLACEHOLDER_FILLED_BY_RELEASE_WORKFLOW"
  license "MIT"

  depends_on "python@3.13"

  def install
    # The renderer resolves its data layer as a sibling of its own resolved
    # path, so both files live in libexec and bin carries only a symlink --
    # bin/leghorn -> libexec/leghorn.py, resolve() follows it home.
    libexec.install "leghorn.py", "coop.py"
    # The shipped shebang is `/usr/bin/env python3`, which would resolve to
    # whatever python happens to be first on PATH -- including a virtualenv the
    # user activated for something else. Pin it to the formula's interpreter.
    rewrite_shebang detected_python_shebang(use_python_from_path: false), libexec/"leghorn.py"
    chmod 0755, libexec/"leghorn.py"
    bin.install_symlink libexec/"leghorn.py" => "leghorn"
    man1.install "leghorn.1"
  end

  test do
    assert_match "leghorn #{version}", shell_output("#{bin}/leghorn --version")
    # The data layer must be importable from the installed layout, and its CLI
    # must degrade to a table (or an empty-state line) with no sessions present.
    assert_match(/session|SESSION/i, shell_output("#{libexec}/leghorn.py --help"))
  end
end
