"""forge seguir: acompanha a rodada em curso de um job, ou a última que começou; com
`--continuo`, segue emendando cada rodada que começa depois, até Ctrl-C.

Em curso é PENDING, RUNNING, CANCELLING ou PAUSED; a rodada só agendada (SCHEDULED) fica de
fora. O acompanhamento é o mesmo do `run --seguir` (forge/commands/run.py).
"""

from __future__ import annotations

import argparse

from forge.commands._common import add_target_args, load_from
from forge.commands.run import (
    add_timeout_arg,
    api_for,
    follow_current,
    follow_stream,
    prefect_job,
    terminal,
)


def register(verbs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = verbs.add_parser(
        "seguir", help="acompanha a rodada em curso de um job, ou mostra a última"
    )
    add_target_args(parser)
    parser.add_argument("job", help="nome do job (resources/<job>.job.yaml)")
    add_timeout_arg(parser)
    parser.add_argument(
        "--continuo",
        action="store_true",
        help="não para no fim da rodada: espera a próxima do job e segue, até Ctrl-C",
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    loaded = load_from(args)
    prefect_job(loaded, args.job)
    if args.continuo:
        return follow_stream(
            loaded, args.job, reader=api_for(loaded), screen=terminal()
        )
    return follow_current(
        loaded,
        args.job,
        reader=api_for(loaded),
        screen=terminal(),
        timeout=args.timeout,
    )
