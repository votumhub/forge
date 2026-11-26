from __future__ import annotations

from pathlib import Path

import pytest

from forge.cli import main
from forge.commands.destroy import destroy
from forge.commands.run import run_job
from forge.errors import ForgeError
from forge.load import load
from forge.state import Release, record
from forge.testing import FakeExecutor
from tests.support import Writer, manifest


def bundle(write_bundle: Writer) -> Path:
    data = manifest(
        include=["resources/*.yaml"],
        env={"PREFECT_API_URL": "secret://PREFECT_API_URL"},
    )
    for target in ("dev", "prd"):
        data["targets"][target]["secrets"] = {"provider": "dotenv", "path": "s.env"}  # type: ignore[index]
    return write_bundle(
        data,
        {
            "s.env": "PREFECT_API_URL=http://user:token-xyz@prefect/api\n",
            "compose.yaml": "services:\n  web: {image: nginx}\n",
            "resources/web.service.yaml": {"compose": ["compose.yaml"]},
            "resources/daily_load.job.yaml": {
                "trigger": {"manual": True},
                "flow": "src/x/flows.py:daily_load",
                "work_pool": "p",
            },
        },
    )


def test_run_triggers_the_target_deployment(write_bundle: Writer) -> None:
    fake = FakeExecutor()
    fake.respond(
        ["prefect", "deployment", "run"],
        output="run at http://user:token-xyz@prefect/api",
    )
    output = run_job(
        load(bundle(write_bundle), "dev", environ={}, release="r1"),
        "daily_load",
        fake,
        approved=False,
    )
    assert fake.argvs() == [
        ("prefect", "deployment", "run", "daily-load/daily_load-dev")
    ]
    assert fake.calls[0].env["PREFECT_API_URL"].endswith("@prefect/api")
    assert output.output == "run at ***"


def test_run_unknown_job_and_production_needs_yes(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    with pytest.raises(ForgeError, match="job nope não existe"):
        run_job(
            load(root, "dev", environ={}, release="r1"),
            "nope",
            FakeExecutor(),
            approved=False,
        )
    fake = FakeExecutor()
    with pytest.raises(ForgeError, match="--yes"):
        run_job(
            load(root, "prd", environ={}, release="r1"),
            "daily_load",
            fake,
            approved=False,
        )
    assert fake.calls == []
    run_job(
        load(root, "prd", environ={}, release="r1"), "daily_load", fake, approved=True
    )
    assert fake.argvs() == [
        ("prefect", "deployment", "run", "daily-load/daily_load-prd")
    ]


def test_destroy_in_development(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    fake = FakeExecutor()
    done = destroy(load(root, "dev", environ={}, release="r1"), fake)
    assert done[1:] == ["job daily_load: deployment removido", "serviço web: derrubado"]
    assert "nenhuma release ok" in done[0] and "forge deploy -t dev" in done[0]
    assert fake.argvs()[0] == (
        "prefect",
        "--no-prompt",
        "deployment",
        "delete",
        "daily-load/daily_load-dev",
    )
    assert fake.argvs()[1][-1] == "down"
    assert fake.argvs()[1][2:4] == ("-p", "toy-dev")
    assert not (root / ".forge" / "state" / ".lock").exists()


def test_destroy_refused_in_production(
    write_bundle: Writer, capsys: pytest.CaptureFixture[str]
) -> None:
    root = bundle(write_bundle)
    fake = FakeExecutor()
    with pytest.raises(ForgeError, match="destroy só existe em development"):
        destroy(load(root, "prd", environ={}, release="r1"), fake)
    assert fake.calls == []
    assert main(["--root", str(root), "destroy", "-t", "prd"]) == 2
    assert "só existe em development" in capsys.readouterr().err


def test_destroy_data_takes_compose_volumes_too(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    fake = FakeExecutor()
    destroy(load(root, "dev", environ={}, release="r1"), fake, data=True)
    assert fake.argvs()[1][-2:] == ("down", "-v")


def test_destroy_removes_only_images_it_built_and_no_one_reaches(
    write_bundle: Writer,
) -> None:
    data = manifest(artifacts={"app": {"env": "APP_IMAGE", "build": {}}})
    data["targets"]["dev2"] = {"mode": "development"}  # type: ignore[index]
    root = write_bundle(data)
    for target, ids in (("dev", ("r1", "r2")), ("dev2", ("r2",))):
        for id_ in ids:
            record(
                root / ".forge" / "state",
                target,
                Release(id=id_, git_sha=None, at="t", status="ok"),
            )
    fake = FakeExecutor()
    fake.respond(
        ["docker", "image", "ls"], output="toy-app:r1\ntoy-app:r2\ntoy-app:x\n"
    )
    done = destroy(load(root, "dev", environ={}, release="r3"), fake)
    removed = [
        argv[-1] for argv in fake.argvs() if argv[:3] == ("docker", "image", "rm")
    ]
    assert removed == ["toy-app:r1"]
    assert done[-1] == "imagem toy-app:r1: removida"
