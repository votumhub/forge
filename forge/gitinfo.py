"""O que o git diz sobre a árvore: sha, tag exata no HEAD, branch e sujeira."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from forge.errors import ForgeError

FORGE_DIR = ".forge"


class GitError(ForgeError):
    pass


@dataclass(frozen=True)
class GitInfo:
    sha: str
    tag: str | None
    branch: str | None
    dirty: bool

    @property
    def short(self) -> str:
        return self.sha[:12]


def read_git(root: Path) -> GitInfo | None:
    """None quando a raiz não está num repositório git."""
    sha = _git(root, "rev-parse", "HEAD")
    if sha is None:
        return None
    return GitInfo(
        sha=sha,
        tag=_git(root, "describe", "--tags", "--exact-match", "HEAD"),
        branch=_git(root, "symbolic-ref", "--short", "-q", "HEAD"),
        dirty=_is_dirty(root),
    )


def checkout_detached(root: Path, sha: str) -> None:
    """Leva o HEAD, destacado, ao commit; o git recusa o que sobrescreveria mudança local."""
    _git_or_fail(root, "checkout", "--quiet", "--detach", sha)


def shadowed(root: Path, sha: str) -> list[str]:
    """Caminhos que o commit rastreia e o HEAD não, e que o checkout sobrescreveria: algo fora
    do git já existe no disco no caminho (ignorado, como um banco local ou um .env tirado do
    índice), ou um diretório dele é arquivo ou symlink que o HEAD não rastreia. A troca entre
    arquivo e diretório que o HEAD rastreia é do git, e passa."""
    top = Path(_git_or_fail(root, "rev-parse", "--show-toplevel").strip())
    added = _names(
        root,
        "diff",
        "-z",
        "--name-only",
        "--no-renames",
        "--diff-filter=A",
        "HEAD",
        sha,
    )
    tracked = frozenset(
        _names(root, "ls-tree", "-z", "-r", "--name-only", "--full-tree", "HEAD")
    )
    return [name for name in added if _occupied(top, name, tracked)]


def _names(root: Path, *args: str) -> list[str]:
    """Saída `-z` do git: nomes crus, separados por NUL, sem aspas nem escape."""
    return [name for name in _git_or_fail(root, *args).split("\0") if name]


def _occupied(top: Path, name: str, tracked: frozenset[str]) -> bool:
    parts = Path(name).parts
    folders = ["/".join(parts[:depth]) for depth in range(1, len(parts))]
    blocked = any(f not in tracked and _not_folder(top / f) for f in folders)
    return blocked or _untracked_at(top, name, tracked)


def _untracked_at(top: Path, name: str, tracked: frozenset[str]) -> bool:
    """No caminho há arquivo, ou diretório com algum arquivo ou symlink, que o HEAD não
    rastreia. O `os.walk` não desce em symlink de diretório: ele conta como entrada."""
    path = top / name
    if not os.path.lexists(path):
        return False
    if os.path.islink(path) or not os.path.isdir(path):
        return True
    return any(
        (Path(folder) / entry).relative_to(top).as_posix() not in tracked
        for folder, dirs, files in os.walk(path)
        for entry in [*files, *(d for d in dirs if os.path.islink(Path(folder) / d))]
    )


def _not_folder(path: Path) -> bool:
    return os.path.lexists(path) and (os.path.islink(path) or not os.path.isdir(path))


def _git_or_fail(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    if done.returncode != 0:
        raise GitError(f"git {args[0]} falhou: {done.stderr.strip()}")
    return done.stdout


def _is_dirty(root: Path) -> bool:
    status = (
        _git(root, "status", "--porcelain", "--", ".", f":(exclude){FORGE_DIR}") or ""
    )
    return bool(status.strip())


def _git(root: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None
