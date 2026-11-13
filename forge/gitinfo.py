"""O que o git diz sobre a árvore: sha, tag exata no HEAD, branch e sujeira."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

FORGE_DIR = ".forge"


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
