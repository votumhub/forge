from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge.artifacts import ArtifactError, artifact_env, resolve_artifacts
from forge.executor import StepError
from forge.load import load
from forge.testing import FakeExecutor
from tests.support import Writer, manifest

LOCAL = json.dumps([{"Id": "sha256:local", "RepoDigests": []}])
REMOTE = json.dumps(
    [{"Id": "sha256:x", "RepoDigests": ["ghcr.io/o/app@sha256:remote"]}]
)
BUILD = {"dockerfile": "docker/Dockerfile", "context": ".", "args": {"PY": "3.12"}}


def artifacts_bundle(write_bundle: Writer, **artifact: object) -> Path:
    return write_bundle(manifest(artifacts={"app": {"env": "APP_IMAGE", **artifact}}))


def test_development_builds_then_pins_local_id(write_bundle: Writer) -> None:
    root = artifacts_bundle(write_bundle, build=BUILD, pull="ghcr.io/o/app:1")
    fake = FakeExecutor()
    fake.respond(["docker", "image", "inspect"], output=LOCAL)
    digests = resolve_artifacts(load(root, "dev", release="r1+x", environ={}), fake)
    assert fake.argvs() == [
        (
            "docker",
            "build",
            "-t",
            "toy-app:r1_x",
            "-f",
            "docker/Dockerfile",
            "--build-arg",
            "PY=3.12",
            ".",
        ),
        ("docker", "image", "inspect", "toy-app:r1_x"),
    ]
    assert digests == {"app": "sha256:local"}


def test_production_pulls_and_pins_repo_digest(write_bundle: Writer) -> None:
    root = artifacts_bundle(write_bundle, build=BUILD, pull="ghcr.io/o/app:1")
    fake = FakeExecutor()
    fake.respond(["docker", "image", "inspect"], output=REMOTE)
    loaded = load(root, "prd", release="v1", environ={})
    digests = resolve_artifacts(loaded, fake)
    assert fake.argvs() == [
        ("docker", "pull", "ghcr.io/o/app:1"),
        ("docker", "image", "inspect", "ghcr.io/o/app:1"),
    ]
    assert artifact_env(loaded.manifest.artifacts, digests) == {
        "APP_IMAGE": "ghcr.io/o/app@sha256:remote"
    }


def test_production_build_only_is_refused(write_bundle: Writer) -> None:
    root = artifacts_bundle(write_bundle, build=BUILD)
    fake = FakeExecutor()
    with pytest.raises(ArtifactError, match="só entra por pull"):
        resolve_artifacts(load(root, "prd", release="v1", environ={}), fake)
    assert fake.calls == []


@pytest.mark.parametrize(
    ("target", "artifact", "verb", "error", "message"),
    [
        ("dev", {"build": BUILD}, "build", StepError, "build de app falhou"),
        (
            "prd",
            {"pull": "ghcr.io/o/app:1"},
            "pull",
            ArtifactError,
            r"pull da imagem ghcr.io/o/app:1(.|\n)*publique(.|\n)*"
            r"docker login ghcr.io(.|\n)*denied",
        ),
        (
            "prd",
            {"pull": "o/app:1"},
            "pull",
            ArtifactError,
            "docker login docker.io",
        ),
    ],
    ids=["build", "pull-registro", "pull-docker-hub"],
)
def test_failed_image_step_stops(
    write_bundle: Writer,
    target: str,
    artifact: dict[str, object],
    verb: str,
    error: type[Exception],
    message: str,
) -> None:
    root = artifacts_bundle(write_bundle, **artifact)
    fake = FakeExecutor()
    fake.respond(["docker", verb], code=1, output='Head "https://x": denied')
    with pytest.raises(error, match=message):
        resolve_artifacts(load(root, target, release="r1", environ={}), fake)


def test_a_secret_inside_the_digest_does_not_change_the_pinned_image(
    write_bundle: Writer,
) -> None:
    root = artifacts_bundle(write_bundle, pull="ghcr.io/o/app:1")
    fake = FakeExecutor()
    fake.hide(["remote"])
    fake.respond(["docker", "image", "inspect"], output=REMOTE)
    digests = resolve_artifacts(load(root, "dev", release="r1", environ={}), fake)
    assert "ghcr.io/o/app@sha256:remote" in json.dumps(digests, default=str)


def test_unreadable_inspect(write_bundle: Writer) -> None:
    root = artifacts_bundle(write_bundle, pull="ghcr.io/o/app:1")
    fake = FakeExecutor()
    fake.respond(["docker", "image", "inspect"], output="[]")
    with pytest.raises(ArtifactError, match="ilegível"):
        resolve_artifacts(load(root, "dev", release="r1", environ={}), fake)
