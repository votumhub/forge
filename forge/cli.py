"""Linha de comando: `python -m forge <verbo>`; cada verbo mora em forge/commands/<verbo>.py."""

from __future__ import annotations

import argparse
import contextlib
import sys
import traceback
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path

from forge import VERSION
from forge.errors import ForgeError
from forge.scaffold import FORGE_REPOSITORY

VERBS = (
    "init",
    "validate",
    "plan",
    "deploy",
    "run",
    "destroy",
    "summary",
    "rollback",
    "env",
    "recipes",
    "restore",
    "seguir",
    "schema",
)
EXIT_ERROR = 2
ISSUES = f"https://{FORGE_REPOSITORY}/issues"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Leva o repositório a dev e prd a partir do forge.yaml.",
    )
    parser.add_argument("--version", action="version", version=f"forge {VERSION}")
    parser.add_argument("--root", type=Path, default=Path("."), help="raiz do bundle")
    verbs = parser.add_subparsers(dest="verb", required=True, metavar="<verbo>")
    for verb in VERBS:
        import_module(f"forge.commands.{verb}").register(verbs)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except ForgeError as error:
        _flush_stdout()
        print(f"erro: {error}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as error:
        _flush_stdout()
        traceback.print_exc()
        print(
            f"erro inesperado: {type(error).__name__}: {error}\n"
            f"isto é defeito do forge {VERSION}, não do seu repositório: abra um relato em "
            f"{ISSUES} com o comando, esta saída e o sistema operacional",
            file=sys.stderr,
        )
        return EXIT_ERROR


def _flush_stdout() -> None:
    """O progresso sai antes do erro; o leitor que já fechou o pipe não troca o erro por
    traceback."""
    with contextlib.suppress(OSError, ValueError):
        sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
