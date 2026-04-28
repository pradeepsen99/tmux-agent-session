class TmuxAgentSession < Formula
  include Language::Python::Virtualenv

  desc "Inspect and jump to active Codex and OpenCode tmux sessions"
  homepage "https://github.com/pradeepsen99/tmux-agent-session"
  url "https://github.com/pradeepsen99/tmux-agent-session/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "bf58d45fe82f86c4206ead28b2d5f6abff5e5ca98c0479e02cdbb236c644aeaa"

  depends_on "python@3.12"
  depends_on "tmux"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/tmux-agent-session --version")
  end
end
