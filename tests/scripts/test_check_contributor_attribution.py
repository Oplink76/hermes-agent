import os
import subprocess
from pathlib import Path

from scripts.check_contributor_attribution import check_contributors, main
from scripts.release import AUTHOR_MAP


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def commit_as(
    repo: Path,
    message: str,
    *,
    email: str,
    author: str = "Test Author",
    filename: str = "history.txt",
) -> str:
    tracked = repo / filename
    previous = tracked.read_text() if tracked.exists() else ""
    tracked.write_text(f"{previous}{message}\n")
    git(repo, "add", tracked.name)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": author,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": author,
        "GIT_COMMITTER_EMAIL": email,
    }
    git(repo, "commit", "-m", message, env=env)
    return git(repo, "rev-parse", "HEAD")


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test Committer")
    git(repo, "config", "user.email", "test@example.com")
    return repo


def make_sync_history(tmp_path: Path) -> tuple[Path, str, str, str]:
    repo = make_repo(tmp_path)
    mapped_email = next(iter(AUTHOR_MAP))
    base = commit_as(repo, "base", email=mapped_email)
    upstream_commit = commit_as(repo, "official upstream", email="official@example.com")
    git(repo, "update-ref", "refs/remotes/upstream/main", upstream_commit)
    head = commit_as(repo, "fork integration", email=mapped_email)
    return repo, base, upstream_commit, head


def test_exact_upstream_commit_is_exempt(tmp_path):
    repo, base, upstream_commit, head = make_sync_history(tmp_path)

    result = check_contributors(repo, base=base, head=head, upstream="upstream/main")

    assert result.ok is True
    assert result.exempt_upstream_commits == (upstream_commit,)
    assert result.missing == ()


def test_branch_name_does_not_exempt_fork_commit(tmp_path):
    repo, base, upstream_commit, _head = make_sync_history(tmp_path)
    fork_commit = commit_as(repo, "fork change", email="unknown@example.com")

    result = check_contributors(
        repo,
        base=base,
        head=fork_commit,
        upstream="upstream/main",
    )

    assert result.ok is False
    assert result.exempt_upstream_commits == (upstream_commit,)
    assert result.missing[0].commit == fork_commit
    assert result.missing[0].email == "unknown@example.com"


def test_missing_upstream_ref_does_not_exempt_commits(tmp_path):
    repo, base, upstream_commit, head = make_sync_history(tmp_path)

    result = check_contributors(repo, base=base, head=head, upstream="upstream/missing")

    assert result.ok is False
    assert result.exempt_upstream_commits == ()
    assert result.missing[0].commit == upstream_commit
    assert result.missing[0].email == "official@example.com"


def test_merge_commit_author_is_not_checked(tmp_path):
    repo = make_repo(tmp_path)
    mapped_email = next(iter(AUTHOR_MAP))
    base = commit_as(repo, "base", email=mapped_email)
    git(repo, "switch", "-c", "feature")
    commit_as(repo, "feature", email=mapped_email, filename="feature.txt")
    git(repo, "switch", "main")
    commit_as(repo, "mainline", email=mapped_email)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Unknown Merger",
        "GIT_AUTHOR_EMAIL": "unknown@example.com",
        "GIT_COMMITTER_NAME": "Unknown Merger",
        "GIT_COMMITTER_EMAIL": "unknown@example.com",
    }
    git(repo, "merge", "--no-ff", "feature", "-m", "merge feature", env=env)

    result = check_contributors(repo, base=base, head="HEAD", upstream=None)

    assert result.ok is True
    assert result.missing == ()


def test_author_map_email_is_mapped(tmp_path):
    repo = make_repo(tmp_path)
    mapped_email = next(iter(AUTHOR_MAP))
    base = commit_as(repo, "base", email=mapped_email)
    head = commit_as(repo, "mapped", email=mapped_email)

    result = check_contributors(repo, base=base, head=head, upstream=None)

    assert result.ok is True


def test_numbered_github_noreply_email_is_mapped_automatically(tmp_path):
    repo = make_repo(tmp_path)
    mapped_email = next(iter(AUTHOR_MAP))
    base = commit_as(repo, "base", email=mapped_email)
    head = commit_as(
        repo,
        "noreply",
        email="12345+contributor@users.noreply.github.com",
    )

    result = check_contributors(repo, base=base, head=head, upstream=None)

    assert result.ok is True


def test_cli_returns_failure_for_unmapped_author(tmp_path):
    repo = make_repo(tmp_path)
    mapped_email = next(iter(AUTHOR_MAP))
    base = commit_as(repo, "base", email=mapped_email)
    head = commit_as(repo, "unknown", email="unknown@example.com")

    exit_code = main(["--repo", str(repo), "--base", base, "--head", head])

    assert exit_code == 1


def test_cli_returns_success_when_all_authors_are_mapped(tmp_path):
    repo = make_repo(tmp_path)
    mapped_email = next(iter(AUTHOR_MAP))
    base = commit_as(repo, "base", email=mapped_email)
    head = commit_as(repo, "mapped", email=mapped_email)

    exit_code = main(["--repo", str(repo), "--base", base, "--head", head])

    assert exit_code == 0
