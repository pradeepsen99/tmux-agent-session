from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from . import __version__


DEFAULT_REPOSITORY = "pradeepsen99/tmux-agent-session"
DEFAULT_FORMULA_PATH = Path("packaging/homebrew/tmux-agent-session.rb")


def package_version() -> str:
    return __version__


def version_tag(version: str | None = None) -> str:
    return f"v{version or package_version()}"


def ensure_tag_matches_version(tag: str, version: str | None = None) -> str:
    expected_tag = version_tag(version)
    if tag != expected_tag:
        raise ValueError(
            f"release tag {tag!r} does not match package version {package_version()!r}; expected {expected_tag!r}"
        )
    return expected_tag


def release_archive_url(repository: str, tag: str) -> str:
    return f"https://github.com/{repository}/archive/refs/tags/{tag}.tar.gz"


def sha256_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_homebrew_formula(repository: str, tag: str, sha256: str) -> str:
    return f'''class TmuxAgentSession < Formula
  include Language::Python::Virtualenv

  desc "Inspect and jump to active Codex and OpenCode tmux sessions"
  homepage "https://github.com/{repository}"
  url "{release_archive_url(repository, tag)}"
  sha256 "{sha256}"

  depends_on "python@3.12"
  depends_on "tmux"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match version.to_s, shell_output("#{{bin}}/tmux-agent-session --version")
  end
end
'''


def write_homebrew_formula(
    repository: str,
    tag: str,
    sha256: str,
    output_path: Path = DEFAULT_FORMULA_PATH,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_homebrew_formula(repository=repository, tag=tag, sha256=sha256),
        encoding="utf-8",
    )
    return output_path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Release helpers for tmux-agent-session"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("version", help="print the package version")

    ensure_tag = subparsers.add_parser(
        "ensure-tag-match",
        help="validate that a release tag matches the package version",
    )
    ensure_tag.add_argument("--tag", required=True)

    checksum = subparsers.add_parser("sha256", help="print a file sha256 checksum")
    checksum.add_argument("--file", required=True, type=Path)

    render_formula = subparsers.add_parser(
        "render-homebrew-formula",
        help="render the Homebrew formula for a release",
    )
    render_formula.add_argument("--tag", required=True)
    render_formula.add_argument("--sha256", required=True)
    render_formula.add_argument("--repository", default=DEFAULT_REPOSITORY)
    render_formula.add_argument("--output", type=Path, default=DEFAULT_FORMULA_PATH)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "version":
            print(package_version())
            return 0

        if args.command == "ensure-tag-match":
            print(ensure_tag_matches_version(args.tag))
            return 0

        if args.command == "sha256":
            print(sha256_digest(args.file))
            return 0

        if args.command == "render-homebrew-formula":
            output_path = write_homebrew_formula(
                repository=args.repository,
                tag=args.tag,
                sha256=args.sha256,
                output_path=args.output,
            )
            print(output_path)
            return 0
    except ValueError as exc:
        parser.error(str(exc))

    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
