class TmuxAgentSession < Formula
  include Language::Python::Virtualenv

  desc "Inspect and jump to active Codex and OpenCode tmux sessions"
  homepage "https://github.com/YOUR_GITHUB_USER/tmux-agent-session"
  url "https://github.com/YOUR_GITHUB_USER/tmux-agent-session/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "REPLACE_WITH_RELEASE_TARBALL_SHA256"

  depends_on "python@3.12"
  depends_on "tmux"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/tmux-agent-session --version")
  end
end
