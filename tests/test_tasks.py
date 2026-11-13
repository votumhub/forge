from __future__ import annotations

import sys
from pathlib import Path

import pytest

from forge.contracts import DeployContext
from forge.executor import LocalExecutor, StepError
from forge.load import load
from forge.model import Job, ManifestError, Service
from forge.tasks import TaskError, run_deploy_jobs
from forge.testing import FakeExecutor
from tests.support import Writer, manifest

PY = sys.executable
SERVICES = {"web": Service(compose=["compose.yaml"])}


def context(tmp_path: Path, executor: object) -> DeployContext:
    return DeployContext(
        root=tmp_path,
        target="dev",
        mode="development",
        release="r1",
        project="toy-dev",
        env={"FORGE_TARGET": "dev", "DB": "pw"},
        env_file=tmp_path / "dev.env",
        build_dir=tmp_path / "build",
        executor=executor,  # type: ignore[arg-type]
    )


def job(when: str, *tasks: dict[str, object]) -> Job:
    return Job.model_validate(
        {"trigger": {"deploy": {"when": when}}, "tasks": list(tasks)}
    )


def test_tasks_run_in_dependency_order_with_envelope(tmp_path: Path) -> None:
    fake = FakeExecutor()
    jobs = {
        "setup": job(
            "before_up",
            {"key": "seed", "run": ["seed"], "depends_on": ["migrate"]},
            {"key": "migrate", "run": ["migrate"], "timeout": 5},
        ),
        "later": job("after_up", {"key": "x", "run": ["later"]}),
    }
    done = run_deploy_jobs(context(tmp_path, fake), jobs, SERVICES, "before_up")
    assert fake.argvs() == [("migrate",), ("seed",)]
    assert fake.calls[0].timeout == 5
    assert fake.calls[0].env["FORGE_HOOK"] == "before_up"
    assert fake.calls[0].env["DB"] == "pw"
    assert done == [
        "job setup: task migrate ok (before_up)",
        "job setup: task seed ok (before_up)",
    ]


def test_failure_names_job_and_task_and_stops(tmp_path: Path) -> None:
    fake = FakeExecutor()
    fake.respond(["migrate"], code=1, output="boom")
    jobs = {
        "setup": job(
            "before_deploy",
            {"key": "migrate", "run": ["migrate"]},
            {"key": "never", "run": ["never"], "depends_on": ["migrate"]},
        )
    }
    with pytest.raises(
        TaskError, match=r"job setup, task migrate falhou em `migrate` \(código 1\)"
    ):
        run_deploy_jobs(context(tmp_path, fake), jobs, SERVICES, "before_deploy")
    assert fake.argvs() == [("migrate",)]


def test_timeout_names_job_and_task(tmp_path: Path) -> None:
    fake = FakeExecutor()
    fake.respond(["slow"], error=StepError("`slow` passou de 1800s e foi interrompido"))
    with pytest.raises(
        TaskError, match="job setup, task slow falhou: `slow` passou de"
    ):
        run_deploy_jobs(
            context(tmp_path, fake),
            {"setup": job("after_up", {"key": "slow", "run": ["slow"]})},
            SERVICES,
            "after_up",
        )


def test_real_failure_masks_secret_from_envelope(tmp_path: Path) -> None:
    executor = LocalExecutor()
    executor.hide(["pw"])
    script = "import os, sys; print('db=' + os.environ['DB']); sys.exit(2)"
    jobs = {"setup": job("before_up", {"key": "boom", "run": [PY, "-c", script]})}
    with pytest.raises(TaskError, match="task boom falhou") as caught:
        run_deploy_jobs(context(tmp_path, executor), jobs, SERVICES, "before_up")
    assert "db=***" in str(caught.value)


def test_task_with_service_runs_inside_the_container(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text(
        "services:\n  web: {image: x}\n", encoding="utf-8"
    )
    fake = FakeExecutor()
    jobs = {
        "setup": job(
            "before_up",
            {"key": "m", "run": ["alembic", "upgrade", "head"], "service": "web"},
        )
    }
    run_deploy_jobs(context(tmp_path, fake), jobs, SERVICES, "before_up")
    assert fake.argvs() == [
        (
            "docker",
            "compose",
            "-p",
            "toy-dev",
            "--env-file",
            (tmp_path / "dev.env").as_posix(),
            "-f",
            "compose.yaml",
            "run",
            "--rm",
            "web",
            "alembic",
            "upgrade",
            "head",
        )
    ]


def test_task_service_must_be_declared_by_one_resource(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text(
        "services:\n  web: {image: x}\n", encoding="utf-8"
    )
    jobs = {"setup": job("after_up", {"key": "m", "run": ["x"], "service": "db"})}
    with pytest.raises(
        TaskError, match="serviço compose db: nenhum resource o declara"
    ):
        run_deploy_jobs(context(tmp_path, FakeExecutor()), jobs, SERVICES, "after_up")


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (
            {
                "trigger": {"manual": True, "schedule": {"cron": "* * * * *"}},
                "flow": "a.py:f",
                "work_pool": "p",
            },
            "exatamente um entre schedule",
        ),
        ({"trigger": {"manual": True}, "work_pool": "p"}, "pede flow e work_pool"),
        (
            {
                "trigger": {"deploy": {"when": "after_up"}},
                "flow": "a.py:f",
                "tasks": [{"key": "a", "run": ["a"]}],
            },
            "gatilho deploy pede tasks",
        ),
        (
            {
                "trigger": {"deploy": {"when": "before_deploy"}},
                "tasks": [{"key": "a", "run": ["a"], "service": "web"}],
            },
            "não roda em before_deploy",
        ),
        (
            {
                "trigger": {"deploy": {"when": "after_up"}},
                "tasks": [
                    {"key": "a", "run": ["a"], "depends_on": ["b"]},
                    {"key": "b", "run": ["b"], "depends_on": ["a"]},
                ],
            },
            "ciclo entre tasks: a -> b -> a",
        ),
        (
            {
                "trigger": {"deploy": {"when": "after_up"}},
                "tasks": [{"key": "a", "run": ["a"], "depends_on": ["ghost"]}],
            },
            "depende de ghost",
        ),
        (
            {"flow": "a.py:f", "work_pool": "p", "schedule": {"cron": "* * * * *"}},
            "schedule",
        ),
    ],
)
def test_job_shape_errors_name_the_file(
    write_bundle: Writer, data: dict[str, object], message: str
) -> None:
    root = write_bundle(
        manifest(include=["resources/*.yaml"]), {"resources/setup.job.yaml": data}
    )
    with pytest.raises(
        ManifestError, match=rf"resources/setup.job.yaml[\s\S]*{message}"
    ):
        load(root, environ={}, release="r1")


def test_hooks_left_the_manifest(write_bundle: Writer) -> None:
    root = write_bundle(manifest(hooks={"after_up": [["x"]]}))
    with pytest.raises(ManifestError, match="hooks: campo desconhecido"):
        load(root, environ={}, release="r1")
