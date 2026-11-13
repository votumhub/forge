from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from forge.cli import main
from forge.commands.destroy import destroy
from forge.commands.plan import plan_lines
from forge.commands.restore import restore
from forge.deploy import deploy
from forge.errors import ForgeError
from forge.load import load
from forge.model import ManifestError
from forge.scaffold import RECIPES, render_tree
from forge.testing import FakeExecutor
from tests.support import Writer, manifest

INSPECT = ("docker", "volume", "inspect")


def bundle(write_bundle: Writer) -> Path:
    data = manifest(
        include=["resources/*.yaml"],
        env={"DB_PASSWORD": "secret://DB_PASSWORD"},
        artifacts={"app": {"env": "APP_IMAGE", "build": {}}},
    )
    for target in ("dev", "prd"):
        data["targets"][target]["secrets"] = {"provider": "dotenv", "path": "s.env"}  # type: ignore[index]
    return write_bundle(
        data,
        {
            "s.env": "DB_PASSWORD=pw-secret\n",
            "resources/data.storage.yaml": {"kind": "volume"},
            "resources/lake.storage.yaml": {
                "kind": "bucket",
                "ensure": ["make", "bucket"],
            },
            "resources/db.storage.yaml": {
                "kind": "database",
                "ensure": ["make", "db"],
                "backup": {"job": "dump", "restore": ["make", "restore"]},
            },
            "resources/first.job.yaml": {
                "trigger": {"deploy": {"when": "before_deploy"}},
                "tasks": [{"key": "a", "run": ["first"]}],
            },
        },
    )


def test_ensure_runs_after_the_envelope_with_secrets(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    fake = FakeExecutor()
    fake.respond(list(INSPECT), code=1)
    fake.respond(["docker", "image", "inspect"], output='[{"Id": "sha256:x"}]')
    deploy(load(root, "dev", environ={}, release="r1"), fake, report=lambda _: None)
    argvs = fake.argvs()
    assert argvs[0] == ("first",)
    assert argvs[1][:2] == ("docker", "build")
    assert argvs[3:7] == [
        (*INSPECT, "toy-dev_data"),
        ("docker", "volume", "create", "toy-dev_data"),
        ("make", "db"),
        ("make", "bucket"),
    ]
    assert fake.calls[5].env["DB_PASSWORD"] == "pw-secret"
    assert (root / ".forge" / "env" / "dev.env").is_file()


def test_existing_volume_is_not_created_and_explicit_name_wins(
    write_bundle: Writer,
) -> None:
    root = bundle(write_bundle)
    (root / "resources" / "data.storage.yaml").write_text(
        "kind: volume\nname: shared-data\n", encoding="utf-8"
    )
    fake = FakeExecutor()
    fake.respond(["docker", "image", "inspect"], output='[{"Id": "sha256:x"}]')
    deploy(load(root, "dev", environ={}, release="r1"), fake, report=lambda _: None)
    assert (*INSPECT, "shared-data") in fake.argvs()
    assert not any(a[:3] == ("docker", "volume", "create") for a in fake.argvs())


def test_plan_shows_volume_state_and_managed_kinds(write_bundle: Writer) -> None:
    fake = FakeExecutor()
    fake.respond([*INSPECT, "toy-dev_data"], code=1)
    lines = plan_lines(
        load(bundle(write_bundle), "dev", environ={}, release="r1"), fake
    )
    assert "storage data (volume toy-dev_data): ausente (o deploy cria)" in lines
    assert "storage lake (bucket): gerido pelo ensure" in lines
    assert "storage db (database): gerido pelo ensure" in lines


def test_destroy_keeps_storage_unless_data(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    fake = FakeExecutor()
    kept = destroy(load(root, "dev", environ={}, release="r1"), fake)
    assert "storage data: mantido (use --data para remover)" in kept
    assert not any(a[:2] == ("docker", "volume") for a in fake.argvs())
    gone = destroy(load(root, "dev", environ={}, release="r1"), fake, data=True)
    assert ("docker", "volume", "rm", "toy-dev_data") in fake.argvs()
    assert "storage lake (bucket): mantido (o forge só remove volume)" in gone


def test_destroy_never_touches_production_storage(write_bundle: Writer) -> None:
    fake = FakeExecutor()
    with pytest.raises(ForgeError, match="só existe em development"):
        destroy(
            load(bundle(write_bundle), "prd", environ={}, release="r1"), fake, data=True
        )
    assert fake.calls == []


def test_restore_runs_declared_command_with_envelope(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    fake = FakeExecutor()
    fake.respond(["make", "restore"], output="restaurado com pw-secret")
    out = restore(
        load(root, "dev", environ={}, release="r1"), "db", fake, approved=False
    )
    assert fake.argvs() == [("make", "restore")]
    assert fake.calls[0].env["DB_PASSWORD"] == "pw-secret"
    assert out == "restaurado com ***"
    assert not (root / ".forge" / "state" / ".lock").exists()


def test_restore_refusals(
    write_bundle: Writer, capsys: pytest.CaptureFixture[str]
) -> None:
    root = bundle(write_bundle)
    fake = FakeExecutor()
    with pytest.raises(ForgeError, match="--yes"):
        restore(load(root, "prd", environ={}, release="r1"), "db", fake, approved=False)
    with pytest.raises(ForgeError, match="não declara backup"):
        restore(
            load(root, "dev", environ={}, release="r1"), "lake", fake, approved=False
        )
    assert fake.calls == []
    assert main(["--root", str(root), "restore", "-t", "dev", "ghost"]) == 2
    assert "storage ghost não existe" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"kind": "bucket"}, "bucket exige ensure"),
        ({"kind": "database"}, "database exige ensure"),
        ({"kind": "volume", "ensure": ["x"]}, "volume não leva ensure"),
        ({"kind": "bucket", "ensure": ["x"], "name": "n"}, "name só vale para volume"),
        ({"kind": "disk"}, "kind"),
    ],
)
def test_storage_shape_errors_name_the_file(
    write_bundle: Writer, data: dict[str, object], message: str
) -> None:
    root = write_bundle(
        manifest(include=["resources/*.yaml"]), {"resources/s.storage.yaml": data}
    )
    with pytest.raises(
        ManifestError, match=rf"resources/s.storage.yaml[\s\S]*{message}"
    ):
        load(root, environ={}, release="r1")


class FakeClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """O bucket como o S3 responde a uma chave escopada: head_bucket diz 404 ou 403."""

    def __init__(self, head: str | None) -> None:
        self.head = head
        self.calls: list[str] = []

    def head_bucket(self, Bucket: str) -> None:
        self.calls.append(f"head {Bucket}")
        if self.head is not None:
            raise FakeClientError(self.head)

    def list_buckets(self) -> None:
        raise FakeClientError("AccessDenied")

    def create_bucket(self, Bucket: str) -> None:
        self.calls.append(f"create {Bucket}")

    def put_bucket_lifecycle_configuration(self, **kwargs: object) -> None:
        self.calls.append(f"lifecycle {kwargs['LifecycleConfiguration']}")


LANDINGS = [
    ("cloudflare-r2", "r2_landing", "R2_BUCKET", "R2_LIFECYCLE_DAYS"),
    ("aws-s3", "aws_s3_landing", "AWS_S3_BUCKET", "AWS_S3_LIFECYCLE_DAYS"),
    ("minio", "minio_landing", "MINIO_BUCKET", "MINIO_LIFECYCLE_DAYS"),
]


def landing(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeS3,
    landing_case: tuple[str, str, str, str],
    days: str | None,
) -> types.ModuleType:
    recipe, module_name, bucket_var, days_var = landing_case
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *_, **__: fake  # type: ignore[attr-defined]
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = FakeClientError  # type: ignore[attr-defined]
    config = types.ModuleType("botocore.config")
    config.Config = lambda **_: None  # type: ignore[attr-defined]
    for name, stub in (
        ("boto3", boto3),
        ("botocore", types.ModuleType("botocore")),
        ("botocore.exceptions", exceptions),
        ("botocore.config", config),
    ):
        monkeypatch.setitem(sys.modules, name, stub)
    for name, value in (
        (bucket_var, "lake"),
        ("R2_ACCOUNT_ID", "a"),
        ("AWS_ENDPOINT_URL", "http://localhost:9000"),
    ):
        monkeypatch.setenv(name, value)
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "k")
    if days is None:
        monkeypatch.delenv(days_var, raising=False)
    else:
        monkeypatch.setenv(days_var, days)
    source = render_tree(RECIPES / recipe / "files", {"package": "p", "name": "n"})
    module = types.ModuleType(module_name)
    exec(
        compile(source[f"src/p/storage/{module_name}.py"], module_name, "exec"),
        module.__dict__,
    )
    return module


@pytest.mark.parametrize("landing_case", LANDINGS, ids=[c[0] for c in LANDINGS])
@pytest.mark.parametrize(
    "head,days,calls",
    [
        (None, None, ["head lake"]),
        ("404", None, ["head lake", "create lake"]),
        ("NoSuchBucket", "30", ["head lake", "create lake", "lifecycle"]),
    ],
)
def test_bootstrap_checks_its_own_bucket_and_expires_only_when_declared(
    monkeypatch: pytest.MonkeyPatch,
    landing_case: tuple[str, str, str, str],
    head: str | None,
    days: str | None,
    calls: list[str],
) -> None:
    fake = FakeS3(head)
    landing(monkeypatch, fake, landing_case, days).bootstrap()
    assert [call.split(" {")[0] for call in fake.calls] == calls
    if days is not None:
        assert "'Days': 30" in fake.calls[-1]


@pytest.mark.parametrize("landing_case", LANDINGS, ids=[c[0] for c in LANDINGS])
def test_bootstrap_without_permission_says_so(
    monkeypatch: pytest.MonkeyPatch, landing_case: tuple[str, str, str, str]
) -> None:
    fake = FakeS3("403")
    with pytest.raises(SystemExit, match="não tem permissão") as caught:
        landing(monkeypatch, fake, landing_case, None).bootstrap()
    assert landing_case[2] in str(caught.value) and fake.calls == ["head lake"]
