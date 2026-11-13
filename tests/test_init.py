from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from forge import VERSION
from forge.cli import main
from forge.load import load
from forge.scaffold import ScaffoldError, answers_for, forge_source


def init(root: Path, *extra: str) -> int:
    return main(["--root", str(root), "init", *extra])


@pytest.mark.parametrize(
    "origin,registry",
    [
        (None, "ghcr.io/example"),
        ("git@github.com:Acme-Org/loja.git", "ghcr.io/acme-org"),
        ("https://github.com/Acme/loja", "ghcr.io/acme"),
        ("https://gitlab.com/acme/loja.git", "ghcr.io/example"),
    ],
)
def test_empty_repo_gets_a_valid_app_bundle(
    tmp_path: Path,
    origin: str | None,
    registry: str,
) -> None:
    root = tmp_path / "my-app"
    root.mkdir()
    if origin is not None:
        for argv in (["init", "-q"], ["remote", "add", "origin", origin]):
            subprocess.run(["git", *argv], cwd=root, check=True)
    assert init(root, "app", "--no-input") == 0
    for relative in (
        "forge.yaml",
        "FORGE.md",
        "docs/debito-tecnico.md",
        ".github/workflows/forge.yml",
        "resources/app.service.yaml",
        "compose.yaml",
        "Dockerfile",
        "src/my_app/app.py",
        ".dockerignore",
    ):
        assert (root / relative).is_file(), relative
    ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {".forge/", "__pycache__/", ".venv/", ".pytest_cache/"} <= set(ignored)
    loaded = load(root, "prd", environ={}, release="v1")
    assert loaded.manifest.artifacts["app"].pull == f"{registry}/my-app:v1"
    assert loaded.target.git is not None and loaded.target.git.tag == "v*"
    assert loaded.target.git.branch is None


def test_second_init_overwrites_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert init(tmp_path, "--no-input") == 0
    (tmp_path / "forge.yaml").write_text("mine\n", encoding="utf-8")
    capsys.readouterr()
    assert init(tmp_path, "--no-input") == 0
    out = capsys.readouterr().out
    assert "criado" not in out
    assert "pulado     forge.yaml (já existe; nada foi sobrescrito)" in out
    assert (tmp_path / "forge.yaml").read_text(encoding="utf-8") == "mine\n"


def test_existing_repo_keeps_its_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "existente"
    (root / "src" / "loja").mkdir(parents=True)
    (root / "src" / "loja" / "__init__.py").write_text("", encoding="utf-8")
    project = '[project]\nname = "Loja"\nversion = "1.0"\n'
    (root / "pyproject.toml").write_text(project, encoding="utf-8")
    (root / "Dockerfile").write_text("FROM mine\n", encoding="utf-8")
    (root / ".gitignore").write_text("node_modules/\n.venv\n", encoding="utf-8")
    assert init(root, "--no-input") == 0
    out = capsys.readouterr().out
    assert "bundle loja (app)" in out
    for kept in ("Dockerfile", "pyproject.toml", "src/loja/__init__.py"):
        assert f"pulado     {kept} " in out
    assert "atualizado .gitignore" in out
    assert not (root / "src" / "existente").exists()
    assert (root / "Dockerfile").read_text(encoding="utf-8") == "FROM mine\n"
    assert (root / "pyproject.toml").read_text(encoding="utf-8") == project
    assert (root / ".gitignore").read_text(encoding="utf-8") == (
        "node_modules/\n.venv\n.forge/\n__pycache__/\n.pytest_cache/\n"
    )


def test_config_file_and_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "answers.yaml"
    config.write_text(
        "name: shop\nregistry: registry.example.com/team\n", encoding="utf-8"
    )
    asked: list[str] = []

    def fake_input(prompt: str) -> str:
        asked.append(prompt)
        return "9000" if prompt.startswith("porta") else ""

    monkeypatch.setattr("builtins.input", fake_input)
    root = tmp_path / "repo"
    assert init(root, "--config-file", str(config)) == 0
    assert not any("nome do bundle" in prompt for prompt in asked)
    loaded = load(root, "dev", environ={}, release="r1")
    assert loaded.name == "shop" and loaded.env["APP_PORT"] == "9000"
    assert loaded.variables["registry"] == "registry.example.com/team"


def test_unknown_config_key_fails(tmp_path: Path) -> None:
    config = tmp_path / "answers.yaml"
    config.write_text("colour: blue\n", encoding="utf-8")
    assert init(tmp_path / "repo", "--config-file", str(config), "--no-input") == 2
    with pytest.raises(ScaffoldError, match="colour"):
        answers_for("app", tmp_path, {"colour": "blue"}, None)


