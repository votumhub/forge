"""Argumentos e carga compartilhados pelos verbos que operam sobre um alvo."""

from __future__ import annotations

import argparse

from forge.errors import ForgeError
from forge.load import Loaded, load


def add_target_args(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    parser.add_argument("-t", "--target", required=required, help="alvo do forge.yaml")
    parser.add_argument(
        "--var",
        action="append",
        default=[],
        metavar="NOME=VALOR",
        help="valor de variável",
    )
    parser.add_argument(
        "--release", help="id da release (padrão: tag exata ou sha curto)"
    )


def cli_vars(pairs: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name:
            raise ForgeError(f"--var {pair!r}: use NOME=VALOR")
        values[name] = value
    return values


def load_from(args: argparse.Namespace, target: str | None = None) -> Loaded:
    return load(
        args.root,
        target if target is not None else args.target,
        cli_vars=cli_vars(args.var),
        release=args.release,
    )
