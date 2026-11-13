from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from forge.cli import main
from forge.commands.validate import judge
from forge.debt import parse_debt
from forge.deploy import ProductionCheckError, production_checks
from forge.gitinfo import GitInfo
from forge.load import load
from forge.policy import check_targets
from tests.support import Writer, manifest

TODAY = date(2026, 9, 23)
GITIGNORE = {".gitignore": ".forge/\n.env\n"}

LEGACY_DEBT = """# Débito técnico

Texto de abertura com exemplo cercado que não conta:

```
## ENV004
- regra: o que a regra exige
- motivo: por que não dá para cumprir hoje
- dono: quem responde por isso
- mitigacao: o que fazemos enquanto não está resolvido
```

## GEN001 - artefatos escritos a mao que o forge ainda nao gera

- regra: todo arquivo que uma spec declara gerar existe no disco e e identico ao que o
  render produz
- motivo: os arquivos foram escritos a mao antes de existir spec, e trocar os arquivos
  vivos pelos gerados e migracao, nao declaracao
- dono: fulano
- mitigacao: os arquivos escritos a mao continuam sendo a verdade operacional


## SEG001 - quatro avisos aceitos

- regra: nenhum padrao inseguro conhecido
- motivo: quatro deles nao se aplicam
- dono: fulano
- mitigacao: cada aviso aceito virou uma regra executavel mais estreita
"""


def rules(root: Path, target: str | None = None) -> list[str]:
    names = [target] if target else ["dev", "prd"]
    loaded = [load(root, name, environ={}, release="r1") for name in names]
    return sorted(f"{f.rule}:{f.where}" for f in check_targets(loaded))


def test_clean_bundle_has_no_findings(write_bundle: Writer) -> None:
    assert rules(write_bundle(manifest(), GITIGNORE)) == []


def test_production_requires_git(write_bundle: Writer) -> None:
    data = manifest()
    del data["targets"]["prd"]["git"]  # type: ignore[index]
    assert rules(write_bundle(data, GITIGNORE)) == ["POL001:targets.prd"]


def test_secret_looking_literal(write_bundle: Writer) -> None:
    data = manifest(
        env={
            "DB_PASSWORD": "hunter2",
            "OK": "secret://X",
            "URL": "postgres://user:pw@host/db",
        }
    )
    data["targets"]["dev"]["secrets"] = {"provider": "env"}  # type: ignore[index]
    data["targets"]["prd"]["secrets"] = {"provider": "env"}  # type: ignore[index]
    found = rules(write_bundle(data, GITIGNORE), "dev")
    assert found == ["POL002:targets.dev.env.DB_PASSWORD", "POL002:targets.dev.env.URL"]


def test_production_only_pull(write_bundle: Writer) -> None:
    data = manifest(
        include=["resources/*.yaml"], artifacts={"app": {"env": "IMG", "build": {}}}
    )
    root = write_bundle(
        data,
        {
            **GITIGNORE,
            "compose.yaml": "services:\n  web: {image: $IMG}\n",
            "resources/web.service.yaml": {"compose": ["compose.yaml"]},
        },
    )
    assert rules(root) == ["POL003:artifacts.app"]


def test_artifact_no_compose_uses(write_bundle: Writer) -> None:
    data = manifest(
        include=["resources/*.yaml"],
        artifacts={
            "app": {"env": "APP_IMAGE", "build": {}, "pull": "r/app:1"},
            "orphan": {"env": "APP", "build": {}, "pull": "r/orphan:1"},
        },
    )
    root = write_bundle(
        data,
        {
            **GITIGNORE,
            "compose.yaml": 'services:\n  web: {image: "${APP_IMAGE}"}\n',
            "resources/web.service.yaml": {"compose": ["compose.yaml"]},
        },
    )
    assert rules(root) == ["POL009:artifacts.orphan"]


def test_same_compose_service_in_two_resources_of_one_project(
    write_bundle: Writer,
) -> None:
    db = "services:\n  db: {image: postgres}\n"
    root = write_bundle(
        manifest(include=["resources/*.yaml"]),
        {
            **GITIGNORE,
            "web.yaml": db + "  web: {image: nginx}\n",
            "other.yaml": db,
            "own.yaml": db,
            "resources/a.service.yaml": {"compose": ["web.yaml"]},
            "resources/b.service.yaml": {"compose": ["other.yaml"]},
            "resources/c.service.yaml": {"compose": ["own.yaml"], "project": "apart"},
        },
    )
    assert rules(root) == ["POL008:resources/b.service.yaml"]


