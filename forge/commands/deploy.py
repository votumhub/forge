"""forge deploy: leva o bundle ao alvo. Em production, a falha que já mexeu no ar volta
sozinha à release anterior; o comando termina em erro mesmo assim."""

from __future__ import annotations

import argparse

from forge.commands._common import add_target_args, load_from
from forge.commands.rollback import reloader
from forge.deploy import deploy_and_recover
from forge.executor import LocalExecutor


def register(verbs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = verbs.add_parser("deploy", help="leva o bundle ao alvo")
    add_target_args(parser)
    parser.add_argument(
        "--yes", action="store_true", help="confirma deploy em production"
    )
    parser.add_argument(
        "--force-unlock", action="store_true", help="remove lock velho antes"
    )
    parser.add_argument("--run", action="store_true", help=argparse.SUPPRESS)
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    loaded = load_from(args)
    print(
        f"deploy {loaded.name} -> {loaded.target_name} ({loaded.mode}), release {loaded.release}"
    )
    deploy_and_recover(
        loaded,
        LocalExecutor(),
        reload=reloader(args),
        approved=args.yes,
        force_unlock=args.force_unlock,
    )
    return 0
