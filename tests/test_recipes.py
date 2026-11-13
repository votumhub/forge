from __future__ import annotations

import ast
import json
import re
import shlex
import sys
from pathlib import Path

import pytest
import yaml

from forge.cli import main
from forge.load import load
from forge.model import Job, parse
from forge.policy import generated_by
from forge.scaffold import RECIPES, load_recipe, recipe_names, render_tree
from tests.test_foundation import HOUSE_NAMES

PROVEN = {"mssql", "duckdb", "dbt-core", "prefect", "postgres"}
CONTEXT = {
    "name": "toy-bundle",
    "package": "toy_bundle",
    "python": "3.12",
    "dev_branch": "develop",
    "vendored": False,
}
COMMENT = {
    ".py": ("#", '"""'),
    ".yaml": ("#",),
    ".yml": ("#",),
    ".toml": ("#",),
    ".sql": ("--",),
    ".service": ("#",),
    ".timer": ("#",),
    ".conf": ("#",),
    ".cfg": ("#",),
    ".properties": ("#",),
    ".json": ("//",),
    ".ini": ("#", ";"),
    "": ("#",),
}
DISTRIBUTION = {
    "airflow": "apache-airflow",
    "clickhouse_connect": "clickhouse-connect",
    "confluent_kafka": "confluent-kafka",
    "nats": "nats-py",
    "soda_core": "soda-core",
    "yaml": "pyyaml",
}


def rendered(name: str) -> dict[str, str]:
    return render_tree(RECIPES / name / "files", CONTEXT)


