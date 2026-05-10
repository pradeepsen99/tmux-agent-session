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
- Textual, which brings Rich for terminal rendering, installed automatically from the project dependencies

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

Tagged releases generate a fresh `packaging/homebrew/tmux-agent-session.rb` formula and upload it as a GitHub release asset.

To publish into a separate tap automatically, configure these repository settings:

- Repository variable `HOMEBREW_TAP_REPO`, for example `pradeepsen99/homebrew-tap`
- Optional repository variable `HOMEBREW_TAP_FORMULA_PATH`, default `Formula/tmux-agent-session.rb`
- Repository secret `TAP_GITHUB_TOKEN` with write access to the tap repo

Without those settings, the workflow still publishes the formula as a release asset so you can copy it into a tap manually.

Install from a tap with:

```bash
brew tap YOUR_GITHUB_USER/tap
brew install tmux-agent-session
```

## Releases

Two release paths are supported.

Release from GitHub Actions:

```bash
make release-dispatch VERSION=v0.2.0
```

That calls the `Release Dispatch` workflow, which validates the version in `src/tmux_agent_session/__init__.py`, creates the annotated tag, and pushes it. The tag push then triggers the `Release` workflow.

Release from a local tag:

```bash
make release-tag VERSION=v0.2.0
```

That validates the version, creates an annotated local tag, and pushes it to GitHub. The pushed tag triggers the same `Release` workflow.

The release workflow does the following:

- validates that the tag matches the package version
- runs `uv run pytest`
- builds wheel and sdist artifacts with `uv build`
- creates the GitHub release and uploads the built artifacts
- renders a Homebrew formula using the GitHub tag archive checksum
- optionally pushes that formula into a separate Homebrew tap repo

For manual checksum generation outside the workflow:

```bash
curl -L -o tmux-agent-session.tar.gz https://github.com/pradeepsen99/tmux-agent-session/archive/refs/tags/v0.1.0.tar.gz
shasum -a 256 tmux-agent-session.tar.gz
```

The CLI exposes `--version`, which gives Homebrew a stable test target:

```bash
uv run tmux-agent-session --version
```

## Usage

List active or recent sessions with the Rich-rendered table output:

```bash
uv run tmux-agent-session
```

Emit machine-readable JSON:

```bash
uv run tas --json
```

Open the Textual interactive picker and jump to the selected tmux pane:

```bash
uv run tas --pick
```

The picker supports native table navigation with arrow keys, `j`/`k`, `Enter` to focus a tmux-backed row, and `q` or `Esc` to quit.

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
