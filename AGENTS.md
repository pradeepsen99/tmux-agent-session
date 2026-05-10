# Repository Guidelines

## Project Structure & Module Organization
This repository is a small Python project managed with `uv`.

- `main.py`: minimal entry point for quick local execution.
- `session_inspector.py`: primary implementation for inspecting Codex/OpenCode sessions and tmux panes.
- `pyproject.toml`: project metadata and Python version requirement.
- `uv.lock`: locked dependency state for reproducible installs.
- `README.md`: reserved for higher-level project documentation.

Keep new modules at the repository root unless the codebase grows enough to justify a package directory.

## Build, Test, and Development Commands
- `uv sync`: create or update the local environment from `pyproject.toml` and `uv.lock`.
- `uv run python main.py`: run the placeholder entry point.
- `uv run python session_inspector.py --help`: inspect available CLI options for the main script.
- `uv run python session_inspector.py`: run the session inspector locally.

If you add tooling such as `ruff` or `pytest`, wire it through `uv run ...` and document the exact command here.

## Coding Style & Naming Conventions
Target Python `>=3.9` as declared in [`pyproject.toml`](). Follow existing style in [`session_inspector.py`](): 4-space indentation, type hints on public functions and dataclasses, and clear snake_case names for functions and variables. Use PascalCase for dataclasses and other class names.

Prefer small helper functions over deeply nested logic. Keep CLI-facing behavior explicit and avoid hidden side effects.

## Testing Guidelines
There is no test suite checked in yet. For changes to session detection or parsing logic, add focused `pytest` coverage under a future `tests/` directory using names such as `test_session_matching.py`.

Until tests exist, verify changes with:
- `uv run python session_inspector.py --help`
- `uv run python session_inspector.py`

## Commit & Pull Request Guidelines
Recent history uses short Conventional Commit prefixes, for example `feat: initial commit` and `feat: it works!`. Continue with concise messages like `fix: handle missing tmux output`.

Pull requests should describe behavior changes, list manual verification steps, and link related issues when applicable. Include terminal output or screenshots when the change affects CLI presentation or curses rendering.