def test_envelope_and_dotenv_must_be_ignored(write_bundle: Writer) -> None:
    data = manifest()
    data["targets"]["dev"]["secrets"] = {"provider": "dotenv", "path": "secrets.env"}  # type: ignore[index]
    found = rules(write_bundle(data, {".gitignore": ".env\n"}), "dev")
    assert found == ["POL004:targets.dev", "POL004:targets.dev"]


def test_two_targets_same_host_project_state(write_bundle: Writer) -> None:
    data = manifest()
    for name in ("dev", "prd"):
        data["targets"][name]["project"] = "same"  # type: ignore[index]
    assert rules(write_bundle(data, GITIGNORE)) == ["POL005:targets.prd"]


def test_release_services_must_exist(write_bundle: Writer) -> None:
    compose = "services:\n  web: {image: nginx}\n"
    service = {"compose": ["compose.yaml"], "release_services": ["web", "api"]}
    root = write_bundle(
        manifest(include=["resources/*.yaml"]),
        {**GITIGNORE, "compose.yaml": compose, "resources/web.service.yaml": service},
    )
    assert rules(root, "dev") == ["POL006:resources/web.service.yaml"]


def test_what_an_included_compose_declares_counts(write_bundle: Writer) -> None:
    """O compose principal que só junta peças por `include:` era lido vazio: o serviço e a
    imagem de uma peça incluída viravam POL006 e POL009, e a peça que falta passava calada."""
    data = manifest(
        include=["resources/*.yaml"],
        artifacts={"app": {"env": "APP_IMAGE", "build": {}, "pull": "r/app:1"}},
    )
    root = write_bundle(
        data,
        {
            **GITIGNORE,
            "compose.yaml": "include:\n  - path: pecas/web.${FORGE_TARGET:-x}.yaml\n",
            "pecas/web.dev.yaml": 'services:\n  web: {image: "${APP_IMAGE}"}\n',
            "resources/web.service.yaml": {
                "compose": ["compose.yaml"],
                "release_services": ["web"],
            },
        },
    )
    assert rules(root, "dev") == []

    (root / "pecas/web.dev.yaml").unlink()
    assert rules(root, "dev") == [
        "POL007:resources/web.service.yaml",
        "POL009:artifacts.app",
    ]


def test_missing_compose_file(write_bundle: Writer) -> None:
    root = write_bundle(
        manifest(include=["resources/*.yaml"]),
        {**GITIGNORE, "resources/web.service.yaml": {"compose": ["nope.yaml"]}},
    )
    assert rules(root, "dev") == ["POL007:resources/web.service.yaml"]


def test_legacy_debt_format_stays_valid() -> None:
    entries = parse_debt(LEGACY_DEBT)
    assert [e.rule for e in entries] == ["GEN001", "SEG001"]
    assert entries[0].title == "artefatos escritos a mao que o forge ainda nao gera"
    assert entries[0].fields["regra"].endswith("identico ao que o render produz")
    assert all(not e.missing() for e in entries)


def test_debt_without_reason_fails_and_excuses_nothing(write_bundle: Writer) -> None:
    data = manifest()
    del data["targets"]["prd"]["git"]  # type: ignore[index]
    root = write_bundle(data, GITIGNORE)
    loaded = [load(root, n, environ={}, release="r1") for n in ("dev", "prd")]
    debt = parse_debt(
        "## POL001 - sem git\n- regra: prd exige git\n- mitigacao: nada\n"
    )
    verdict = judge(loaded, debt, strict=False, today=TODAY)
    assert sorted(f.rule for f in verdict.failing) == ["DEB001", "POL001"]


def test_debt_excuses_rule_and_instance(write_bundle: Writer) -> None:
    data = manifest()
    del data["targets"]["prd"]["git"]  # type: ignore[index]
    root = write_bundle(data, GITIGNORE)
    loaded = [load(root, n, environ={}, release="r1") for n in ("dev", "prd")]
    other = parse_debt(
        "## POL001\n- regra: r\n- motivo: m\n- mitigacao: x\n- instancia: targets.hml\n"
    )
    assert [f.rule for f in judge(loaded, other, False, TODAY).failing] == ["POL001"]
    right = parse_debt(
        "## POL001\n- regra: r\n- motivo: m\n- mitigacao: x\n- instancia: targets.prd\n"
    )
    verdict = judge(loaded, right, False, TODAY)
    assert verdict.failing == [] and len(verdict.excused) == 1


