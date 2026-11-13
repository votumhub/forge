"""Artifacts: imagem construída (development) ou puxada (production), fixada por digest."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping

from forge.contracts import Executor
from forge.errors import ForgeError
from forge.executor import OUTPUT_TAIL, require_ok
from forge.load import Loaded
from forge.model import Artifact, Build

UNSAFE_TAG = re.compile(r"[^A-Za-z0-9_.-]")


class ArtifactError(ForgeError):
    pass


def resolve_artifacts(loaded: Loaded, executor: Executor) -> dict[str, str]:
    """{nome do artifact: referência fixada} na ordem declarada."""
    return {
        name: _resolve(loaded, executor, name, artifact)
        for name, artifact in loaded.manifest.artifacts.items()
    }


def artifact_env(
    artifacts: Mapping[str, Artifact], digests: Mapping[str, str]
) -> dict[str, str]:
    return {
        artifacts[name].env: digest
        for name, digest in digests.items()
        if name in artifacts
    }


def local_tag(loaded: Loaded, name: str) -> str:
    return built_tag(loaded, name, loaded.release)


def built_tag(loaded: Loaded, name: str, release: str) -> str:
    """A etiqueta que o forge põe na imagem que ele constrói: só ela é dele para remover."""
    return f"{built_repository(loaded, name)}:{UNSAFE_TAG.sub('_', release)}"


def built_repository(loaded: Loaded, name: str) -> str:
    return f"{loaded.name}-{name}"


def built_artifacts(loaded: Loaded) -> list[str]:
    """Os artifacts que o forge constrói neste alvo; em production nada é construído."""
    if loaded.mode != "development":
        return []
    return [n for n, a in loaded.manifest.artifacts.items() if a.build is not None]


def remove_built_images(
    loaded: Loaded, executor: Executor, releases: Iterable[str]
) -> list[str]:
    """Remove as imagens construídas pelo forge para essas releases; o que falha vira linha
    do relatório, nunca erro: imagem que sobra não derruba o deploy."""
    chosen = set(releases)
    lines: list[str] = []
    for name in built_artifacts(loaded):
        present, listing = _present_tags(loaded, executor, name)
        lines += listing
        wanted = {built_tag(loaded, name, release) for release in chosen}
        lines += [_remove(loaded, executor, tag) for tag in sorted(wanted & present)]
    return lines


def _present_tags(
    loaded: Loaded, executor: Executor, name: str
) -> tuple[set[str], list[str]]:
    repository = built_repository(loaded, name)
    argv = ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}", repository]
    result = executor.run(argv, cwd=loaded.root)
    if not result.ok:
        return set(), [
            f"imagens de {repository}: não listadas ({_last_line(result.output)})"
        ]
    return {line.strip() for line in result.output.splitlines() if line.strip()}, []


def _remove(loaded: Loaded, executor: Executor, tag: str) -> str:
    result = executor.run(["docker", "image", "rm", tag], cwd=loaded.root)
    if result.ok:
        return f"imagem {tag}: removida"
    return f"imagem {tag}: não removida ({_last_line(result.output)})"


def _last_line(output: str) -> str:
    lines = [line for line in output.splitlines() if line.strip()]
    return lines[-1].strip() if lines else "sem saída do docker"


def build_argv(tag: str, build: Build) -> list[str]:
    argv = ["docker", "build", "-t", tag, "-f", build.dockerfile]
    for key, value in build.args.items():
        argv += ["--build-arg", f"{key}={value}"]
    return [*argv, build.context]


def _resolve(loaded: Loaded, executor: Executor, name: str, artifact: Artifact) -> str:
    if loaded.mode == "production" and artifact.pull is None:
        raise ArtifactError(
            f"artifact {name}: em production a imagem só entra por pull; declare pull"
        )
    if loaded.mode == "development" and artifact.build is not None:
        ref = local_tag(loaded, name)
        require_ok(
            executor.run(build_argv(ref, artifact.build), cwd=loaded.root),
            f"build de {name}",
        )
    else:
        ref = str(artifact.pull)
        _pull(executor, loaded, name, ref)
    return pinned(executor, loaded, name, ref)


def _pull(executor: Executor, loaded: Loaded, name: str, ref: str) -> None:
    result = executor.run(["docker", "pull", ref], cwd=loaded.root)
    if result.ok:
        return
    raise ArtifactError(
        f"pull da imagem {ref} (artifact {name}) falhou: o registro negou acesso, não "
        f"respondeu ou a imagem não existe lá.\n"
        f"próximos passos: publique no registro a imagem desta versão ({ref}, o pull de "
        f"artifacts.{name} no forge.yaml) ou faça login no registro "
        f"(docker login {registry(ref)}).\n"
        f"fim da saída do docker:\n{result.output[-OUTPUT_TAIL:]}"
    )


def registry(ref: str) -> str:
    """Host do registro na referência da imagem; sem host explícito, o Docker Hub."""
    first, slash, _ = ref.partition("/")
    explicit = slash and ("." in first or ":" in first or first == "localhost")
    return first if explicit else "docker.io"


def pinned(executor: Executor, loaded: Loaded, name: str, ref: str) -> str:
    """RepoDigests[0] quando a imagem veio de registry; senão o .Id local."""
    result = require_ok(
        executor.run(["docker", "image", "inspect", ref], cwd=loaded.root),
        f"inspect de {name}",
    )
    try:
        image = json.loads(result.output)[0]
        digests = image.get("RepoDigests") or []
        return str(digests[0] if digests else image["Id"])
    except (
        json.JSONDecodeError,
        IndexError,
        KeyError,
        TypeError,
        AttributeError,
    ) as error:
        raise ArtifactError(
            f"artifact {name}: docker image inspect devolveu algo ilegível"
        ) from error
