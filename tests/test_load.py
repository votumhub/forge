from __future__ import annotations

import io
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from forge.cli import main
from forge.gitinfo import GitInfo
from forge.load import load, overlay
from forge.model import ManifestError
from tests.support import Writer, manifest


def no_git(_: Path) -> GitInfo | None:
    return None


def fixed_git(tag: str | None = None, dirty: bool = False) -> Callable[[Path], GitInfo]:
    info = GitInfo(sha="a" * 40, tag=tag, branch="main", dirty=dirty)
    return lambda _root: info


def test_unknown_field_in_resource_names_file_and_path(write_bundle: Writer) -> None:
    root = write_bundle(
        manifest(include=["resources/*.yaml"]),
        {"resources/web.service.yaml": {"compose": ["compose.yaml"], "replicas": 2}},
    )
    with pytest.raises(
        ManifestError,
        match=r"resources/web.service.yaml[\s\S]*replicas: campo desconhecido",
    ):
        load(root, environ={}, git_reader=no_git)


def test_default_target_is_picked_and_unknown_target_fails(
    write_bundle: Writer,
) -> None:
    root = write_bundle(manifest())
    assert load(root, environ={}, git_reader=no_git).target_name == "dev"
    with pytest.raises(ManifestError, match="targets.qa não existe"):
        load(root, "qa", environ={}, git_reader=no_git)


def test_overlay_merges_maps_and_replaces_scalars_and_lists() -> None:
    base = {"a": {"x": 1, "y": [1, 2]}, "b": "keep", "c": "old"}
    top = {"a": {"y": [3]}, "c": "new"}
    assert overlay(base, top) == {"a": {"x": 1, "y": [3]}, "b": "keep", "c": "new"}


def test_target_env_overlays_bundle_env(write_bundle: Writer) -> None:
    data = manifest(env={"LEVEL": "info", "KEEP": "1"})
    data["targets"]["dev"]["env"] = {"LEVEL": "debug"}  # type: ignore[index]
    loaded = load(write_bundle(data), environ={}, git_reader=no_git)
    assert loaded.env == {"LEVEL": "debug", "KEEP": "1"}


@pytest.mark.parametrize(
    ("cli", "environ", "target", "expected"),
    [
        ({"size": "cli"}, {"FORGE_VAR_size": "env"}, "tgt", "cli"),
        ({}, {"FORGE_VAR_size": "env"}, "tgt", "env"),
        ({}, {}, "tgt", "tgt"),
        ({}, {}, None, "default"),
    ],
)
def test_variable_precedence(
    write_bundle: Writer,
    cli: dict[str, str],
    environ: dict[str, str],
    target: str | None,
    expected: str,
) -> None:
    data = manifest(variables={"size": {"default": "default"}})
    if target:
        data["targets"]["dev"]["variables"] = {"size": target}  # type: ignore[index]
    loaded = load(write_bundle(data), cli_vars=cli, environ=environ, git_reader=no_git)
    assert loaded.variables["size"] == expected


def test_cli_var_not_declared_fails(write_bundle: Writer) -> None:
    with pytest.raises(ManifestError, match=r"--var ghost: variável não declarada"):
        load(
            write_bundle(manifest()),
            cli_vars={"ghost": "1"},
            environ={},
            git_reader=no_git,
        )


def test_env_var_not_declared_fails(write_bundle: Writer) -> None:
    with pytest.raises(ManifestError, match=r"FORGE_VAR_ghost: variável não declarada"):
        load(
            write_bundle(manifest()),
            environ={"FORGE_VAR_ghost": "1"},
            git_reader=no_git,
        )


def test_target_var_not_declared_names_file_and_field(write_bundle: Writer) -> None:
    data = manifest()
    data["targets"]["dev"]["variables"] = {"ghost": "1"}  # type: ignore[index]
    with pytest.raises(ManifestError, match=r"forge.yaml: targets.dev.variables.ghost"):
        load(write_bundle(data), environ={}, git_reader=no_git)


def test_variable_without_value_names_file_and_field(write_bundle: Writer) -> None:
    data = manifest(variables={"region": {"description": "onde"}})
    with pytest.raises(
        ManifestError, match=r"forge.yaml: variables.region sem valor para o alvo dev"
    ):
        load(write_bundle(data), environ={}, git_reader=no_git)


def test_var_interpolates_in_env_and_resources(write_bundle: Writer) -> None:
    data = manifest(
        include=["resources/*.yaml"],
        variables={
            "port": {"default": "8080"},
            "url": {"default": "http://x:${var.port}"},
        },
        env={"URL": "${var.url}"},
    )
    service = {"compose": ["compose.yaml"], "health": {"url": "${var.url}/health"}}
    root = write_bundle(data, {"resources/web.service.yaml": service})
    loaded = load(root, environ={}, git_reader=no_git)
    assert loaded.env["URL"] == "http://x:8080"
    health = loaded.services["web"].health
    assert health is not None and health.url == "http://x:8080/health"


def test_bundle_references(write_bundle: Writer) -> None:
    data = manifest(
        env={"A": "${bundle.name}/${bundle.target}/${bundle.release}/${bundle.root}"}
    )
    root = write_bundle(data)
    loaded = load(root, release="v9", environ={}, git_reader=no_git)
    assert loaded.env["A"] == f"toy/dev/v9/{root.resolve().as_posix()}"


