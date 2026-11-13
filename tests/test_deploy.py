from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from forge.artifacts import ArtifactError
from forge.contracts import DeployContext, RunResult
from forge.deploy import (
    Adapters,
    DeployFailedError,
    ProductionCheckError,
    deploy,
    deploy_and_recover,
    rollback,
)
from forge.executor import StepError
from forge.gitinfo import GitInfo
from forge.load import Loaded, load
from forge.model import GitRule
from forge.state import LockError, StateError, read_state, state_file
from forge.testing import FakeExecutor
from tests.support import Writer, manifest

REPO = Path(__file__).resolve().parents[1]
INSPECT = json.dumps([{"Id": "sha256:built", "RepoDigests": []}])


@dataclass
class Journal(FakeExecutor):
    events: list[str] = field(default_factory=list)

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: float = 1800.0,
    ) -> RunResult:
        self.events.append("exec " + " ".join(argv[:2]))
        return super().run(argv, cwd=cwd, env=env, timeout=timeout)


@dataclass
class Recorder:
    events: list[str]
    fail_health: bool = False
    seen_env: dict[str, str] = field(default_factory=dict)
    fail_releases: set[str] = field(default_factory=set)

    def plan(self, ctx: DeployContext, name: str, resource: object) -> list[str]:
        return []

    def up(self, ctx: DeployContext, name: str, resource: object) -> None:
        self.seen_env.update(ctx.env)
        self.events.append(f"up {name}")

    def health(self, ctx: DeployContext, name: str, resource: object) -> None:
        self.events.append(f"health {name}")
        if self.fail_health or ctx.release in self.fail_releases:
            raise RuntimeError(f"unhealthy {ctx.release}")

    def destroy(
        self, ctx: DeployContext, name: str, resource: object, *, data: bool = False
    ) -> None:
        self.events.append(f"destroy {name}")


def bundle(write_bundle: Writer) -> Path:
    data = manifest(
        include=["resources/*.yaml"],
        env={"DB": "secret://DB_PASS"},
        artifacts={"app": {"env": "APP_IMAGE", "build": {}, "pull": "reg/app:1"}},
    )
    data["targets"]["dev"]["secrets"] = {"provider": "dotenv", "path": "s.env"}  # type: ignore[index]
    data["targets"]["prd"]["secrets"] = {"provider": "dotenv", "path": "s.env"}  # type: ignore[index]
    for target in ("dev", "prd"):
        data["targets"][target]["proxy"] = {
            "kind": "caddy",
            "config_dir": "proxy",  # type: ignore[index]
            "reload": ["caddy", "reload"],
        }
    return write_bundle(
        data,
        {
            "resources/data.storage.yaml": {"kind": "volume"},
            "resources/site.route.yaml": {"service": "web", "port": 80, "host": "a.b"},
            "s.env": "DB_PASS=pw-secret\n",
            "resources/web.service.yaml": {"compose": ["compose.yaml"]},
            "resources/daily.job.yaml": {
                "trigger": {"manual": True},
                "flow": "src/x/flows.py:daily",
                "work_pool": "p",
            },
            **{
                f"resources/{when}.job.yaml": {
                    "trigger": {"deploy": {"when": when}},
                    "tasks": [{"key": "k", "run": ["hook", when]}],
                }
                for when in ("before_deploy", "before_up", "after_up")
            },
        },
    )


def journal() -> Journal:
    fake = Journal()
    fake.respond(["docker", "image", "inspect"], output=INSPECT)
    return fake


def loaded_dev(root: Path, release: str = "r1") -> Loaded:
    return load(root, "dev", environ={}, release=release)


