SHELL := /bin/sh

PYTHONPATH := src
RELEASE_DISPATCH_WORKFLOW := release-dispatch.yml

.PHONY: test build version check-release check-clean tag push-tag release-tag release-dispatch

test:
	uv sync --group dev
	uv run pytest

build:
	uv build

version:
	@PYTHONPATH=$(PYTHONPATH) uv run python -m tmux_agent_session.release version

check-release:
	@test -n "$(VERSION)" || { printf '%s\n' "VERSION is required, for example VERSION=v0.2.0"; exit 1; }
	@PYTHONPATH=$(PYTHONPATH) uv run python -m tmux_agent_session.release ensure-tag-match --tag "$(VERSION)"

check-clean:
	@git diff --quiet && git diff --cached --quiet || { printf '%s\n' "Working tree has tracked changes; commit or stash them before creating a release tag."; exit 1; }

tag: check-release check-clean
	@if git rev-parse "$(VERSION)" >/dev/null 2>&1; then printf '%s\n' "Tag $(VERSION) already exists locally."; exit 1; fi
	git tag -a "$(VERSION)" -m "Release $(VERSION)"

push-tag:
	@test -n "$(VERSION)" || { printf '%s\n' "VERSION is required, for example VERSION=v0.2.0"; exit 1; }
	git push origin "$(VERSION)"

release-tag: tag push-tag

release-dispatch: check-release
	gh workflow run "$(RELEASE_DISPATCH_WORKFLOW)" -f version="$(VERSION)"
