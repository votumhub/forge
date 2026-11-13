"""forge rollback: volta à última release ok diferente da atual, sem rebuild."""

from __future__ import annotations

import argparse
from collections.abc import Callable

from forge.commands._common import add_target_args, load_from
from forge.deploy import rollback
from forge.executor import LocalExecutor
from forge.load import Loaded


def register(verbs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = verbs.add_parser(
        "rollback", help="volta à última release ok diferente da atual"
    )
    add_target_args(parser)
    parser.add_argument(
        "--yes", action="store_true", help="confirma rollback em production"
    )
    parser.add_argument(
        "--force-unlock", action="store_true", help="remove lock velho antes"
    )
    parser.set_defaults(handler=run)


def reloader(args: argparse.Namespace) -> Callable[[str], Loaded]:
    """Carrega o bundle de novo com outra release: o rollback re-aplica a release de volta."""

    def reload(release: str) -> Loaded:
        return load_from(argparse.Namespace(**{**vars(args), "release": release}))

    return reload


def run(args: argparse.Namespace) -> int:
    rollback(
        load_from(args),
        LocalExecutor(),
        reload=reloader(args),
        approved=args.yes,
        force_unlock=args.force_unlock,
    )
    return 0
