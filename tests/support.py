from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

Writer = Callable[..., Path]


def manifest(**extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "forge": 2,
        "bundle": {"name": "toy"},
        "targets": {
            "dev": {"mode": "development", "default": True},
            "prd": {"mode": "production", "git": {"branch": "main"}},
        },
    }
    base.update(extra)
    return base
