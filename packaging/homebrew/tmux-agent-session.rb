class TmuxAgentSession < Formula
  include Language::Python::Virtualenv

  desc "Inspect and jump to active Codex and OpenCode tmux sessions"
  homepage "https://github.com/pradeepsen99/tmux-agent-session"
  url "https://github.com/pradeepsen99/tmux-agent-session/archive/refs/tags/v0.1.1.tar.gz"
  sha256 "6f9cda356e17d5c37a348d6b10939d31666724e92df770d7285207b3b33132b0"

  depends_on "python@3.12"
  depends_on "tmux"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/tmux-agent-session --version")
  end
end
