"""Dispatcher-side dependency provisioning for isolated project worktrees.

Governed workers must not install dependencies. This module is called by the
dispatcher before a worker process is spawned so Node verification commands can
run without widening worker authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import IO, Any, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX is the supported dispatcher host
    fcntl = None  # type: ignore[assignment]


_NODE_LOCKFILE_NAMES = ("npm-shrinkwrap.json", "package-lock.json")
_UNSUPPORTED_LOCKFILE_NAMES = (
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "bun.lock",
    ".pnp.cjs",
    ".pnp.js",
)
_NODE_SKIP_DIRS = {
    ".git",
    ".worktrees",
    "node_modules",
    "dist",
    "build",
    ".next",
    ".turbo",
}
_PROVISION_MARKER = ".hermes-provisioned-node-dependencies.json"
_INSTALL_TIMEOUT_SECONDS = 15 * 60


def _expand_workspace_braces(pattern: str) -> list[str]:
    """Expand npm/minimatch brace alternatives without silent under-matching."""
    # pathlib glob syntax is not npm minimatch syntax. Reject forms whose
    # meaning differs rather than silently omitting workspace members (and
    # therefore their manifests/lifecycle requirements).
    if "\\" in pattern or "[^" in pattern or "[:" in pattern:
        raise RuntimeError(f"unsupported npm workspace pattern {pattern!r}")
    start = pattern.find("{")
    if start < 0:
        if "}" in pattern or any(token in pattern for token in ("!(", "+(", "@(", "?(", "*(")):
            raise RuntimeError(f"unsupported npm workspace pattern {pattern!r}")
        return [pattern]

    depth = 0
    end = -1
    for index in range(start, len(pattern)):
        char = pattern[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    if end < 0:
        raise RuntimeError(f"unsupported npm workspace pattern {pattern!r}")

    body = pattern[start + 1 : end]
    alternatives: list[str] = []
    chunk_start = 0
    nested = 0
    for index, char in enumerate(body):
        if char == "{":
            nested += 1
        elif char == "}":
            nested -= 1
        elif char == "," and nested == 0:
            alternatives.append(body[chunk_start:index])
            chunk_start = index + 1
    alternatives.append(body[chunk_start:])
    if len(alternatives) < 2 or any(not item for item in alternatives):
        raise RuntimeError(f"unsupported npm workspace pattern {pattern!r}")

    expanded: list[str] = []
    for alternative in alternatives:
        expanded.extend(
            _expand_workspace_braces(
                pattern[:start] + alternative + pattern[end + 1 :]
            )
        )
    return expanded



def _read_package_manager(package_json: Path) -> Optional[str]:
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    value = data.get("packageManager") if isinstance(data, dict) else None
    if not isinstance(value, str):
        return None
    manager = value.split("@", 1)[0].strip().lower()
    return manager or None


def _applicable_lockfile(project_dir: Path) -> Optional[Path]:
    """Return this npm project's lockfile, if present."""
    for name in _NODE_LOCKFILE_NAMES:
        path = project_dir / name
        if path.is_file():
            return path
    return None


