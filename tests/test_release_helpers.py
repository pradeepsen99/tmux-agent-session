from __future__ import annotations

from pathlib import Path

import pytest

from tmux_agent_session import release


def test_ensure_tag_matches_version_accepts_current_version() -> None:
    assert (
        release.ensure_tag_matches_version(release.version_tag())
        == release.version_tag()
    )


def test_ensure_tag_matches_version_rejects_mismatched_tag() -> None:
    with pytest.raises(ValueError):
        release.ensure_tag_matches_version("v9.9.9")


def test_release_archive_url_uses_github_tag_archive() -> None:
    assert release.release_archive_url("owner/repo", "v1.2.3") == (
        "https://github.com/owner/repo/archive/refs/tags/v1.2.3.tar.gz"
    )


def test_sha256_digest_hashes_file_contents(tmp_path: Path) -> None:
    sample = tmp_path / "sample.txt"
    sample.write_text("hello", encoding="utf-8")

    assert release.sha256_digest(sample) == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_render_homebrew_formula_includes_release_metadata() -> None:
    formula = release.render_homebrew_formula(
        repository="owner/repo",
        tag="v1.2.3",
        sha256="abc123",
    )

    assert 'homepage "https://github.com/owner/repo"' in formula
    assert (
        'url "https://github.com/owner/repo/archive/refs/tags/v1.2.3.tar.gz"' in formula
    )
    assert 'sha256 "abc123"' in formula


def test_render_homebrew_formula_includes_python_resources() -> None:
    formula = release.render_homebrew_formula(
        repository="owner/repo",
        tag="v1.2.3",
        sha256="abc123",
    )

    for name, url, sha256 in release.HOMEBREW_PYTHON_RESOURCES:
        assert f'resource "{name}" do' in formula
        assert f'url "{url}"' in formula
        assert f'sha256 "{sha256}"' in formula