def test_workflow_triggers(tmp_path: Path) -> None:
    root = tmp_path / "workflow"
    assert init(root, "--no-input") == 0
    workflow = yaml.safe_load(
        (root / ".github/workflows/forge.yml").read_text(encoding="utf-8")
    )
    triggers = workflow[True]  # "on" vira True no YAML 1.1
    assert triggers["push"]["branches"] == ["develop"]
    assert "pull_request" in triggers and "workflow_dispatch" in triggers
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert jobs["prd"]["environment"] == "production"
    assert jobs["prd"]["if"] == (
        "github.event_name == 'workflow_dispatch' && vars.FORGE_RUNNER != ''"
    )
    assert jobs["dev"]["if"] == "github.event_name == 'push' && vars.FORGE_RUNNER != ''"
    for deploy in ("dev", "prd"):
        assert jobs[deploy]["runs-on"] == "${{ vars.FORGE_RUNNER }}"
    assert jobs["sem-runner"]["if"] == (
        "github.event_name != 'pull_request' && vars.FORGE_RUNNER == ''"
    )
    assert jobs["validar"]["runs-on"] == (
        "${{ github.event.pull_request.head.repo.full_name == github.repository"
        " && vars.FORGE_CI_RUNNER || 'ubuntu-latest' }}"
    )
    validate = {s.get("name"): s for s in jobs["validar"]["steps"]}
    assert validate["validate"]["run"] == "uv run forge validate --strict"
    steps = {s.get("name"): s for s in jobs["dev"]["steps"]}
    assert steps["deploy dev"]["run"] == "uv run forge deploy -t dev --yes"
    assert steps["plan prd"]["run"] == "uv run forge plan -t prd"
    assert (
        steps["instala o forge (grupo de dependências do pyproject.toml)"]["run"]
        == "uv sync --group forge"
    )


def test_vendor_copies_forge(tmp_path: Path) -> None:
    """`--vendor` continua existindo; deixou de ser o caminho recomendado, nao de funcionar."""
    assert init(tmp_path, "--no-input", "--vendor") == 0
    assert (tmp_path / "forge" / "cli.py").is_file()
    assert (tmp_path / "forge" / "archetypes" / "app" / "archetype.yaml").is_file()
    workflow = (tmp_path / ".github/workflows/forge.yml").read_text(encoding="utf-8")
    assert "vendorizado" in workflow
    assert "uv sync --group forge" not in workflow and "uv run forge" not in workflow
    project = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert "[dependency-groups]" in project and forge_source() not in project


def test_debt_file_carries_the_instruction(tmp_path: Path) -> None:
    assert init(tmp_path, "--no-input") == 0
    text = (tmp_path / "docs" / "debito-tecnico.md").read_text(encoding="utf-8")
    for phrase in (
        "Registre o atalho no ato",
        "Leia o débito antes de mexer na área",
        "Pague ao passar pelo código",
        "Gatilho vencido passa na frente de feature",
        "Pago sai do arquivo",
    ):
        assert phrase in text


def test_forge_md_teaches_the_installed_verb(tmp_path: Path) -> None:
    assert init(tmp_path, "--no-input") == 0
    text = (tmp_path / "FORGE.md").read_text(encoding="utf-8")
    assert "uv run forge validate" in text
    assert "python -m forge" not in text
    assert "uv sync --group forge" in text


def test_pyproject_declares_the_forge_group(tmp_path: Path) -> None:
    """O projeto vizinho instala o forge pelo grupo proprio, e o venv e do uv sync."""
    assert init(tmp_path, "pipeline", "--no-input") == 0
    project = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert "[dependency-groups]" in project
    assert forge_source() in project
    assert f'forge = ["{forge_source()}"]' in project


def test_forge_source_points_at_the_installed_version() -> None:
    """A tag que o projeto vizinho instala e a versao que o pyproject publica: a 2.1.4 saiu
    com VERSION ainda em 2.1.1, e todo init novo apontava para uma tag velha."""
    published = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert VERSION == published["project"]["version"]
    assert forge_source().endswith(f"@v{VERSION}")


def test_init_step_tells_the_installed_verb(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert init(tmp_path, "--no-input") == 0
    out = capsys.readouterr().out
    assert "uv run forge validate" in out
    assert "python -m forge" not in out


@pytest.mark.parametrize(
    "name,archetype,argv",
    [
        ("pyproject.toml", None, ["init", "--no-input"]),
        (".gitignore", None, ["init", "--no-input"]),
        ("config.yaml", None, ["init", "--no-input", "--config-file", "config.yaml"]),
        ("pyproject.toml", "app", ["validate"]),
        ("forge.yaml", "app", ["validate"]),
        (".gitignore", "app", ["validate"]),
        ("compose.yaml", "app", ["validate"]),
        ("docs/debito-tecnico.md", "app", ["validate"]),
        (".forge/secrets/dev.env", "automation", ["env", "-t", "dev"]),
    ],
)
def test_a_file_outside_utf8_is_a_one_line_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    archetype: str | None,
    argv: list[str],
) -> None:
    """O UTF-16 que o PowerShell grava terminava em traceback de defeito do forge."""
    monkeypatch.chdir(tmp_path)
    if archetype is not None:
        assert init(tmp_path, archetype, "--no-input") == 0
    (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / name).write_text('[project]\nname = "loja"\n', encoding="utf-16")
    capsys.readouterr()
    assert main(["--root", str(tmp_path), *argv]) == 2
    error = capsys.readouterr().err
    assert f"{Path(name)}: o arquivo não está em UTF-8" in error
    assert "Traceback" not in error
