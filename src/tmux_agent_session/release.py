from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from . import __version__


DEFAULT_REPOSITORY = "pradeepsen99/tmux-agent-session"
DEFAULT_FORMULA_PATH = Path("packaging/homebrew/tmux-agent-session.rb")
HOMEBREW_PYTHON_RESOURCES = [
    (
        "linkify-it-py",
        "https://files.pythonhosted.org/packages/2e/c9/06ea13676ef354f0af6169587ae292d3e2406e212876a413bf9eece4eb23/linkify_it_py-2.1.0.tar.gz",
        "43360231720999c10e9328dc3691160e27a718e280673d444c38d7d3aaa3b98b",
    ),
    (
        "markdown-it-py",
        "https://files.pythonhosted.org/packages/06/ff/7841249c247aa650a76b9ee4bbaeae59370dc8bfd2f6c01f3630c35eb134/markdown_it_py-4.2.0.tar.gz",
        "04a21681d6fbb623de53f6f364d352309d4094dd4194040a10fd51833e418d49",
    ),
    (
        "mdit-py-plugins",
        "https://files.pythonhosted.org/packages/d8/3d/e0e8d9d1cee04f758120915e2b2a3a07eb41f8cf4654b4734788a522bcd1/mdit_py_plugins-0.6.0.tar.gz",
        "2436f14a7295837ac9228a36feeabda867c4abc488c8d019ad5c0bda88eee040",
    ),
    (
        "mdurl",
        "https://files.pythonhosted.org/packages/d6/54/cfe61301667036ec958cb99bd3efefba235e65cdeb9c84d24a8293ba1d90/mdurl-0.1.2.tar.gz",
        "bb413d29f5eea38f31dd4754dd7377d4465116fb207585f97bf925588687c1ba",
    ),
    (
        "platformdirs",
        "https://files.pythonhosted.org/packages/9f/4a/0883b8e3802965322523f0b200ecf33d31f10991d0401162f4b23c698b42/platformdirs-4.9.6.tar.gz",
        "3bfa75b0ad0db84096ae777218481852c0ebc6c727b3168c1b9e0118e458cf0a",
    ),
    (
        "pygments",
        "https://files.pythonhosted.org/packages/c3/b2/bc9c9196916376152d655522fdcebac55e66de6603a76a02bca1b6414f6c/pygments-2.20.0.tar.gz",
        "6757cd03768053ff99f3039c1a36d6c0aa0b263438fcab17520b30a303a82b5f",
    ),
    (
        "rich",
        "https://files.pythonhosted.org/packages/c0/8f/0722ca900cc807c13a6a0c696dacf35430f72e0ec571c4275d2371fca3e9/rich-15.0.0.tar.gz",
        "edd07a4824c6b40189fb7ac9bc4c52536e9780fbbfbddf6f1e2502c31b068c36",
    ),
    (
        "textual",
        "https://files.pythonhosted.org/packages/62/1e/1eedc5bac184d00aaa5f9a99095f7e266af3ec46fa926c1051be5d358da1/textual-8.2.5.tar.gz",
        "6c894e65a879dadb4f6cf46ddcfedb0173ff7e0cb1fe605ff7b357a597bdbc90",
    ),
    (
        "typing-extensions",
        "https://files.pythonhosted.org/packages/72/94/1a15dd82efb362ac84269196e94cf00f187f7ed21c242792a923cdb1c61f/typing_extensions-4.15.0.tar.gz",
        "0cea48d173cc12fa28ecabc3b837ea3cf6f38c6d1136f85cbaaf598984861466",
    ),
    (
        "uc-micro-py",
        "https://files.pythonhosted.org/packages/78/67/9a363818028526e2d4579334460df777115bdec1bb77c08f9db88f6389f2/uc_micro_py-2.0.0.tar.gz",
        "c53691e495c8db60e16ffc4861a35469b0ba0821fe409a8a7a0a71864d33a811",
    ),
]


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


def render_homebrew_resources() -> str:
    blocks = []
    for name, url, sha256 in HOMEBREW_PYTHON_RESOURCES:
        blocks.append(
            f'''  resource "{name}" do
    url "{url}"
    sha256 "{sha256}"
  end'''
        )
    return "\n\n".join(blocks)


def render_homebrew_formula(repository: str, tag: str, sha256: str) -> str:
    resources = render_homebrew_resources()
    return f'''class TmuxAgentSession < Formula
  include Language::Python::Virtualenv

  desc "Inspect and jump to active Codex and OpenCode tmux sessions"
  homepage "https://github.com/{repository}"
  url "{release_archive_url(repository, tag)}"
  sha256 "{sha256}"

  depends_on "python@3.12"
  depends_on "tmux"

{resources}

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
