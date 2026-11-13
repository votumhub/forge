"""forge restore: roda o `restore` declarado no backup de um storage."""

from __future__ import annotations

import argparse

from forge.adapters.storage import StorageAdapter
from forge.commands._common import add_target_args, load_from
from forge.commands.env import deployed_images
from forge.contracts import Executor
from forge.deploy import prepared_context
from forge.errors import ForgeError
from forge.executor import LocalExecutor
from forge.load import Loaded
from forge.state import deploy_lock


def register(verbs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = verbs.add_parser(
        "restore", help="restaura um storage pelo backup declarado"
    )
    add_target_args(parser)
    parser.add_argument(
        "storage", help="nome do storage (resources/<nome>.storage.yaml)"
    )
    parser.add_argument(
        "--yes", action="store_true", help="confirma restore em production"
    )
    parser.add_argument(
        "--force-unlock", action="store_true", help="remove lock velho antes"
    )
    parser.set_defaults(handler=run)


def restore(
    loaded: Loaded,
    name: str,
    executor: Executor,
    *,
    approved: bool,
    force_unlock: bool = False,
) -> str:
    if name not in loaded.storages:
        known = ", ".join(sorted(loaded.storages)) or "nenhum"
        raise ForgeError(f"storage {name} não existe no bundle (storages: {known})")
    if loaded.mode == "production" and not approved:
        raise ForgeError(
            f"alvo {loaded.target_name} é production: confirme o restore com --yes"
        )
    with deploy_lock(loaded.state_path, force=force_unlock):
        ctx = prepared_context(loaded, executor, deployed_images(loaded))
        return StorageAdapter().restore(ctx, name, loaded.storages[name])


def run(args: argparse.Namespace) -> int:
    print(
        restore(
            load_from(args),
            args.storage,
            LocalExecutor(),
            approved=args.yes,
            force_unlock=args.force_unlock,
        )
    )
    return 0
