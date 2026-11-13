"""forge destroy: derruba serviços e deployments do bundle no alvo e remove as imagens que o
forge construiu para ele. Só development; storage só sai com --data."""

from __future__ import annotations

import argparse

from forge.commands._common import add_target_args, load_from
from forge.commands.env import deployed_images
from forge.contracts import DeployContext, Executor
from forge.artifacts import remove_built_images
from forge.deploy import Adapters, default_adapters, prepared_context, reachable
from forge.errors import ForgeError
from forge.executor import LocalExecutor
from forge.load import Loaded
from forge.state import deploy_lock, read_state, state_file


def register(verbs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = verbs.add_parser(
        "destroy", help="derruba o que o bundle subiu (só development)"
    )
    add_target_args(parser)
    parser.add_argument(
        "--force-unlock", action="store_true", help="remove lock velho antes"
    )
    parser.add_argument(
        "--data", action="store_true", help="remove também os volumes (storage)"
    )
    parser.set_defaults(handler=run)


def destroy(
    loaded: Loaded,
    executor: Executor,
    *,
    force_unlock: bool = False,
    adapters: Adapters | None = None,
    data: bool = False,
) -> list[str]:
    if loaded.mode != "development":
        raise ForgeError(
            f"alvo {loaded.target_name} é {loaded.mode}: destroy só existe em development"
        )
    chosen = adapters or default_adapters(loaded)
    done = _nothing_recorded(loaded)
    with deploy_lock(loaded.state_path, force=force_unlock):
        ctx = prepared_context(loaded, executor, deployed_images(loaded))
        for name, job in loaded.flow_jobs.items():
            chosen.job.destroy(ctx, name, job)
            done.append(f"job {name}: deployment removido")
        # routes saem antes dos serviços: o proxy que recarrega pode ser serviço do bundle
        done += chosen.route.remove(ctx, loaded.name, loaded.target.proxy)
        for name, service in loaded.services.items():
            chosen.service.destroy(ctx, name, service, data=data)
            done.append(f"serviço {name}: derrubado")
        done += _storages(ctx, loaded, chosen, data)
        done += remove_built_images(loaded, executor, _built_here(loaded))
    return done


def _nothing_recorded(loaded: Loaded) -> list[str]:
    target = loaded.target_name
    if read_state(loaded.state_path, target).current is not None:
        return []
    return [
        f"alvo {target}: nenhuma release ok em {state_file(loaded.state_path, target).as_posix()}; "
        f"o forge não tinha nada de pé aqui, o down abaixo só confirma. Para subir: "
        f"forge deploy -t {target}"
    ]


def _built_here(loaded: Loaded) -> set[str]:
    """As releases deste alvo cuja imagem nenhum outro alvo de development ainda alcança."""
    own = {r.id for r in read_state(loaded.state_path, loaded.target_name).releases}
    return own - reachable(loaded, skip_own=True)


def _storages(
    ctx: DeployContext, loaded: Loaded, chosen: Adapters, data: bool
) -> list[str]:
    if not data:
        return [
            f"storage {name}: mantido (use --data para remover)"
            for name in loaded.storages
        ]
    return [chosen.storage.destroy(ctx, name, s) for name, s in loaded.storages.items()]


def run(args: argparse.Namespace) -> int:
    print(
        "\n".join(
            destroy(
                load_from(args),
                LocalExecutor(),
                force_unlock=args.force_unlock,
                data=args.data,
            )
        )
    )
    return 0
