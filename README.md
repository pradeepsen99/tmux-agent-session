# tmux-agent-session

Inspect likely active Codex and OpenCode sessions and map them to tmux panes.

`tmux-agent-session` provides the full command name, and `tas` is the short alias.

## What It Does

- Detects running `codex` and `opencode` processes
- Inspects known session storage directories for both tools
- Correlates sessions by session id, cwd, and recency
- Classifies sessions as `active`, `recent`, or `stale`
- Optionally opens an interactive picker and focuses the matching tmux pane

This tool is intentionally heuristic. It does not depend on an official live session registry from either CLI.

## Requirements

- Python 3.9+
- `tmux`
- `ps`
- `lsof`

The tool works best on systems where those commands are available and session metadata is present in the default Codex/OpenCode storage locations.

## Installation

Set up the local environment with `uv`:

```bash
uv sync
```

Then run the CLI with either command name:

```bash
uv run tmux-agent-session --help
uv run tas --help
uv run python -m tmux_agent_session --help
```

## Homebrew

For Homebrew, publish tagged releases from this repository and install through a custom tap.

1. Create a GitHub release such as `v0.1.0`.
2. Copy `packaging/homebrew/tmux-agent-session.rb` into a tap repo at `Formula/tmux-agent-session.rb`.
3. Replace `YOUR_GITHUB_USER` and `REPLACE_WITH_RELEASE_TARBALL_SHA256` in the formula.
4. Install from the tap:

```bash
brew tap YOUR_GITHUB_USER/tap
brew install tmux-agent-session
```

Generate the release tarball checksum with:

```bash
curl -L -o tmux-agent-session.tar.gz https://github.com/YOUR_GITHUB_USER/tmux-agent-session/archive/refs/tags/v0.1.0.tar.gz
shasum -a 256 tmux-agent-session.tar.gz
```

The CLI exposes `--version`, which gives Homebrew a stable test target:

```bash
uv run tmux-agent-session --version
```

## Usage

List active or recent sessions:

```bash
uv run tmux-agent-session
```

Emit machine-readable JSON:

```bash
uv run tas --json
```

Open the interactive picker and jump to the selected tmux pane:

```bash
uv run tas --pick
```

Inspect a single tool:

```bash
uv run tas --tool codex
uv run tas --tool opencode
```

Show scoring reasons and include stale sessions:

```bash
uv run tas --show-reasons
uv run tas --include-stale
```

Override the default session directories when needed:

```bash
uv run tas --codex-dir ~/.codex/sessions
uv run tas --opencode-dir "~/Library/Application Support/opencode/storage"
```

## Notes

- Session matching is best-effort and based on process inspection plus session file heuristics.
- tmux focusing only works for sessions that can be mapped to a tmux pane.
- cwd detection differs by platform. Linux can use `/proc`, while macOS typically falls back to `lsof`.