def test_order_of_steps(write_bundle: Writer) -> None:
    fake = journal()
    service = Recorder(fake.events)
    job = Recorder(fake.events)
    deploy(
        loaded_dev(bundle(write_bundle)),
        fake,
        adapters=Adapters(service, job),
        report=lambda _: None,
    )
    assert fake.events == [
        "exec hook before_deploy",
        "exec docker build",
        "exec docker image",
        "exec docker volume",
        "exec hook before_up",
        "up web",
        "health web",
        "exec caddy reload",
        "up daily",
        "health daily",
        "exec hook after_up",
        "exec docker image",
    ]
    assert service.seen_env["DB"] == "pw-secret"
    assert service.seen_env["APP_IMAGE"] == "sha256:built"


def test_before_deploy_sees_no_secret(write_bundle: Writer) -> None:
    fake = journal()
    events: list[str] = []
    deploy(
        loaded_dev(bundle(write_bundle)),
        fake,
        adapters=Adapters(Recorder(events), Recorder(events)),
        report=lambda _: None,
    )
    assert "DB" not in fake.calls[0].env
    assert fake.calls[0].env["FORGE_HOOK"] == "before_deploy"


def test_success_records_ok_with_digests(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    events: list[str] = []
    deploy(
        loaded_dev(root),
        journal(),
        adapters=Adapters(Recorder(events), Recorder(events)),
        report=lambda _: None,
    )
    state = read_state(root / ".forge" / "state", "dev")
    assert state.current == "r1"
    assert state.releases[0].digests == {"app": "sha256:built"}
    assert not (root / ".forge" / "state" / ".lock").exists()
    assert "DB=pw-secret" in (root / ".forge" / "env" / "dev.env").read_text(
        encoding="utf-8"
    )


def test_failure_never_records_ok_and_releases_lock(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    events: list[str] = []
    adapters = Adapters(Recorder(events, fail_health=True), Recorder(events))
    with pytest.raises(RuntimeError, match="unhealthy"):
        deploy(loaded_dev(root), journal(), adapters=adapters, report=lambda _: None)
    state = read_state(root / ".forge" / "state", "dev")
    assert [r.status for r in state.releases] == ["failed"]
    assert state.current is None
    assert "exec hook after_up" not in events
    assert not (root / ".forge" / "state" / ".lock").exists()


def test_busy_lock_refuses_before_anything(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    (root / ".forge" / "state").mkdir(parents=True)
    (root / ".forge" / "state" / ".lock").write_text(
        '{"pid": 1, "host": "h", "at": "2099-01-01T00:00:00+00:00"}', encoding="utf-8"
    )
    fake = journal()
    with pytest.raises(LockError):
        deploy(loaded_dev(root), fake, report=lambda _: None)
    assert fake.calls == []


def prd(root: Path, release: str = "v1", **git: object) -> Loaded:
    info = {"sha": "b" * 40, "tag": None, "branch": "main", "dirty": False, **git}
    return load(
        root, "prd", environ={}, release=release, git_reader=lambda _: GitInfo(**info)
    )  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("git", "approved", "message"),
    [
        ({}, False, "--yes"),
        ({"dirty": True}, True, "árvore suja"),
        ({"branch": "feature"}, True, "sai da branch main"),
    ],
)
def test_production_checks(
    write_bundle: Writer, git: dict[str, object], approved: bool, message: str
) -> None:
    fake = journal()
    with pytest.raises(ProductionCheckError, match=message):
        deploy(
            prd(bundle(write_bundle), **git),
            fake,
            approved=approved,
            report=lambda _: None,
        )
    assert fake.calls == []


def test_production_tag_rule_and_pull_only(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    loaded = prd(root, tag="v1.0.0")
    tag_rule = loaded.target.model_copy(update={"git": GitRule(tag="v*")})
    events: list[str] = []
    fake = journal()
    fake.respond(
        ["docker", "image", "inspect"],
        output=json.dumps([{"Id": "x", "RepoDigests": ["reg/app@sha256:1"]}]),
    )
    deploy(
        dataclasses.replace(loaded, target=tag_rule),
        fake,
        approved=True,
        adapters=Adapters(Recorder(events), Recorder(events)),
        report=lambda _: None,
    )
    assert ("docker", "pull", "reg/app:1") in fake.argvs()
    assert not any(argv[:2] == ("docker", "build") for argv in fake.argvs())
    cleanup = {("docker", "image", "ls"), ("docker", "image", "rm")}
    assert not [argv for argv in fake.argvs() if argv[:3] in cleanup]
    untagged = dataclasses.replace(prd(root), target=tag_rule)
    with pytest.raises(ProductionCheckError, match=r"tag v\*; HEAD não tem tag exata"):
        deploy(untagged, journal(), approved=True, report=lambda _: None)


def test_rollback_reapplies_digests_without_rebuild(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    events: list[str] = []
    adapters = Adapters(Recorder(events), Recorder(events))
    first = journal()
    deploy(loaded_dev(root, "r1"), first, adapters=adapters, report=lambda _: None)
    second = journal()
    second.respond(
        ["docker", "image", "inspect"],
        output=json.dumps([{"Id": "sha256:second", "RepoDigests": []}]),
    )
    deploy(loaded_dev(root, "r2"), second, adapters=adapters, report=lambda _: None)
    back = Journal()
    service = Recorder([])
    release = rollback(
        loaded_dev(root, "r3"),
        back,
        reload=lambda r: loaded_dev(root, r),
        adapters=Adapters(service, Recorder([])),
        report=lambda _: None,
    )
    assert release.id == "r1"
    assert not any(
        e.startswith(("exec docker build", "exec docker pull")) for e in back.events
    )
    assert service.seen_env["APP_IMAGE"] == "sha256:built"
    assert service.seen_env["FORGE_RELEASE"] == "r1"
    assert read_state(root / ".forge" / "state", "dev").current == "r1"


@pytest.mark.parametrize(
    ("target", "code", "last", "current"),
    [
        ("dev", 0, "release r1 ok", "r1"),
        ("prd", 2, "erro: alvo prd é production e exige git", None),
    ],
)
def test_compat_cli_invocation(
    write_bundle: Writer, target: str, code: int, last: str, current: str | None
) -> None:
    """Saída capturada (CI, agente) junta stdout e stderr: o erro tem de ser a última linha,
    depois do progresso, e não sair do buffer antes dele."""
    root = write_bundle(manifest())
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "forge.cli",
            "--root",
            str(root),
            "deploy",
            "-t",
            target,
            "--yes",
            "--run",
            "--release",
            "r1",
        ],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        env={
            **os.environ,
            "PYTHONUTF8": "1",
            "GIT_CEILING_DIRECTORIES": str(root.parent),
        },
        check=False,
    )
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    assert done.returncode == code, done.stdout
    assert lines[0].startswith("deploy toy")
    assert lines[-1].startswith(last)
    assert read_state(root / ".forge" / "state", target).current == current


def test_rollback_reloads_release_interpolation(write_bundle: Writer) -> None:
    root = write_bundle(
        manifest(include=["resources/*.yaml"]),
        {
            "resources/stamp.job.yaml": {
                "trigger": {"deploy": {"when": "after_up"}},
                "tasks": [{"key": "k", "run": ["stamp", "${bundle.release}"]}],
            },
        },
    )
    for release in ("r1", "r2"):
        deploy(loaded_dev(root, release), FakeExecutor(), report=lambda _: None)
    back = FakeExecutor()
    rollback(
        loaded_dev(root, "r3"),
        back,
        reload=lambda r: loaded_dev(root, r),
        report=lambda _: None,
    )
    assert ("stamp", "r1") in back.argvs()
    assert ("stamp", "r3") not in back.argvs()


def refuse_write(path: Path, body: str) -> None:
    raise StateError(f"não consegui gravar {path}: Permission denied")


def interrupt(ctx: DeployContext, name: str, resource: object) -> None:
    raise KeyboardInterrupt


def pulling(fake: Journal, ok: bool) -> Journal:
    fake.respond(["docker", "pull"], code=0 if ok else 1, output="denied")
    return fake


@pytest.mark.parametrize(
    ("target", "before", "breaks", "error", "message", "statuses"),
    [
        (
            "prd",
            ("v1",),
            "health",
            DeployFailedError,
            r"v2 falhou(.|\n)*voltei sozinho para a release v1",
            ["ok", "failed", "ok"],
        ),
        (
            "prd",
            ("v1",),
            "health e volta",
            DeployFailedError,
            r"unhealthy v2(.|\n)*v1 também falhou: unhealthy v1",
            ["ok", "failed", "failed"],
        ),
        (
            "prd",
            ("v1",),
            "pull",
            ArtifactError,
            "pull da imagem reg/app:1",
            ["ok", "failed"],
        ),
        (
            "prd",
            ("v0", "v1"),
            "state",
            DeployFailedError,
            r"v2 subiu saudável no alvo prd, mas não consegui gravar(.|\n)*não registrou "
            r"esta tentativa e ainda marca a release v1, mas o que está no ar é a v2\. "
            r"Cuidado: forge rollback -t prd --yes agora iria para a v0(.|\n)*--yes de novo",
            ["ok", "ok"],
        ),
        (
            "prd",
            ("v0", "v1"),
            "health e state",
            DeployFailedError,
            r"falhou no alvo prd: unhealthy v2\na falha também não foi registrada no "
            r"state: não consegui gravar(.|\n)*ainda marca a release v1(.|\n)*"
            r"gravação de .*\.forge/state/prd\.json",
            ["ok", "ok"],
        ),
        (
            "prd",
            ("v0", "v1"),
            "ctrl-c e state",
            DeployFailedError,
            r"falhou no alvo prd: KeyboardInterrupt\na falha também não foi registrada"
            r"(.|\n)*ainda marca a release v1",
            ["ok", "ok"],
        ),
        (
            "dev",
            ("v1",),
            "health e state",
            DeployFailedError,
            r"falhou no alvo dev: unhealthy v2(.|\n)*ainda marca a release v1, mas o que "
            r"está no ar é a v2\. O forge não volta sozinho(.|\n)*forge deploy -t dev de",
            ["ok"],
        ),
        (
            "dev",
            ("v0", "v1"),
            "state",
            DeployFailedError,
            r"v2 subiu saudável no alvo dev(.|\n)*Cuidado: forge rollback -t dev agora iria "
            r"para a v0",
            ["ok", "ok"],
        ),
        (
            "prd",
            (),
            "state",
            DeployFailedError,
            r"v2 subiu saudável(.|\n)*não marca release nenhuma, mas o que está no ar é a "
            r"v2\. O forge não volta sozinho",
            [],
        ),
        (
            "dev",
            (),
            "corrompido",
            DeployFailedError,
            r"v2 falhou(.|\n)*state do alvo não pôde ser lido \(.*state ilegível(.|\n)*"
            r"forge plan -t dev$",
            None,
        ),
        (
            "dev",
            (),
            "utf-16",
            DeployFailedError,
            r"v2 falhou(.|\n)*state do alvo não pôde ser lido \(.*não está em UTF-8(.|\n)*"
            r"forge plan -t dev$",
            None,
        ),
        (
            "prd",
            (),
            "health",
            DeployFailedError,
            "não há release anterior(.|\n)*forge deploy -t prd --yes",
            ["failed"],
        ),
        (
            "dev",
            ("v1",),
            "health",
            DeployFailedError,
            "ficou na release v2(.|\n)*forge rollback -t dev",
            ["ok", "failed"],
        ),
        (
            "dev",
            (),
            "health",
            DeployFailedError,
            "não há release anterior(.|\n)*forge destroy -t dev",
            ["failed"],
        ),
    ],
    ids=[
        "prd-volta",
        "prd-volta-falha",
        "prd-pull-nao-mexe",
        "prd-state-nao-grava",
        "prd-falha-e-state-nao-grava",
        "prd-ctrl-c-e-state-nao-grava",
        "dev-falha-e-state-nao-grava-sem-candidata",
        "dev-state-nao-grava-com-candidata",
        "prd-primeira-e-state-nao-grava",
        "dev-state-ilegivel",
        "dev-state-utf16",
        "prd-primeira",
        "dev-fica",
        "dev-primeira",
    ],
)
def test_failed_deploy_says_where_the_service_stayed(
    write_bundle: Writer,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    before: tuple[str, ...],
    breaks: str,
    error: type[Exception],
    message: str,
    statuses: list[str] | None,
) -> None:
    root = bundle(write_bundle)
    service = Recorder([])
    adapters = Adapters(service, Recorder([]))

    def at(release: str) -> Loaded:
        return prd(root, release) if target == "prd" else loaded_dev(root, release)

    for release in before:
        deploy(
            at(release),
            pulling(journal(), True),
            approved=True,
            adapters=adapters,
            report=lambda _: None,
        )
    states = root / ".forge" / "state"
    if breaks in ("corrompido", "utf-16"):
        states.mkdir(parents=True)
        encoding = "utf-16" if breaks == "utf-16" else "utf-8"
        state_file(states, target).write_text("{corrompido", encoding=encoding)
    if "state" in breaks:
        monkeypatch.setattr("forge.state._write_atomic", refuse_write)
    if breaks.startswith("ctrl-c"):
        monkeypatch.setattr(service, "health", interrupt)
    elif breaks != "state":
        service.fail_releases = {"v2", *(before[-1:] if "volta" in breaks else [])}
    reported: list[str] = []
    with pytest.raises(error, match=message):
        deploy_and_recover(
            at("v2"),
            pulling(journal(), breaks != "pull"),
            reload=at,
            approved=True,
            adapters=adapters,
            report=reported.append,
        )
    if "state" in breaks:
        assert not [line for line in reported if "voltando sozinho" in line]
    if statuses is not None:
        state = read_state(states, target)
        assert [r.status for r in state.releases] == statuses
        assert state.current == (before[-1] if before else None)


def test_success_removes_built_images_rollback_cannot_reach(
    write_bundle: Writer,
) -> None:
    root = bundle(write_bundle)
    events: list[str] = []
    adapters = Adapters(Recorder(events), Recorder(events))
    for release in ("r1", "r2", "r3"):
        deploy(
            loaded_dev(root, release),
            journal(),
            adapters=adapters,
            report=lambda _: None,
        )
    fake = journal()
    fake.respond(
        ["docker", "image", "ls"],
        output="toy-app:r1\ntoy-app:r2\ntoy-app:r3\ntoy-app:latest\n",
    )
    fake.respond(["docker", "image", "rm", "toy-app:r1"], code=1, output="in use")
    lines: list[str] = []
    deploy(loaded_dev(root, "r4"), fake, adapters=adapters, report=lines.append)
    removed = [
        argv[-1] for argv in fake.argvs() if argv[:3] == ("docker", "image", "rm")
    ]
    assert removed == ["toy-app:r1", "toy-app:r2"]
    assert "imagem toy-app:r1: não removida (in use)" in lines
    assert "imagem toy-app:r2: removida" in lines
    assert lines[-3] == "release r4 ok"

    stuck = journal()
    stuck.respond(["docker", "image", "ls"], error=StepError("passou de 1800s"))
    lines.clear()
    release = deploy_and_recover(
        loaded_dev(root, "r5"),
        stuck,
        reload=lambda r: loaded_dev(root, r),
        adapters=adapters,
        report=lines.append,
    )
    assert release.id == "r5"
    assert read_state(root / ".forge" / "state", "dev").current == "r5"
    assert "limpeza não rodou (passou de 1800s)" in lines[-1]
