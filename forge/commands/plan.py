"""forge plan: mostra o que o deploy mudaria no alvo, sem mudar nada no ar e sem ler segredo."""

from __future__ import annotations

import argparse
import dataclasses
from collections.abc import Mapping

from forge.commands._common import add_target_args, load_from
from forge.commands.env import deployed_images
from forge.artifacts import local_tag
from forge.contracts import DeployContext, Executor
from forge.deploy import context, default_adapters, public_env
from forge.envelope import Envelope, secret_refs, write_envelope
from forge.executor import LocalExecutor
from forge.load import Loaded
from forge.model import task_order


def register(verbs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = verbs.add_parser(
        "plan", help="mostra o que o deploy mudaria, sem mudar nada"
    )
    add_target_args(parser)
    parser.set_defaults(handler=run)


def artifact_lines(loaded: Loaded) -> list[str]:
    lines: list[str] = []
    for name, artifact in loaded.manifest.artifacts.items():
        build = loaded.mode == "development" and artifact.build is not None
        how = "build local" if build else f"pull {artifact.pull}"
        lines.append(f"artifact {name}: {how} -> {artifact.env}")
    return lines


def deploy_job_lines(loaded: Loaded) -> list[str]:
    return [
        f"job {name}: roda no deploy ({job.on_deploy}), tasks "
        f"{' -> '.join(t.key for t in task_order(job.tasks))}"
        for name, job in loaded.jobs.items()
        if job.on_deploy is not None
    ]


def declared_ref(loaded: Loaded, name: str) -> str | None:
    """A imagem que o deploy build ou puxa, sem build nem pull: dev com build usa a tag local."""
    artifact = loaded.manifest.artifacts[name]
    if loaded.mode == "development" and artifact.build is not None:
        return local_tag(loaded, name)
    return None if artifact.pull is None else str(artifact.pull)


def plan_images(loaded: Loaded) -> dict[str, str]:
    """As imagens da release atual; o artifact que ainda não tem release (primeiro deploy)
    entra pela referência declarada, senão o compose recusa o serviço sem imagem."""
    declared = {
        artifact.env: ref
        for name, artifact in loaded.manifest.artifacts.items()
        if (ref := declared_ref(loaded, name)) is not None
    }
    return {**declared, **deployed_images(loaded)}


def probe_context(
    loaded: Loaded, executor: Executor, images: Mapping[str, str], tag: str
) -> DeployContext:
    """Envelope de sondagem em .forge/build/<alvo>/: segredo vira marcador, nada é resolvido."""
    path = loaded.build_dir / f"plan-{tag}.env"
    refs = secret_refs(loaded.env, f"env do alvo {loaded.target_name}")
    values = {
        **public_env(loaded),
        **images,
        **{k: f"forge-plan-{tag}" for k in refs},
        "FORGE_ENV_FILE": path.as_posix(),
    }
    write_envelope(Envelope(loaded.target_name, values, frozenset()), path)
    return dataclasses.replace(context(loaded, executor, values), env_file=path)


def plan_lines(loaded: Loaded, executor: Executor) -> list[str]:
    """Compara com as imagens da release atual; o envelope do alvo não é escrito."""
    images = plan_images(loaded)
    ctx = dataclasses.replace(
        probe_context(loaded, executor, images, "a"),
        twin=probe_context(loaded, executor, images, "b"),
    )
    adapters = default_adapters(loaded)
    lines = [
        f"plan {loaded.name} -> {loaded.target_name} ({loaded.mode}), "
        f"release {loaded.release}",
        *artifact_lines(loaded),
    ]
    for name, storage in loaded.storages.items():
        lines += adapters.storage.plan(ctx, name, storage)
    for name, service in loaded.services.items():
        lines += adapters.service.plan(ctx, name, service)
    for name, route in loaded.routes.items():
        lines += adapters.route.plan(ctx, loaded.name, name, route, loaded.target.proxy)
    for name, job in loaded.flow_jobs.items():
        lines += adapters.job.plan(ctx, name, job)
    lines += deploy_job_lines(loaded)
    return lines


def run(args: argparse.Namespace) -> int:
    print("\n".join(plan_lines(load_from(args), LocalExecutor())))
    return 0