def test_overdue_trigger_is_warning_and_strict_fails(write_bundle: Writer) -> None:
    root = write_bundle(manifest(), GITIGNORE)
    loaded = [load(root, "dev", environ={}, release="r1")]
    debt = parse_debt(
        "## ABC001\n- regra: r\n- motivo: m\n- mitigacao: x\n- gatilho: 2026-01-01\n"
    )
    assert [f.rule for f in judge(loaded, debt, False, TODAY).passing] == ["DEB002"]
    assert [f.rule for f in judge(loaded, debt, True, TODAY).failing] == ["DEB002"]


def test_cli_validate_exit_codes(
    write_bundle: Writer, capsys: pytest.CaptureFixture[str]
) -> None:
    root = write_bundle(manifest(), GITIGNORE)
    assert main(["--root", str(root), "validate", "--strict"]) == 0
    data = manifest()
    del data["targets"]["prd"]["git"]  # type: ignore[index]
    write_bundle(data, GITIGNORE)
    assert main(["--root", str(root), "validate"]) == 1
    assert "POL001" in capsys.readouterr().out
    assert main(["--root", str(root), "validate", "-t", "dev"]) == 0


def test_four_type_rules(write_bundle: Writer) -> None:
    data = manifest(include=["resources/*.yaml"])
    data["targets"]["prd"]["proxy"] = {
        "kind": "nginx",
        "config_dir": "p",  # type: ignore[index]
        "reload": ["nginx", "-s", "reload"],
    }
    root = write_bundle(
        data,
        {
            **GITIGNORE,
            "compose.yaml": "services:\n  web: {image: x}\n",
            "resources/web.service.yaml": {"compose": ["compose.yaml"]},
            "resources/site.route.yaml": {
                "service": "ghost",
                "port": 80,
                "host": "a.b",
            },
            "resources/db.storage.yaml": {
                "kind": "database",
                "ensure": ["make", "db"],
                "backup": {"job": "dump", "restore": ["make", "r"]},
            },
            "resources/setup.job.yaml": {
                "trigger": {"deploy": {"when": "after_up"}},
                "tasks": [{"key": "m", "run": ["m"], "service": "nope"}],
            },
        },
    )
    route, storage, job = (
        "resources/site.route.yaml",
        "resources/db.storage.yaml",
        "resources/setup.job.yaml",
    )
    common = [f"POL010:{route}", f"POL011:{storage}", f"POL014:{job}"]
    assert rules(root, "dev") == sorted([*common, f"POL012:{route}"])
    assert rules(root, "prd") == sorted([*common, f"POL013:{route}"])


def test_example_registry_warns_fails_strict_and_blocks_production(
    tmp_path: Path,
) -> None:
    """O molde app sem resposta para registry deixava prd puxando de ghcr.io/example: o
    validate estrito passava e o deploy de prd só morria no pull."""
    root = tmp_path / "shop"
    assert main(["--root", str(root), "init", "app", "--no-input"]) == 0
    assert rules(root) == ["POL015:artifacts.app"]
    loaded = [load(root, n, environ={}, release="v1") for n in ("dev", "prd")]
    assert [f.rule for f in judge(loaded, [], False, TODAY).passing] == ["POL015"]
    verdict = judge(loaded, [], True, TODAY)
    assert [f.rule for f in verdict.failing] == ["POL015"]
    assert "variables.registry" in verdict.failing[0].message
    assert ".github/workflows/imagem.yml" in verdict.failing[0].message
    tagged = GitInfo(sha="a" * 40, tag="v1", branch=None, dirty=False)
    prd = load(root, "prd", environ={}, release="v1", git_reader=lambda _: tagged)
    with pytest.raises(ProductionCheckError, match="registro de exemplo"):
        production_checks(prd, approved=True)
    real = load(
        root,
        "prd",
        cli_vars={"registry": "ghcr.io/acme"},
        environ={},
        release="v1",
        git_reader=lambda _: tagged,
    )
    production_checks(real, approved=True)


