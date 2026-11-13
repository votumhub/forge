from __future__ import annotations

import sys
from pathlib import Path

import yaml

from forge.adapters.prefect import PrefectAdapter, cli_env, render
from forge.contracts import DeployContext
from forge.executor import LocalExecutor
from forge.model import Job, Schedule, Trigger
from forge.testing import FakeExecutor

JOBS = {
    "daily_load": Job(
        flow="src/toy/flows.py:daily_load",
        parameters={"days": 1},
        trigger=Trigger(
            schedule=Schedule(cron="0 6 * * *", timezone="America/Sao_Paulo")
        ),
        work_pool="local",
    ),
    "adhoc": Job(
        flow="src/toy/flows.py:adhoc", work_pool="local", trigger=Trigger(manual=True)
    ),
}
ENV = {
    "FORGE_TARGET": "dev",
    "FORGE_RELEASE": "r1",
    "FORGE_ENV_FILE": "/srv/toy/.forge/env/dev.env",
    "FORGE_ROOT": "/srv/toy",
    "FORGE_STATE_PATH": "/srv/toy/.forge/state",
    "DB_PASSWORD": "super-secret",
    "PREFECT_API_URL": "http://prefect:4200/api",
}

SNAPSHOT_DEV = """name: toy
pull:
- prefect.deployments.steps.set_working_directory:
    directory: /srv/toy
deployments:
- name: daily_load-dev
  entrypoint: src/toy/flows.py:daily_load
  parameters:
    days: 1
  tags:
  - toy-dev
  - dev
  work_pool:
    name: local
    job_variables:
      env:
        FORGE_TARGET: dev
        FORGE_RELEASE: r1
        FORGE_ENV_FILE: /srv/toy/.forge/env/dev.env
        FORGE_ROOT: /srv/toy
  schedules:
  - cron: 0 6 * * *
    timezone: America/Sao_Paulo
    active: false
- name: adhoc-dev
  entrypoint: src/toy/flows.py:adhoc
  parameters: {}
  tags:
  - toy-dev
  - dev
  work_pool:
    name: local
    job_variables:
      env:
        FORGE_TARGET: dev
        FORGE_RELEASE: r1
        FORGE_ENV_FILE: /srv/toy/.forge/env/dev.env
        FORGE_ROOT: /srv/toy
"""


def context(
    tmp_path: Path, mode: str = "development", fake: FakeExecutor | None = None
) -> DeployContext:
    return DeployContext(
        root=Path("/srv/toy") if fake is None else tmp_path,
        target="dev",
        mode=mode,  # type: ignore[arg-type]
        release="r1",
        project="toy-dev",
        env=ENV,
        env_file=tmp_path / "dev.env",
        build_dir=tmp_path / "build",
        executor=fake or FakeExecutor(),
    )


def test_prefect_yaml_snapshot_in_development(tmp_path: Path) -> None:
    assert render(context(tmp_path), "toy", JOBS) == SNAPSHOT_DEV


def test_schedule_active_in_production(tmp_path: Path) -> None:
    assert "active: true" in render(context(tmp_path, "production"), "toy", JOBS)


def test_no_secret_in_job_variables(tmp_path: Path) -> None:
    rendered = render(context(tmp_path), "toy", JOBS)
    assert "super-secret" not in rendered
    assert "PREFECT_API_URL" not in rendered


def test_up_writes_build_file_and_runs_exact_argv(tmp_path: Path) -> None:
    fake = FakeExecutor()
    ctx = context(tmp_path, fake=fake)
    PrefectAdapter("toy", JOBS).up(ctx, "daily_load", JOBS["daily_load"])
    prefect_file = tmp_path / "build" / "prefect.yaml"
    assert prefect_file.is_file()
    assert fake.argvs() == [
        (
            "prefect",
            "--no-prompt",
            "deploy",
            "--prefect-file",
            prefect_file.as_posix(),
            "--name",
            "daily_load-dev",
        ),
    ]
    assert fake.calls[0].env["PREFECT_API_URL"] == "http://prefect:4200/api"
    assert fake.calls[0].env["PYTHONIOENCODING"] == "utf-8"


def test_cli_output_with_box_drawing_reads_as_utf8(tmp_path: Path) -> None:
    """No Windows, sem PYTHONIOENCODING o filho quebra ao escrever `└──` num pipe."""
    script = (
        "import sys; print(chr(0x2514) + chr(0x2500) * 2 + ' UUID: x'); sys.exit(0)"
    )
    result = LocalExecutor().run(
        [sys.executable, "-c", script], cwd=tmp_path, env=cli_env(context(tmp_path))
    )
    assert result.ok
    assert result.output.strip() == "└── UUID: x"


def test_plan_names_deployment_and_paused_schedule(tmp_path: Path) -> None:
    lines = PrefectAdapter("toy", JOBS).plan(
        context(tmp_path), "daily_load", JOBS["daily_load"]
    )
    assert lines == [
        "job daily_load: deployment daily-load/daily_load-dev (agenda pausada)"
    ]


def test_event_trigger_is_passed_through_and_off_in_development(tmp_path: Path) -> None:
    event = {
        "match": {"prefect.resource.id": "my.resource"},
        "expect": ["my.resource.pinged"],
    }
    jobs = {
        "on_ping": Job(
            flow="src/toy/flows.py:on_ping",
            work_pool="local",
            trigger=Trigger(event=event),
        )
    }
    dev = yaml.safe_load(render(context(tmp_path), "toy", jobs))["deployments"][0]
    assert dev["triggers"] == [{"type": "event", **event, "enabled": False}]
    assert "schedules" not in dev
    prd = yaml.safe_load(render(context(tmp_path, "production"), "toy", jobs))
    assert prd["deployments"][0]["triggers"][0]["enabled"] is True
    lines = PrefectAdapter("toy", jobs).plan(
        context(tmp_path), "on_ping", jobs["on_ping"]
    )
    assert lines == [
        "job on_ping: deployment on-ping/on_ping-dev (gatilho de evento desligado)"
    ]
