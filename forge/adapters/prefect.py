"""Adaptador prefect: cada job vira o deployment `<job>-<alvo>` num prefect.yaml gerado."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

import yaml

from forge.contracts import DeployContext, RunResult
from forge.errors import ForgeError
from forge.executor import require_ok
from forge.model import Job

PREFECT_FILE = "prefect.yaml"
JOB_ENV_NAMES = ("FORGE_TARGET", "FORGE_RELEASE", "FORGE_ENV_FILE", "FORGE_ROOT")
RUN_ID = re.compile(
    r"UUID:\s*(?P<id>[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})"
)


def deployment_name(job: str, target: str) -> str:
    return f"{job}-{target}"


def entrypoint(job: Job) -> str:
    if job.flow is None:
        raise ForgeError(
            "job de gatilho deploy não vai ao Prefect: o forge roda as tasks no deploy"
        )
    return job.flow


def flow_name(job: Job) -> str:
    """O nome padrão do flow no prefect: a função com `_` trocado por `-`."""
    return entrypoint(job).rsplit(":", 1)[1].replace("_", "-")


def full_name(ctx: DeployContext, name: str, job: Job) -> str:
    return f"{flow_name(job)}/{deployment_name(name, ctx.target)}"


def render(ctx: DeployContext, bundle: str, jobs: Mapping[str, Job]) -> str:
    """prefect.yaml do alvo; job_variables só carrega FORGE_* sem segredo."""
    job_env = {key: ctx.env[key] for key in JOB_ENV_NAMES if key in ctx.env}
    document = {
        "name": bundle,
        "pull": [
            {
                "prefect.deployments.steps.set_working_directory": {
                    "directory": ctx.root.as_posix()
                }
            }
        ],
        "deployments": [
            _deployment(ctx, name, job, job_env) for name, job in jobs.items()
        ],
    }
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


def _deployment(
    ctx: DeployContext, name: str, job: Job, job_env: Mapping[str, str]
) -> dict[str, object]:
    entry: dict[str, object] = {
        "name": deployment_name(name, ctx.target),
        "entrypoint": entrypoint(job),
        "parameters": dict(job.parameters),
        "tags": [ctx.project, ctx.target],
        "work_pool": {"name": job.work_pool, "job_variables": {"env": dict(job_env)}},
    }
    active = ctx.mode == "production"
    schedule, event = job.trigger.schedule, job.trigger.event
    if schedule is not None:
        entry["schedules"] = [
            {"cron": schedule.cron, "timezone": schedule.timezone, "active": active}
        ]
    if event is not None:
        entry["triggers"] = [{"type": "event", **event, "enabled": active}]
    return entry


def _dormant(ctx: DeployContext, job: Job) -> str:
    if ctx.mode != "development":
        return ""
    if job.trigger.schedule is not None:
        return " (agenda pausada)"
    return " (gatilho de evento desligado)" if job.trigger.event is not None else ""


@dataclass(frozen=True)
class PrefectAdapter:
    bundle: str
    jobs: Mapping[str, Job]

    def prefect_file(self, ctx: DeployContext) -> str:
        path = ctx.build_dir / PREFECT_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render(ctx, self.bundle, self.jobs), encoding="utf-8", newline="\n"
        )
        return path.as_posix()

    def plan(self, ctx: DeployContext, name: str, job: Job) -> list[str]:
        paused = _dormant(ctx, job)
        return [f"job {name}: deployment {full_name(ctx, name, job)}{paused}"]

    def up(self, ctx: DeployContext, name: str, job: Job) -> None:
        argv = [
            "prefect", "--no-prompt", "deploy",
            "--prefect-file", self.prefect_file(ctx),
            "--name", deployment_name(name, ctx.target),
        ]  # fmt: skip
        require_ok(
            ctx.executor.run(argv, cwd=ctx.root, env=cli_env(ctx)),
            f"deploy do job {name}",
        )

    def health(self, ctx: DeployContext, name: str, job: Job) -> None:
        del ctx, name, job

    def run(self, ctx: DeployContext, name: str, job: Job) -> RunResult:
        argv = ["prefect", "deployment", "run", full_name(ctx, name, job)]
        result = ctx.executor.run(argv, cwd=ctx.root, env=cli_env(ctx))
        return require_ok(result, f"run do job {name}")

    def destroy(
        self, ctx: DeployContext, name: str, job: Job, *, data: bool = False
    ) -> None:
        del data  # o deployment não guarda dado do projeto
        argv = [
            "prefect",
            "--no-prompt",
            "deployment",
            "delete",
            full_name(ctx, name, job),
        ]
        require_ok(
            ctx.executor.run(argv, cwd=ctx.root, env=cli_env(ctx)),
            f"destroy do job {name}",
        )


def cli_env(ctx: DeployContext) -> dict[str, str]:
    """O envelope com a saída do CLI em UTF-8: o forge captura por pipe e lê em UTF-8,
    e no Windows o CLI quebra ao escrever `└──` na página de código do console."""
    return {**ctx.env, "PYTHONIOENCODING": "utf-8"}


def created_run_id(output: str) -> str:
    """O id da rodada que `prefect deployment run` criou (linha `└── UUID: ...`)."""
    found = RUN_ID.search(output)
    if found is None:
        raise ForgeError(
            "`prefect deployment run` não informou o UUID da rodada criada"
        )
    return found.group("id")