def _workspace_package_dirs(project_dir: Path) -> list[Path]:
    """Return package directories owned by an npm workspace root."""
    try:
        data = json.loads((project_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    raw = data.get("workspaces") if isinstance(data, dict) else None
    if isinstance(raw, dict):
        raw = raw.get("packages")
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RuntimeError(f"unsupported npm workspaces declaration in {project_dir}")

    members: set[Path] = set()
    for pattern in raw:
        if not pattern or pattern.startswith("!") or Path(pattern).is_absolute():
            raise RuntimeError(
                f"unsupported npm workspace pattern {pattern!r} in {project_dir}"
            )
        for expanded_pattern in _expand_workspace_braces(pattern):
            for candidate in project_dir.glob(expanded_pattern):
                candidate = candidate.resolve(strict=False)
                try:
                    relative_candidate = candidate.relative_to(project_dir)
                except ValueError:
                    continue
                if (
                    candidate.is_dir()
                    and (candidate / "package.json").is_file()
                    and not any(
                        part in _NODE_SKIP_DIRS for part in relative_candidate.parts
                    )
                ):
                    members.add(candidate)
    return sorted(members, key=lambda path: path.as_posix())


def _node_project_dirs(root: Path) -> list[Path]:
    """Find npm project roots without descending into generated trees."""
    root = root.resolve(strict=False)
    package_dirs: list[Path] = []
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in _NODE_SKIP_DIRS)
        current_path = Path(current)
        if "package.json" in filenames:
            package_dirs.append(current_path)

    if not package_dirs:
        return []

    selected: list[Path] = []
    workspace_members: set[Path] = set()
    for project_dir in package_dirs:
        if project_dir in workspace_members:
            continue
        if (
            project_dir == root
            or _applicable_lockfile(project_dir) is not None
            or any(
                (project_dir / name).exists() for name in _UNSUPPORTED_LOCKFILE_NAMES
            )
        ):
            selected.append(project_dir)
            workspace_members.update(_workspace_package_dirs(project_dir))
    return selected


def _manifest_state(project_dir: Path) -> dict[str, Any]:
    package_bytes = (project_dir / "package.json").read_bytes()
    lockfile = _applicable_lockfile(project_dir)
    lock_bytes = lockfile.read_bytes() if lockfile is not None else None
    return {
        "package_sha256": hashlib.sha256(package_bytes).hexdigest(),
        "lockfile": lockfile.name if lockfile is not None else None,
        "lock_sha256": hashlib.sha256(lock_bytes).hexdigest() if lock_bytes else None,
        "workspace_packages": {
            member.relative_to(project_dir).as_posix(): hashlib.sha256(
                (member / "package.json").read_bytes()
            ).hexdigest()
            for member in _workspace_package_dirs(project_dir)
        },
    }


def _manifests_match(source_dir: Path, target_dir: Path) -> bool:
    source_package = source_dir / "package.json"
    target_package = target_dir / "package.json"
    source_lockfile = _applicable_lockfile(source_dir)
    target_lockfile = _applicable_lockfile(target_dir)
    if (
        not source_package.is_file()
        or not target_package.is_file()
        or source_lockfile is None
        or target_lockfile is None
        or source_lockfile.name != target_lockfile.name
    ):
        return False
    try:
        return _manifest_state(source_dir) == _manifest_state(target_dir)
    except (OSError, RuntimeError):
        return False


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _node_modules_marker(node_modules: Path) -> Path:
    return node_modules / _PROVISION_MARKER


def _write_marker(node_modules: Path, state: dict[str, Any]) -> None:
    _node_modules_marker(node_modules).write_text(
        json.dumps(state, sort_keys=True) + "\n", encoding="utf-8"
    )


def _copy_source_is_isolated(source_root: Path, node_modules: Path) -> bool:
    """Reject dependency trees whose links escape or remain absolute."""
    try:
        source_root = source_root.resolve()
        for current, dirnames, filenames in os.walk(node_modules):
            for name in [*dirnames, *filenames]:
                candidate = Path(current) / name
                if not candidate.is_symlink():
                    continue
                raw_target = Path(os.readlink(candidate))
                if raw_target.is_absolute():
                    return False
                resolved = candidate.resolve()
                if not resolved.is_relative_to(source_root):
                    return False
        return True
    except OSError:
        return False


def _installed_dependencies_match_lock(project_dir: Path) -> bool:
    """Return whether lock-declared installed package versions are present."""
    lockfile = _applicable_lockfile(project_dir)
    if lockfile is None:
        return False
    try:
        data = json.loads(lockfile.read_text(encoding="utf-8"))
        packages = data.get("packages") if isinstance(data, dict) else None
        if not isinstance(packages, dict):
            return False
        for package_path, expected in packages.items():
            if (
                not package_path
                or "node_modules" not in Path(package_path).parts
                or not isinstance(expected, dict)
                or expected.get("link") is True
            ):
                continue
            manifest = project_dir / package_path / "package.json"
            if not manifest.is_file():
                if expected.get("optional") is True or expected.get("devOptional") is True:
                    continue
                return False
            installed = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(installed, dict) or installed.get("version") != expected.get("version"):
                return False
        return True
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


def _validate_supported_project(project_dir: Path) -> None:
    manager = _read_package_manager(project_dir / "package.json")
    unsupported_lock = next(
        (name for name in _UNSUPPORTED_LOCKFILE_NAMES if (project_dir / name).exists()),
        None,
    )
    if manager not in {None, "npm"} or unsupported_lock is not None:
        detected = manager or unsupported_lock or "unknown"
        raise RuntimeError(
            f"unsupported Node dependency layout in {project_dir}: {detected}; "
            "dispatcher provisioning currently supports npm lockfiles only"
        )
    if _applicable_lockfile(project_dir) is None:
        raise RuntimeError(
            f"cannot deterministically provision Node dependencies in {project_dir}: "
            "package-lock.json or npm-shrinkwrap.json is required"
        )


def _ensure_dependency_tree_is_untracked_and_ignored(
    worktree_root: Path, node_modules: Path
) -> None:
    """Fail before provisioning when Git could stage generated dependencies."""
    relative = node_modules.relative_to(worktree_root).as_posix()
    tracked = subprocess.run(
        ["git", "-C", str(worktree_root), "ls-files", "--", relative],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if tracked.returncode != 0:
        raise RuntimeError(
            f"cannot verify Git tracking state for dependency tree {node_modules}"
        )
    if (tracked.stdout or "").strip():
        raise RuntimeError(
            f"refusing to provision tracked dependency tree {node_modules}"
        )

    ignored = subprocess.run(
        [
            "git",
            "-C",
            str(worktree_root),
            "check-ignore",
            "--quiet",
            "--no-index",
            "--",
            relative + "/",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if ignored.returncode == 1:
        raise RuntimeError(
            f"refusing to provision unignored dependency tree {node_modules}; "
            "the repository must ignore generated node_modules"
        )
    if ignored.returncode != 0:
        raise RuntimeError(
            f"cannot verify Git ignore rules for dependency tree {node_modules}"
        )


def _install_command(project_dir: Path) -> list[str]:
    from hermes_constants import find_node_executable

    executable = find_node_executable("npm")
    if not executable:
        raise RuntimeError(
            f"npm is required to provision Node dependencies in {project_dir}"
        )
    return [executable, "ci", "--include=dev", "--ignore-scripts"]


def _scripted_dependency_is_safe_without_lifecycle_scripts(
    package_path: str, package: dict
) -> bool:
    """Return whether one known optional watcher remains safe script-free."""
    return (
        package_path == "node_modules/fsevents"
        and package.get("version") == "2.3.3"
        and package.get("dev") is True
        and package.get("optional") is True
        and package.get("os") == ["darwin"]
    )


def _install_requires_lifecycle_scripts(project_dir: Path) -> bool:
    lockfile = _applicable_lockfile(project_dir)
    if lockfile is None:
        raise RuntimeError(f"npm lockfile disappeared during provisioning in {project_dir}")
    try:
        data = json.loads(lockfile.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot validate npm lockfile {lockfile}: {exc}") from exc
    packages = data.get("packages") if isinstance(data, dict) else None
    if not isinstance(packages, dict):
        raise RuntimeError(f"cannot validate npm lockfile packages in {lockfile}")
    return any(
        package_path
        and isinstance(package, dict)
        and package.get("hasInstallScript") is True
        and not _scripted_dependency_is_safe_without_lifecycle_scripts(
            package_path, package
        )
        for package_path, package in packages.items()
    )


_NPM_LIFECYCLE_SCRIPTS = {
    "preinstall",
    "install",
    "postinstall",
    "prepublish",
    "preprepare",
    "prepare",
    "postprepare",
}


def _project_has_binding_gyp(project_dir: Path) -> bool:
    """Return whether npm would implicitly run node-gyp in this workspace."""
    return any(
        (package_dir / "binding.gyp").is_file()
        for package_dir in [project_dir, *_workspace_package_dirs(project_dir)]
    )


def _project_requires_lifecycle_scripts(project_dir: Path) -> bool:
    """Return whether script-free npm ci would omit required project setup."""
    for package_dir in [project_dir, *_workspace_package_dirs(project_dir)]:
        try:
            data = json.loads(
                (package_dir / "package.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"cannot validate npm package manifest in {package_dir}: {exc}"
            ) from exc
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if isinstance(scripts, dict) and any(
            name in scripts for name in _NPM_LIFECYCLE_SCRIPTS
        ):
            return True
    # npm implicitly runs ``node-gyp rebuild`` as install when binding.gyp
    # exists, even if package.json declares no install script.
    return _project_has_binding_gyp(project_dir) or _install_requires_lifecycle_scripts(
        project_dir
    )


def _run_real_install(project_dir: Path) -> None:
    """Run one deterministic, script-free install in dispatcher context."""
    from tools.environments.local import build_subprocess_env

    env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=False)
    env.pop("HERMES_KANBAN_TASK", None)
    env.pop("HERMES_KANBAN_WORKSPACE", None)
    env["CI"] = "1"
    command = _install_command(project_dir)
    try:
        result = subprocess.run(
            command,
            cwd=project_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_INSTALL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Node dependency install failed in {project_dir}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError(
            f"Node dependency install failed in {project_dir}: "
            f"{detail[-1] if detail else f'exit {result.returncode}'}"
        )


class _ProjectLock:
    def __init__(self, path: Path, handle: IO[str]) -> None:
        self.path = path
        self.handle = handle

    def release(self) -> None:
        if self.handle.closed:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()


def _acquire_project_lock(project_dir: Path) -> _ProjectLock:
    if fcntl is None:
        raise RuntimeError("safe Node dependency locking requires POSIX flock support")
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    lock_root = hermes_home / "locks" / "node-dependencies"
    lock_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(
        str(project_dir.resolve(strict=False)).encode("utf-8")
    ).hexdigest()
    marker = lock_root / f"{digest}.lock"
    handle = marker.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError(f"Node dependency provisioning already active in {project_dir}")
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\n")
    handle.flush()
    return _ProjectLock(marker, handle)


def provision_node_dependencies(primary_root: Path, worktree_root: Path) -> None:
    """Provision every supported npm project in a materialized worktree.

    Primary dependencies are copied only when package.json and the npm lockfile
    are byte-identical and the source tree contains no escaping links. Any
    mismatch uses a single script-free npm install in dispatcher context.
    """
    primary_root = primary_root.expanduser().resolve(strict=False)
    worktree_root = worktree_root.expanduser().resolve(strict=False)
    if primary_root == worktree_root:
        return

    touched: list[Path] = []
    held_locks: list[_ProjectLock] = []
    try:
        for target_dir in _node_project_dirs(worktree_root):
            _validate_supported_project(target_dir)
            relative = target_dir.relative_to(worktree_root)
            source_dir = primary_root / relative
            target_package_dirs = [target_dir, *_workspace_package_dirs(target_dir)]
            for package_dir in target_package_dirs:
                target_node_modules = package_dir / "node_modules"
                _ensure_dependency_tree_is_untracked_and_ignored(
                    worktree_root, target_node_modules
                )
                held_locks.append(_acquire_project_lock(package_dir))

            state = _manifest_state(target_dir)
            source_node_modules = source_dir / "node_modules"
            copy_from_primary = (
                _manifests_match(source_dir, target_dir)
                and source_node_modules.is_dir()
                and _installed_dependencies_match_lock(source_dir)
                and all(
                    _copy_source_is_isolated(
                        primary_root,
                        primary_root
                        / package_dir.relative_to(worktree_root)
                        / "node_modules",
                    )
                    for package_dir in target_package_dirs
                    if (
                        primary_root
                        / package_dir.relative_to(worktree_root)
                        / "node_modules"
                    ).is_dir()
                )
            )
            if _project_has_binding_gyp(target_dir):
                raise RuntimeError(
                    f"dependencies in {target_dir} require lifecycle scripts; "
                    "refusing to omit implicit node-gyp build output"
                )
            if copy_from_primary:
                for package_dir in target_package_dirs:
                    target_node_modules = package_dir / "node_modules"
                    source_tree = (
                        primary_root
                        / package_dir.relative_to(worktree_root)
                        / "node_modules"
                    )
                    _remove_path(target_node_modules)
                    touched.append(target_node_modules)
                    if source_tree.is_dir():
                        shutil.copytree(
                            source_tree, target_node_modules, symlinks=True
                        )
            else:
                if _project_requires_lifecycle_scripts(target_dir):
                    raise RuntimeError(
                        f"dependencies in {target_dir} require lifecycle scripts; "
                        "refusing to mark a script-free install as build-ready"
                    )
                for package_dir in target_package_dirs:
                    target_node_modules = package_dir / "node_modules"
                    _remove_path(target_node_modules)
                    touched.append(target_node_modules)
                _run_real_install(target_dir)

            # npm may omit node_modules entirely for a dependency-free package.
            # Create the directory so the provenance marker still has a stable
            # home and cleanup can distinguish dispatcher-owned state.
            target_node_modules = target_dir / "node_modules"
            target_node_modules.mkdir(exist_ok=True)
            for package_dir in target_package_dirs:
                target_node_modules = package_dir / "node_modules"
                if not target_node_modules.exists():
                    continue
                if not target_node_modules.is_dir():
                    raise RuntimeError(
                        f"dependency install did not create {target_node_modules}"
                    )
                if not _copy_source_is_isolated(worktree_root, target_node_modules):
                    raise RuntimeError(
                        f"provisioned dependency links escape worktree {worktree_root}"
                    )
                _write_marker(target_node_modules, state)

        release_errors: list[str] = []
        for lock_marker in held_locks:
            try:
                lock_marker.release()
            except OSError as exc:
                release_errors.append(f"{lock_marker.path}: {exc}")
        held_locks.clear()
        if release_errors:
            raise RuntimeError(
                "dependency provisioning completed but lock release failed: "
                + "; ".join(release_errors)
            )
    except Exception:
        for node_modules in sorted(
            set(touched), key=lambda path: len(path.parts), reverse=True
        ):
            try:
                _remove_path(node_modules)
            except OSError:
                pass
        for lock_marker in reversed(held_locks):
            try:
                lock_marker.release()
            except OSError:
                pass
        raise


def cleanup_provisioned_node_dependencies(worktree_root: Path) -> None:
    """Remove dependency trees and lock files created by this dispatcher."""
    worktree_root = worktree_root.expanduser().resolve(strict=False)
    if not worktree_root.is_dir():
        return

    provisioned: list[Path] = []
    for current, dirnames, filenames in os.walk(worktree_root):
        dirnames[:] = sorted(
            name for name in dirnames if name not in {".git", ".worktrees"}
        )
        current_path = Path(current)
        if current_path.name == "node_modules":
            if _PROVISION_MARKER in filenames:
                provisioned.append(current_path)
            dirnames[:] = []

    for node_modules in sorted(
        set(provisioned), key=lambda path: len(path.parts), reverse=True
    ):
        lock_marker: Optional[_ProjectLock] = None
        try:
            lock_marker = _acquire_project_lock(node_modules.parent)
            _remove_path(node_modules)
        except (OSError, RuntimeError):
            pass
        finally:
            if lock_marker is not None:
                lock_marker.release()
