from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from forge.adapters.compose import BLIND, ComposeAdapter, ComposeError
from forge.adapters.prefect import PrefectAdapter
from forge.commands.plan import plan_lines
from forge.deploy import Adapters, deploy, rollback
from forge.load import load
from forge.state import read_state
from forge.testing import FakeExecutor
from tests.support import Writer, manifest

# Fixtures no formato de saída do docker: `compose config --hash '*'` e `docker ps --format`.
CONFIG_HASH = """web 9b1f0e0c2d7a4a55
worker 1c2d3e4f5a6b7c8d
db 77aa88bb99cc00dd
"""
PS_LABELS = """web 9b1f0e0c2d7a4a55
worker 0000000000000000
old 1234123412341234
"""
COMPOSE = 'services:\n  web: {image: "${APP_IMAGE}"}\n  worker: {image: x}\n  db: {image: y}\n'


def bundle(write_bundle: Writer) -> Path:
    data = manifest(
        include=["resources/*.yaml"],
        artifacts={"app": {"env": "APP_IMAGE", "build": {}, "pull": "reg/app:1"}},
    )
    return write_bundle(
        data,
        {
            "compose.yaml": COMPOSE,
            "resources/stack.service.yaml": {
                "compose": ["compose.yaml"],
                "release_services": ["web"],
            },
        },
    )