def third_party(text: str) -> set[str]:
    nodes = list(ast.walk(ast.parse(text)))
    tops = {
        node.module.split(".")[0]
        for node in nodes
        if isinstance(node, ast.ImportFrom) and node.module and not node.level
    } | {
        alias.name.split(".")[0]
        for node in nodes
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    return {
        DISTRIBUTION.get(top, top)
        for top in tops - set(sys.stdlib_module_names) - {CONTEXT["package"]}
    }


def distribution(requirement: str) -> str:
    name = re.split(r"[\[<>=!~;\s]", requirement, maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def test_catalog_covers_proven_and_carries_no_house_name() -> None:
    names = set(recipe_names())
    assert PROVEN <= names
    assert not [name for name in names if HOUSE_NAMES.search(name)]
    assert {n for n in names if load_recipe(n).provada} == PROVEN


@pytest.mark.parametrize("name", recipe_names())
def test_recipe_renders_compiles_and_loads(name: str) -> None:
    recipe = load_recipe(name)
    files = rendered(name)
    assert files, f"receita {name} sem arquivos"
    imported: set[str] = set()
    for relative, text in files.items():
        assert not HOUSE_NAMES.search(text), f"{name}:{relative} tem nome proibido"
        suffix = Path(relative).suffix
        if suffix == ".py":
            compile(text, relative, "exec")
            imported |= third_party(text)
        if suffix in (".yaml", ".yml"):
            list(yaml.safe_load_all(text))
        first = next(line for line in text.splitlines() if line.strip())
        markers = COMMENT.get(suffix)
        if markers is not None:
            assert first.lstrip().startswith(markers), (
                f"{name}:{relative} sem comentário de topo"
            )
        assert "Documentação" in text, f"{name}:{relative} sem link da documentação"
        if relative.startswith("resources/"):
            headed = {
                generated_by(lead + text)
                for lead in ("", "\ufeff", "\r\n\n", "\ufeff \t\r\n")
            }
            assert headed == {name}, f"{name}:{relative} sem # Receita {name}"
    assert recipe.docs.url.startswith("https://")
    assert {distribution(r) for r in recipe.requires} == imported, (
        f"{name}: requires do recipe.yaml difere dos imports de terceiros"
    )


def test_init_recipe_adds_files_to_existing_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--root", str(tmp_path), "init", "--no-input"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "init",
                "--recipe",
                "duckdb",
                "--recipe",
                "mssql",
                "--recipe",
                "dagster",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "só receitas" in out
    assert "MSSQL_PASSWORD" in out
    assert 'uv add "pymssql>=2.3"' in out
    assert "dagster>=1.8" in out and 'uv add "dagster' not in out
    package = next((tmp_path / "src").iterdir()).name
    assert (tmp_path / "src" / package / "engine" / "duckdb_connection.py").is_file()
    assert (tmp_path / "src" / package / "sources" / "mssql.py").is_file()


def test_unknown_recipe(tmp_path: Path) -> None:
    assert (
        main(["--root", str(tmp_path), "init", "--no-input", "--recipe", "nope"]) == 2
    )


def test_recipes_command_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["recipes", "-o", "json"]) == 0
    items = json.loads(capsys.readouterr().out)
    assert {i["name"] for i in items if i["provada"]} == PROVEN
    assert all(
        {"description", "family", "env", "docs", "provada"} <= set(i) for i in items
    )


@pytest.mark.parametrize("name", recipe_names())
def test_recipe_deploy_jobs_point_at_rendered_modules(name: str) -> None:
    files = rendered(name)
    for relative, text in files.items():
        if not relative.endswith(".job.yaml"):
            continue
        job = parse(Job, yaml.safe_load(text), relative)
        for task in job.tasks:
            module = task.run[task.run.index("-m") + 1]
            assert f"src/{module.replace('.', '/')}.py" in files, f"{name}: {task.run}"


def test_init_adds_recipe_deploy_jobs_and_keeps_existing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--root", str(tmp_path), "init", "--no-input"]) == 0
    mine = tmp_path / "resources" / "minio-bootstrap.job.yaml"
    mine.write_text(
        "# meu\ntrigger: {deploy: {when: after_up}}\ntasks: [{key: a, run: [make]}]\n",
        encoding="utf-8",
    )
    manifest_before = (tmp_path / "forge.yaml").read_text(encoding="utf-8")
    capsys.readouterr()
    for _ in range(2):
        assert (
            main(
                [
                    "--root",
                    str(tmp_path),
                    "init",
                    "--recipe",
                    "minio",
                    "--recipe",
                    "n8n",
                    "--recipe",
                    "local-fs",
                ]
            )
            == 0
        )
    out = capsys.readouterr().out
    assert "pulado     resources/minio-bootstrap.job.yaml" in out
    assert mine.read_text(encoding="utf-8").startswith("# meu")
    assert (tmp_path / "forge.yaml").read_text(encoding="utf-8") == manifest_before
    loaded = load(tmp_path, "dev", environ={}, release="r1")
    assert loaded.jobs["n8n-apply"].on_deploy == "after_up"
    assert loaded.jobs["local-fs-bootstrap"].on_deploy == "before_up"
    package = next((tmp_path / "src").iterdir()).name
    assert loaded.jobs["n8n-apply"].tasks[0].run == [
        "python",
        "-m",
        f"{package}.orchestration.n8n_apply",
    ]


@pytest.mark.parametrize("name", ["metabase", "n8n", "minio", "superset", "redpanda"])
def test_pinned_recipe_images_are_cited_on_top(name: str) -> None:
    text = rendered(name)[f"compose/{name}.yaml"]
    images = [
        line.split("image:", 1)[1].strip()
        for line in text.splitlines()
        if line.strip().startswith("image:")
    ]
    top = "".join(line for line in text.splitlines() if line.startswith("#"))
    own = [image for image in images if not image.startswith("postgres:")]
    assert own and all("${" not in i and not i.endswith(":latest") for i in images)
    assert all(f"Versão fixada: {image}" in top for image in own)


@pytest.mark.parametrize(
    "name,prefix", [("prefect", "PREFECT"), ("dagster", "DAGSTER")]
)
def test_orchestrator_ui_is_published_on_loopback_by_default(
    name: str, prefix: str
) -> None:
    compose = yaml.safe_load(rendered(name)[f"compose/{name}.yaml"])
    ports = [
        port
        for service in compose["services"].values()
        for port in service.get("ports", [])
    ]
    assert ports
    assert all(port.startswith(f"${{{prefix}_BIND:-127.0.0.1}}:") for port in ports)
    assert f"{prefix}_BIND" in load_recipe(name).env


def test_scheduled_command_is_accepted_on_the_default_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """O passo "roda" chamava `forge env` sem --yes e o alvo padrão é o prd, production: o
    forge env recusava e a agenda nunca rodava."""
    argv = ["--root", str(tmp_path), "init", "--no-input"]
    assert main([*argv, "--recipe", "github-actions-cron"]) == 0
    path = tmp_path / ".github/workflows/github-actions-cron.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    default = re.search(r"\|\| '([^']+)'", workflow["env"]["CRON_TARGET"])
    assert default is not None
    step = next(s for s in workflow["jobs"]["run"]["steps"] if s.get("name") == "roda")
    run = shlex.split(step["run"].replace("$CRON_TARGET", default.group(1)))
    forge_args = run[run.index("env") : run.index("--") + 1]
    command = [sys.executable, "-c", "raise SystemExit(7)"]
    capsys.readouterr()
    assert main(["--root", str(tmp_path), *forge_args, *command]) == 7, (
        capsys.readouterr().err
    )
