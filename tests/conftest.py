from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

from tests.support import Writer

REPO = Path(__file__).resolve().parents[1]
ROOM = REPO.parent.parent / (REPO.name + "-room")
"""Fora da arvore do repositorio: dentro dela, o `git check-ignore` das fixtures
passaria a enxergar o .gitignore do proprio forge."""


@pytest.fixture(autouse=True, scope="session")
def room_for_wheels() -> None:
    """O /tmp do runner e pequeno demais para um venv inteiro (mmap falha no meio).

    TMPDIR e UV_CACHE_DIR sao as alavancas que o pytest e o `uv` respeitam sem parametro; a sala
    fica fora do repositorio (o check-ignore das fixtures nao a enxerga) e fora do home, que num
    runner restrito recusa escrita: sem cache proprio o uv morre de Permission denied em vez de rodar.
    """
    import os
    import tempfile

    ROOM.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TMPDIR", str(ROOM))
    os.environ.setdefault("UV_CACHE_DIR", str(ROOM.joinpath("uv-cache")))
    tempfile.tempdir = os.environ["TMPDIR"]


@pytest.fixture
def write_bundle(tmp_path: Path) -> Writer:
    """Escreve forge.yaml e recursos ({"resources/x.service.yaml": {...}}) em tmp_path."""

    def write(
        manifest: Mapping[str, object], files: Mapping[str, object] | None = None
    ) -> Path:
        (tmp_path / "forge.yaml").write_text(
            yaml.safe_dump(dict(manifest)), encoding="utf-8"
        )
        for relative, content in (files or {}).items():
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            text = content if isinstance(content, str) else yaml.safe_dump(content)
            path.write_text(text, encoding="utf-8")
        return tmp_path

    return write
