"""Command-line boundary for CloudAdvisor Hermes operations."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import yaml

from .command import CommandRunner, SubprocessCommandRunner
from .sync import SyncConfig, SyncResult, SyncState, run as run_sync


def load_sync_config(path: Path) -> SyncConfig:
    raw = yaml.safe_load(path.expanduser().read_text(encoding="utf-8")) or {}
    values = raw.get("sync")
    if not isinstance(values, dict):
        raise ValueError("operations config must contain a 'sync' mapping")
    required = {
        "repo",
        "worktree",
        "origin",
        "upstream",
        "candidate_branch",
        "repo_slug",
    }
    missing = sorted(required - set(values))
    if missing:
        raise ValueError(f"sync config is missing required fields: {missing}")
    lock_path = values.get("lock_path")
    kwargs = {
        "repo": Path(values["repo"]).expanduser().resolve(strict=False),
        "worktree": Path(values["worktree"]).expanduser().resolve(strict=False),
        "origin": str(values["origin"]),
        "upstream": str(values["upstream"]),
        "candidate_branch": str(values["candidate_branch"]),
        "repo_slug": str(values["repo_slug"]),
    }
    if lock_path is not None:
        kwargs["lock_path"] = Path(lock_path).expanduser().resolve(strict=False)
    return SyncConfig(**kwargs)


class GhGitHub:
    def __init__(self, repo_slug: str, runner: CommandRunner, cwd: Path):
        self.repo_slug = repo_slug
        self.runner = runner
        self.cwd = cwd

    def _run(self, argv: list[str]):
        completed = self.runner.run(argv, cwd=self.cwd, timeout=300)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"GitHub CLI failed: {detail}")
        return completed

    def find_open_pull_request(self, head: str, base: str) -> int | None:
        completed = self._run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                self.repo_slug,
                "--head",
                head,
                "--base",
                base,
                "--state",
                "open",
                "--json",
                "number",
                "--limit",
                "2",
            ]
        )
        rows = json.loads(completed.stdout or "[]")
        if len(rows) > 1:
            raise RuntimeError("more than one open upstream sync PR")
        return int(rows[0]["number"]) if rows else None

    def create_pull_request(self, *, head: str, base: str, title: str, body: str) -> int:
        completed = self._run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                self.repo_slug,
                "--head",
                head,
                "--base",
                base,
                "--title",
                title,
                "--body",
                body,
            ]
        )
        url = (completed.stdout or "").strip().rstrip("/")
        try:
            return int(url.rsplit("/", maxsplit=1)[1])
        except (IndexError, ValueError) as exc:
            raise RuntimeError(f"could not parse created PR number from {url!r}") from exc

    def update_pull_request(self, number: int, *, title: str, body: str) -> None:
        self._run(
            [
                "gh",
                "pr",
                "edit",
                str(number),
                "--repo",
                self.repo_slug,
                "--title",
                title,
                "--body",
                body,
            ]
        )


def _sync_payload(result: SyncResult) -> dict[str, object]:
    return {
        "state": result.state.value,
        "base_sha": result.base_sha,
        "upstream_sha": result.upstream_sha,
        "candidate_sha": result.candidate_sha,
        "pr_number": result.pr_number,
        "checks": [asdict(check) for check in result.checks],
        "risk": result.risk,
        "changed_files": list(result.changed_files),
        "transitions": [state.value for state in result.transitions],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync_parser = subparsers.add_parser("sync", help="prepare or update the upstream PR")
    sync_parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "sync":
        config = load_sync_config(args.config)
        runner = SubprocessCommandRunner()
        github = GhGitHub(config.repo_slug, runner, config.repo)
        result = run_sync(config, runner=runner, github=github)
        print(json.dumps(_sync_payload(result), indent=2, sort_keys=True))
        if result.state in {SyncState.NO_CHANGE, SyncState.PR_UPDATED}:
            return 0
        if result.state is SyncState.LOCKED:
            return 75
        if result.state is SyncState.VERIFY_FAILED:
            return 3
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