def test_git_sha_reference(write_bundle: Writer) -> None:
    root = write_bundle(manifest(env={"SHA": "${git.sha}"}))
    assert load(root, environ={}, git_reader=fixed_git()).env["SHA"] == "a" * 40
    with pytest.raises(ManifestError, match="git.sha"):
        load(root, environ={}, git_reader=no_git)


def test_double_dollar_escapes(write_bundle: Writer) -> None:
    loaded = load(
        write_bundle(manifest(env={"RAW": "$${var.x}"})), environ={}, git_reader=no_git
    )
    assert loaded.env["RAW"] == "${var.x}"


def test_name_without_dot_passes_intact(write_bundle: Writer) -> None:
    loaded = load(
        write_bundle(manifest(env={"P": "${HOME}/x"})), environ={}, git_reader=no_git
    )
    assert loaded.env["P"] == "${HOME}/x"


def test_variable_cycle_fails(write_bundle: Writer) -> None:
    data = manifest(
        variables={"a": {"default": "${var.b}"}, "b": {"default": "${var.a}"}}
    )
    with pytest.raises(
        ManifestError, match="ciclo de variáveis: var.a -> var.b -> var.a"
    ):
        load(write_bundle(data), environ={}, git_reader=no_git)


def test_unknown_reference_names_field(write_bundle: Writer) -> None:
    root = write_bundle(manifest(env={"X": "${bundle.owner}"}))
    with pytest.raises(
        ManifestError, match=r"env.X: referência desconhecida \$\{bundle.owner\}"
    ):
        load(root, environ={}, git_reader=no_git)
    root = write_bundle(manifest(env={"X": "${var.nope}"}))
    with pytest.raises(ManifestError, match=r"env.X: \$\{var.nope\} não declarada"):
        load(root, environ={}, git_reader=no_git)


def test_release_prefers_flag_then_tag_then_short_sha(write_bundle: Writer) -> None:
    root = write_bundle(manifest())
    tagged = fixed_git(tag="v1.2.0")
    assert load(root, release="r1", environ={}, git_reader=tagged).release == "r1"
    assert load(root, environ={}, git_reader=tagged).release == "v1.2.0"
    assert load(root, environ={}, git_reader=fixed_git()).release == "a" * 12
    assert load(root, environ={}, git_reader=no_git).release == "unversioned"


def test_dirty_suffix_only_in_development(write_bundle: Writer) -> None:
    root = write_bundle(manifest())
    dirty = fixed_git(dirty=True)
    assert (
        load(root, "dev", environ={}, git_reader=dirty).release == "a" * 12 + "-dirty"
    )
    assert load(root, "prd", environ={}, git_reader=dirty).release == "a" * 12


def test_real_git_tag_and_forge_dir_ignored(write_bundle: Writer) -> None:
    root = write_bundle(manifest())
    identity = ["-c", "user.email=t@example.com", "-c", "user.name=t"]
    for argv in (
        ["init", "-q"],
        ["add", "."],
        [*identity, "commit", "-qm", "x"],
        ["tag", "v0.1"],
    ):
        subprocess.run(["git", *argv], cwd=root, check=True)
    (root / ".forge").mkdir()
    (root / ".forge" / "x").write_text("x")
    loaded = load(root, environ={})
    assert loaded.release == "v0.1"
    assert loaded.git is not None and not loaded.git.dirty


def test_include_rejects_bad_file_name_and_empty_match(write_bundle: Writer) -> None:
    root = write_bundle(
        manifest(include=["resources/*.yaml"]), {"resources/web.yaml": {}}
    )
    with pytest.raises(ManifestError, match="nome de recurso inválido"):
        load(root, environ={}, git_reader=no_git)
    root = write_bundle(manifest(include=["nothing/*.yaml"]))
    with pytest.raises(ManifestError, match="não casou"):
        load(root, environ={}, git_reader=no_git)


def test_job_resource_and_derived_paths(write_bundle: Writer) -> None:
    root = write_bundle(
        manifest(include=["resources/*.yaml"]),
        {
            "resources/daily.job.yaml": {
                "trigger": {"manual": True},
                "flow": "src/p/flows.py:daily",
                "work_pool": "pool",
            }
        },
    )
    loaded = load(root, environ={}, git_reader=no_git)
    assert loaded.jobs["daily"].work_pool == "pool"
    assert loaded.project == "toy-dev"
    assert loaded.env_file == root.resolve() / ".forge" / "env" / "dev.env"
    assert loaded.state_path == root.resolve() / ".forge" / "state"


def test_two_defaults_fail(write_bundle: Writer) -> None:
    data = manifest()
    data["targets"]["prd"]["default"] = True  # type: ignore[index]
    with pytest.raises(ManifestError, match="mais de um alvo com default"):
        load(write_bundle(data), environ={}, git_reader=no_git)


class ClosedPipe(io.StringIO):
    def flush(self) -> None:
        raise OSError(22, "Invalid argument")


@pytest.mark.parametrize(
    "verb", [["validate"], ["plan"], ["summary"], ["deploy", "-t", "prd", "--yes"]]
)
def test_folder_without_manifest_points_to_init(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    verb: list[str],
) -> None:
    """O stdout cujo leitor fechou o pipe não troca a linha de erro por traceback."""
    monkeypatch.setattr(sys, "stdout", ClosedPipe())
    assert main(["--root", str(tmp_path), *verb]) == 2
    error = capsys.readouterr().err
    assert "não há forge.yaml" in error
    assert "`forge init`" in error and "--root <pasta>" in error