def test_plan_compares_config_hash_with_running_labels(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    fake = FakeExecutor()
    fake.respond(["docker", "compose"], output=CONFIG_HASH)
    fake.respond(["docker", "ps"], output=PS_LABELS)
    lines = plan_lines(load(root, "dev", environ={}, release="r1"), fake)
    assert lines == [
        "plan toy -> dev (development), release r1",
        "artifact app: build local -> APP_IMAGE",
        "serviço stack (project toy-dev):",
        "  db: criar",
        "  web: sem mudança",
        "  worker: atualizar",
        "  old: remover (não está mais no compose)",
    ]
    config, twin, ps = fake.argvs()
    assert config[-3:] == twin[-3:] == ("config", "--hash", "*")
    assert config[2:4] == ("-p", "toy-dev")
    assert config[5].endswith("plan-a.env") and twin[5].endswith("plan-b.env")
    assert "label=com.docker.compose.project=toy-dev" in ps
    assert not (root / ".forge" / "env" / "dev.env").exists()


def test_plan_resolves_no_secret_and_marks_what_reads_it(
    write_bundle: Writer, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = bundle(write_bundle)
    data = yaml.safe_load((root / "forge.yaml").read_text(encoding="utf-8"))
    data["env"] = {"TOKEN": "secret://TOKEN"}
    data["targets"]["dev"]["secrets"] = {
        "provider": "exec",
        "command": ["never-called"],
    }
    (root / "forge.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("o plan chamou um provider de segredo")

    monkeypatch.setattr("forge.envelope.provider_for", refuse)
    monkeypatch.setattr("forge.secrets.provider_for", refuse)
    fake = FakeExecutor()
    fake.respond(["docker", "compose"], output=CONFIG_HASH)
    fake.respond(["docker", "ps"], output=CONFIG_HASH)
    twin_file = (root / ".forge" / "build" / "dev" / "plan-b.env").as_posix()
    fake.respond(
        ["docker", "compose", "-p", "toy-dev", "--env-file", twin_file],
        output=CONFIG_HASH.replace("9b1f0e0c2d7a4a55", "ffffffffffffffff"),
    )
    lines = plan_lines(load(root, "dev", environ={}, release="r1"), fake)
    assert f"  web: {BLIND}" in lines and "  worker: sem mudança" in lines
    assert not (root / ".forge" / "env" / "dev.env").exists()
    assert "forge-plan-b" in Path(twin_file).read_text(encoding="utf-8")
    assert not any(call.argv[0] == "never-called" for call in fake.calls)


def test_plan_without_change(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    fake = FakeExecutor()
    fake.respond(["docker", "compose"], output=CONFIG_HASH)
    fake.respond(["docker", "ps"], output=CONFIG_HASH)
    lines = plan_lines(load(root, "dev", environ={}, release="r1"), fake)
    assert all(line.endswith("sem mudança") for line in lines[3:])


def test_plan_refuses_unexpected_docker_output(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    fake = FakeExecutor()
    fake.respond(["docker", "compose"], output="garbage line with many words\n")
    with pytest.raises(ComposeError, match="saída inesperada"):
        plan_lines(load(root, "dev", environ={}, release="r1"), fake)


def test_compose_rollback_reapplies_old_image(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    adapters = Adapters(service=ComposeAdapter(), job=PrefectAdapter("toy", {}))
    for release, image in (("r1", "sha256:one"), ("r2", "sha256:two")):
        fake = FakeExecutor()
        fake.respond(
            ["docker", "image", "inspect"],
            output=json.dumps([{"Id": image, "RepoDigests": []}]),
        )
        deploy(
            load(root, "dev", environ={}, release=release),
            fake,
            adapters=adapters,
            report=lambda _: None,
        )
    back = FakeExecutor()
    rollback(
        load(root, "dev", environ={}, release="r3"),
        back,
        reload=lambda r: load(root, "dev", environ={}, release=r),
        adapters=adapters,
        report=lambda _: None,
    )
    env_file = root / ".forge" / "env" / "dev.env"
    assert "APP_IMAGE=sha256:one\n" in env_file.read_text(encoding="utf-8")
    compose_calls = [a for a in back.argvs() if a[:2] == ("docker", "compose")]
    assert compose_calls[-1][-4:] == ("--no-deps", "--force-recreate", "web")[
        -4:
    ] or compose_calls[-1][-3:] == ("--no-deps", "--force-recreate", "web")
    assert not any(
        a[:2] in {("docker", "build"), ("docker", "pull")} for a in back.argvs()
    )
    assert read_state(root / ".forge" / "state", "dev").current == "r1"


def test_plan_does_not_remove_services_of_other_resources_in_the_project(
    write_bundle: Writer,
) -> None:
    root = write_bundle(
        manifest(include=["resources/*.yaml"]),
        {
            "a.yaml": "services:\n  web: {image: x}\n",
            "b.yaml": "services:\n  proxy: {image: y}\n",
            "resources/web.service.yaml": {"compose": ["a.yaml"]},
            "resources/proxy.service.yaml": {"compose": ["b.yaml"]},
        },
    )
    fake = FakeExecutor()
    fake.respond(["docker", "compose"], output="web 1\n")
    fake.respond(["docker", "ps"], output="web 1\nproxy 2\nold 3\n")
    lines = plan_lines(load(root, "dev", environ={}, release="r1"), fake)
    assert "  proxy: remover (não está mais no compose)" not in lines
    assert "  web: remover (não está mais no compose)" not in lines
    assert lines.count("  old: remover (não está mais no compose)") == 2


@pytest.mark.parametrize(
    ("target", "image"), [("dev", "toy-app:r1"), ("prd", "reg/app:1")]
)
def test_first_plan_probes_with_the_declared_image(
    write_bundle: Writer, target: str, image: str
) -> None:
    """Sem release no state, o envelope de sondagem leva a imagem declarada, sem build nem
    pull: com a variável vazia o compose recusa o serviço e o plan do primeiro deploy quebra."""
    root = bundle(write_bundle)
    fake = FakeExecutor()
    fake.respond(["docker", "compose"], output=CONFIG_HASH)
    fake.respond(["docker", "ps"], output="")
    plan_lines(load(root, target, environ={}, release="r1"), fake)
    probe = (root / ".forge" / "build" / target / "plan-a.env").read_text(
        encoding="utf-8"
    )
    assert f"APP_IMAGE={image}" in probe.splitlines()
    assert not [
        a for a in fake.argvs() if a[:2] in {("docker", "build"), ("docker", "pull")}
    ]
