"""Jobs de gatilho deploy: o forge roda as tasks em ordem, no executor local ou no contêiner."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from forge.adapters.compose import compose_argv
from forge.contracts import STEP_TIMEOUT, DeployContext, hook_env
from forge.errors import ForgeError
from forge.executor import OUTPUT_TAIL, StepError, show
from forge.model import DeployMoment, Job, Service, Task, task_order
from forge.policy import compose_service_names


class TaskError(ForgeError):
    pass


def run_deploy_jobs(
    ctx: DeployContext,
    jobs: Mapping[str, Job],
    services: Mapping[str, Service],
    when: DeployMoment,
) -> list[str]:
    """Roda os jobs do momento `when`; a primeira task que falha para tudo."""
    done: list[str] = []
    for name, job in jobs.items():
        if job.on_deploy != when:
            continue
        for task in task_order(job.tasks):
            _run(ctx, name, task, services, when)
            done.append(f"job {name}: task {task.key} ok ({when})")
    return done


def task_argv(
    ctx: DeployContext, task: Task, services: Mapping[str, Service]
) -> list[str]:
    if task.service is None:
        return list(task.run)
    owner = service_owner(ctx.root, task.service, services, ctx.env)
    return [*compose_argv(ctx, owner), "run", "--rm", task.service, *task.run]


def service_owner(
    root: Path,
    compose_service: str,
    services: Mapping[str, Service],
    env: Mapping[str, str] | None = None,
) -> Service:
    """O resource service cujos arquivos compose declaram `compose_service`; um e só um."""
    owners = [
        s
        for s in services.values()
        if compose_service in compose_service_names(root, s.compose, env)[0]
    ]
    if len(owners) != 1:
        found = (
            "nenhum resource o declara"
            if not owners
            else f"{len(owners)} resources o declaram"
        )
        raise TaskError(
            f"serviço compose {compose_service}: {found}; "
            f"a task precisa de exatamente um"
        )
    return owners[0]


def _run(
    ctx: DeployContext, job: str, task: Task, services: Mapping[str, Service], when: str
) -> None:
    where = f"job {job}, task {task.key}"
    argv = task_argv(ctx, task, services)
    try:
        result = ctx.executor.run(
            argv,
            cwd=ctx.root,
            env=hook_env(ctx, when),
            timeout=task.timeout or STEP_TIMEOUT,
        )
    except StepError as error:
        raise TaskError(f"{where} falhou: {error}") from error
    if not result.ok:
        raise TaskError(
            f"{where} falhou em `{show(argv)}` (código {result.code}):\n"
            f"{result.output[-OUTPUT_TAIL:]}"
        )
