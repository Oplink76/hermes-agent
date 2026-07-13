#!/usr/bin/env python3
"""Check that new commit authors can be attributed to GitHub users."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.release import AUTHOR_MAP


@dataclass(frozen=True)
class MissingContributor:
    commit: str
    email: str
    author: str


@dataclass(frozen=True)
class AttributionResult:
    ok: bool
    exempt_upstream_commits: tuple[str, ...]
    missing: tuple[MissingContributor, ...]


_SKIPPED_EMAIL_MARKERS = (
    "teknium",
    "noreply@github.com",
    "dependabot",
    "github-actions",
    "anthropic.com",
    "cursor.com",
)
_NUMBERED_NOREPLY = re.compile(r"\+.*@users\.noreply\.github\.com$", re.IGNORECASE)


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _git_output(repo: Path, *args: str) -> str:
    result = _run_git(repo, *args)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _is_automatically_mapped(email: str) -> bool:
    lowered = email.lower()
    return any(marker in lowered for marker in _SKIPPED_EMAIL_MARKERS) or bool(
        _NUMBERED_NOREPLY.search(email)
    )


def check_contributors(
    repo: Path, *, base: str, head: str, upstream: str | None
) -> AttributionResult:
    """Exempt only commits for which merge-base --is-ancestor COMMIT UPSTREAM succeeds."""
    repo = Path(repo)
    commits = _git_output(
        repo,
        "rev-list",
        "--reverse",
        "--no-merges",
        f"{base}..{head}",
    ).splitlines()
    exempt_upstream_commits: list[str] = []
    missing: list[MissingContributor] = []

    for commit in commits:
        if upstream is not None:
            ancestry = _run_git(repo, "merge-base", "--is-ancestor", commit, upstream)
            if ancestry.returncode == 0:
                exempt_upstream_commits.append(commit)
                continue

        metadata = _git_output(repo, "show", "-s", "--format=%H%x00%ae%x00%an", commit)
        sha, email, author = metadata.rstrip("\n").split("\0", 2)
        if email in AUTHOR_MAP or _is_automatically_mapped(email):
            continue
        missing.append(MissingContributor(commit=sha, email=email, author=author))

    return AttributionResult(
        ok=not missing,
        exempt_upstream_commits=tuple(exempt_upstream_commits),
        missing=tuple(missing),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--upstream")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = check_contributors(
            args.repo,
            base=args.base,
            head=args.head,
            upstream=args.upstream,
        )
    except RuntimeError as exc:
        print(f"Contributor attribution check failed: {exc}", file=sys.stderr)
        return 2

    if result.missing:
        print("New contributor email(s) not in AUTHOR_MAP:")
        for contributor in result.missing:
            print(
                f"  {contributor.email} ({contributor.author}, {contributor.commit[:12]})"
            )
        print("Please add mappings to scripts/release.py AUTHOR_MAP.")
        return 1

    print("All contributor emails are mapped in AUTHOR_MAP or mirrored from upstream.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