def test_half_applied_recipe_and_job_code_that_does_not_import(tmp_path: Path) -> None:
    """A receita cloudflare-r2 num repo novo passava no validate estrito sem as variáveis
    que o código dela lê, sem os pacotes que ele importa e com o módulo do job em src/ de
    um projeto que não é instalado; a dagster roda o código na imagem dela e não conta. A
    custom-app não cobra CUSTOM_APP_IMAGE quando um artifact o produz; a pasta dagster/ da
    receita não é o pacote dagster, mas src/shop sem __init__.py é do projeto; um arquivo de
    resources/ escrito à mão não é da receita de mesmo nome."""
    root = tmp_path / "shop"
    argv = ["--root", str(root), "init", "--no-input", "--recipe", "cloudflare-r2"]
    recipes = ["--recipe", "dagster", "--recipe", "fastapi", "--recipe", "custom-app"]
    assert main([*argv, *recipes]) == 0
    pyproject = root / "pyproject.toml"
    project = '[project]\nname = "shop"\nversion = "0"\n'
    pyproject.write_text(project, encoding="utf-8")
    job = "resources/cloudflare-r2-bootstrap.job.yaml"
    dagster = "resources/dagster.service.yaml"
    fastapi = "resources/fastapi.service.yaml"
    custom = "resources/custom-app.service.yaml"
    assert rules(root) == [
        "POL015:artifacts.app",
        f"POL016:{job}",
        f"POL016:{custom}",
        f"POL016:{dagster}",
        f"POL016:{fastapi}",
        f"POL018:{job}",
        f"POL019:{job}",
    ]
    loaded = [load(root, "dev", environ={}, release="r1")]
    found = {f.rule + f.where: f.message for f in check_targets(loaded)}
    assert 'uv add "boto3>=1.34" "botocore"' in found[f"POL019{job}"]
    assert "lê FASTAPI_PORT, FASTAPI_DOCS, GOLD_PATH, que" in found[f"POL016{fastapi}"]

    data = yaml.safe_load((root / "forge.yaml").read_text(encoding="utf-8"))
    data["targets"]["dev"].setdefault("env", {})["PYTHONPATH"] = "src"
    (root / "forge.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    assert rules(root, "dev") == [
        f"POL016:{job}",
        f"POL016:{custom}",
        f"POL016:{dagster}",
        f"POL016:{fastapi}",
        f"POL019:{job}",
    ]

    data["variables"]["registry"]["default"] = "ghcr.io/acme"
    data["env"] |= {
        "R2_ACCOUNT_ID": "conta",
        "R2_BUCKET": "shop-dev",
        "AWS_ACCESS_KEY_ID": "secret://R2_KEY",
        "AWS_SECRET_ACCESS_KEY": "secret://R2_SECRET",
        "DAGSTER_PG_PASSWORD": "secret://DAGSTER_PG",
        "DAGSTER_SCHEDULES_ACTIVE": "false",
        "FASTAPI_PORT": "8000",
        "FASTAPI_DOCS": "false",
        "GOLD_PATH": "gold.duckdb",
        "CUSTOM_APP_COMMAND": "serve",
        "CUSTOM_APP_PORT": "8001",
        "CUSTOM_APP_INTERNAL_PORT": "8000",
        "CUSTOM_APP_HEALTH_PATH": "/health",
    }
    data["artifacts"]["custom"] = {"env": "CUSTOM_APP_IMAGE", "pull": "reg/c:1"}
    (root / "forge.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    pyproject.write_text(
        project + 'dependencies = ["boto3>=1.34", "BotoCore[crt]"]\n'
        '[build-system]\nrequires = ["hatchling"]\n',
        encoding="utf-8",
    )
    assert rules(root) == []

    hand_made = {
        "compose/manual.yaml": "services:\n  manual: {image: x}\n",
        "resources/n8n.service.yaml": "compose: [compose/manual.yaml]\n",
        "resources/versions.job.yaml": yaml.safe_dump(
            {
                "trigger": {"deploy": {"when": "after_up"}},
                "tasks": [
                    {"key": "v", "run": ["python", "-m", "dagster", "--version"]}
                ],
            }
        ),
    }
    for relative, text in hand_made.items():
        (root / relative).write_text(text, encoding="utf-8")
    assert rules(root) == []

    (root / "src/shop/storage/r2_landing.py").unlink()
    (root / "src/shop/__init__.py").unlink()
    flow = {
        "trigger": {"manual": True},
        "flow": "src/shop/flows.py:ingest",
        "work_pool": "p",
    }
    (root / "resources/ingest.job.yaml").write_text(
        yaml.safe_dump(flow), encoding="utf-8"
    )
    assert rules(root) == [f"POL017:{job}", "POL017:resources/ingest.job.yaml"]
