"""Regras do `forge validate`: cada achado tem id, nível, onde e porquê; o débito pode excusar."""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml

from forge.debt import DEBT_PATH, DebtEntry
from forge.errors import ForgeError, read_utf8
from forge.load import Loaded, secret_name
from forge.model import DotenvSecrets, Job, Task
from forge.scaffold import (
    archetype_names,
    archetype_spec,
    load_recipe,
    recipe_names,
)

Level = Literal["error", "warning"]
SECRETISH_NAME = re.compile(
    r"PASS|SECRET|TOKEN|API_?KEY|PRIVATE|CREDENTIAL", re.IGNORECASE
)
SECRETISH_VALUE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY|\bgh[pousr]_[A-Za-z0-9]{20,}|\bAKIA[0-9A-Z]{16}\b"
    r"|\bsk-[A-Za-z0-9_-]{20,}|\bxox[abpr]-[A-Za-z0-9-]{10,}|://[^/\s:@]+:[^/\s@]+@"
)
REFERENCE_ONLY = re.compile(r"^\$\{[^}]*\}$")
INCLUDED_VARIABLE = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::?-(?P<default>[^}]*))?\}"
)
DEFAULTED_VARIABLE = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*):?-")
REGISTRY_QUESTION = "registry"
IMAGE_WORKFLOW = ".github/workflows/imagem.yml"
FORGE_PROVIDED = "FORGE_"
PYTHON_PROGRAMS = ("python", "python3")
SOURCE_ROOT = "src"
CODE_ROOTS = ("", SOURCE_ROOT)
REQUIREMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
RECIPE_HEADER = re.compile(r"# Receita (?P<name>[a-z0-9][a-z0-9-]*)")


@dataclass(frozen=True)
class Finding:
    rule: str
    level: Level
    where: str
    message: str


def check_targets(loaded: Sequence[Loaded]) -> list[Finding]:
    found: list[Finding] = []
    for item in loaded:
        found += _production_rules(item)
        found += _secret_literals(item)
        found += _envelope_outside_vcs(item)
        found += _compose_services(item)
        found += _repeated_compose_names(item)
        found += _unused_artifacts(item)
        found += _routes(item)
        found += _backup_jobs(item)
        found += _task_services(item)
        found += example_registry(item)
        found += _recipe_findings(item)
        found += _job_code(item)
    found += _shared_state(loaded)
    return list(dict.fromkeys(found))


def check_debt(entries: Iterable[DebtEntry], today: date) -> list[Finding]:
    found: list[Finding] = []
    for entry in entries:
        where = f"{DEBT_PATH.as_posix()}#{entry.rule}"
        if entry.missing():
            found.append(
                Finding(
                    "DEB001",
                    "error",
                    where,
                    f"exceção sem {', '.join(entry.missing())}: não vale",
                )
            )
        elif entry.overdue(today):
            found.append(
                Finding(
                    "DEB002",
                    "warning",
                    where,
                    f"gatilho vencido ({entry.trigger}): passa na frente de feature",
                )
            )
    return found


def _production_rules(item: Loaded) -> list[Finding]:
    if item.mode != "production":
        return []
    where = f"targets.{item.target_name}"
    found: list[Finding] = []
    if item.target.git is None:
        found.append(
            Finding("POL001", "error", where, "alvo production sem git{branch|tag}")
        )
    for name, artifact in item.manifest.artifacts.items():
        if artifact.pull is None:
            found.append(
                Finding(
                    "POL003",
                    "error",
                    f"artifacts.{name}",
                    f"alvo {item.target_name} é production e o artifact só tem build",
                )
            )
    return found


def _secret_literals(item: Loaded) -> list[Finding]:
    values = {f"env.{k}": v for k, v in item.env.items()}
    values |= {f"variables.{k}": v for k, v in item.variables.items()}
    return [
        Finding(
            "POL002",
            "error",
            f"targets.{item.target_name}.{key}",
            "literal com cara de segredo; use secret://NOME",
        )
        for key, value in sorted(values.items())
        if _looks_secret(key, value)
    ]


def _looks_secret(key: str, value: str) -> bool:
    if not value or secret_name(value) is not None or REFERENCE_ONLY.match(value):
        return False
    return bool(SECRETISH_VALUE.search(value)) or bool(
        SECRETISH_NAME.search(key.split(".")[-1])
    )


def _envelope_outside_vcs(item: Loaded) -> list[Finding]:
    paths = [item.env_file]
    if isinstance(item.target.secrets, DotenvSecrets):
        paths.append(
            (item.root / Path(item.target.secrets.path).expanduser()).resolve()
        )
    return [
        Finding(
            "POL004",
            "error",
            f"targets.{item.target_name}",
            f"{path.relative_to(item.root).as_posix()} não está no .gitignore",
        )
        for path in paths
        if path.is_relative_to(item.root) and not ignored(item.root, path)
    ]


def ignored(root: Path, path: Path) -> bool:
    relative = path.relative_to(root).as_posix()
    try:
        done = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", relative],
            cwd=root,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return _gitignore_matches(root, relative)
    if done.returncode in (0, 1):
        return done.returncode == 0
    return _gitignore_matches(root, relative)


def _gitignore_matches(root: Path, relative: str) -> bool:
    ignore = root / ".gitignore"
    if not ignore.is_file():
        return False
    parts = relative.split("/")
    prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    for pattern in read_utf8(ignore).splitlines():
        clean = pattern.strip().strip("/")
        if (
            clean
            and not clean.startswith(("#", "!"))
            and any(
                fnmatch.fnmatch(p, clean)
                or fnmatch.fnmatch(p.rsplit("/", 1)[-1], clean)
                for p in prefixes
            )
        ):
            return True
    return False


def _compose_services(item: Loaded) -> list[Finding]:
    found: list[Finding] = []
    for name, service in item.services.items():
        where = item.sources[("service", name)]
        declared, missing = compose_service_names(
            item.root, service.compose, paths_env(item)
        )
        found += [
            Finding("POL007", "error", where, f"arquivo compose {m} não existe")
            for m in missing
        ]
        wanted = [
            *service.release_services,
            *(service.health.healthy or [] if service.health else []),
        ]
        found += [
            Finding(
                "POL006", "error", where, f"serviço {s} não existe nos arquivos compose"
            )
            for s in wanted
            if not missing and s not in declared
        ]
    return found


def _repeated_compose_names(item: Loaded) -> list[Finding]:
    """Dois resources no mesmo project com o mesmo serviço compose: um derruba o outro."""
    owners: dict[tuple[str, str], str] = {}
    found: list[Finding] = []
    for name, service in item.services.items():
        project = service.project or item.project
        for svc in sorted(
            compose_service_names(item.root, service.compose, paths_env(item))[0]
        ):
            other = owners.setdefault((project, svc), name)
            if other != name:
                found.append(
                    Finding(
                        "POL008",
                        "error",
                        item.sources[("service", name)],
                        f"serviço compose {svc} também está em "
                        f"{item.sources[('service', other)]}, "
                        f"no mesmo project: um derruba o outro",
                    )
                )
    return found


def _unused_artifacts(item: Loaded) -> list[Finding]:
    files = sorted({f for service in item.services.values() for f in service.compose})
    paths = compose_files(item.root, files, paths_env(item))[0]
    text = "\n".join(read_utf8(path) for path in paths)
    return [
        Finding(
            "POL009",
            "warning",
            f"artifacts.{name}",
            f"env {artifact.env} não aparece em nenhum arquivo compose: imagem sem uso",
        )
        for name, artifact in item.manifest.artifacts.items()
        if not re.search(rf"\$\{{?{artifact.env}\b", text)
    ]


def _all_compose_names(item: Loaded) -> set[str]:
    return {
        svc
        for service in item.services.values()
        for svc in compose_service_names(item.root, service.compose, paths_env(item))[0]
    }


def _routes(item: Loaded) -> list[Finding]:
    """Route para serviço que não existe, alvo sem proxy e nginx sem tls."""
    known, proxy = _all_compose_names(item), item.target.proxy
    found: list[Finding] = []
    for name, route in item.routes.items():
        where = item.sources[("route", name)]
        if route.service not in known:
            found.append(
                Finding(
                    "POL010",
                    "error",
                    where,
                    f"route para o serviço compose {route.service}, que não existe",
                )
            )
        if proxy is None:
            found.append(
                Finding(
                    "POL012",
                    "error",
                    where,
                    f"alvo {item.target_name} não declara proxy para a route",
                )
            )
        elif proxy.kind == "nginx" and route.tls is None:
            found.append(
                Finding(
                    "POL013",
                    "error",
                    where,
                    f"alvo {item.target_name} usa nginx: a route exige tls",
                )
            )
    return found


def _backup_jobs(item: Loaded) -> list[Finding]:
    return [
        Finding(
            "POL011",
            "error",
            item.sources[("storage", name)],
            f"backup.job {storage.backup.job} não existe entre os jobs do bundle",
        )
        for name, storage in item.storages.items()
        if storage.backup is not None and storage.backup.job not in item.jobs
    ]


def _task_services(item: Loaded) -> list[Finding]:
    known = _all_compose_names(item)
    return [
        Finding(
            "POL014",
            "error",
            item.sources[("job", name)],
            f"task {task.key} roda no serviço compose {task.service}, que não existe",
        )
        for name, job in item.jobs.items()
        for task in job.tasks
        if task.service is not None and task.service not in known
    ]


def example_registries() -> set[str]:
    """O registro que os moldes oferecem como padrão de exemplo: nenhum prd puxa de lá."""
    return {
        question.default
        for name in archetype_names()
        for key, question in archetype_spec(name).questions.items()
        if key == REGISTRY_QUESTION and question.default
    }


def example_registry(item: Loaded) -> list[Finding]:
    if item.mode != "production":
        return []
    examples = example_registries()
    return [
        Finding(
            "POL015",
            "warning",
            f"artifacts.{name}",
            f"alvo {item.target_name} puxa {artifact.pull}, do registro de exemplo "
            f"{example}, de onde nenhuma imagem vem: {_registry_fix(item)}",
        )
        for name, artifact in item.manifest.artifacts.items()
        for example in sorted(examples)
        if artifact.pull is not None and artifact.pull.startswith(f"{example}/")
    ]


def _registry_fix(item: Loaded) -> str:
    fix = "troque variables.registry no forge.yaml pelo registro real"
    if (item.root / IMAGE_WORKFLOW).is_file():
        fix += (
            f"; em {IMAGE_WORKFLOW}, que publica a imagem, troque no env REGISTRO (o host "
            f"do registro) e IMAGEM (o caminho da imagem depois do host), e fora do "
            f"ghcr.io o login do docker/login-action passa a usar os segredos "
            f"REGISTRY_USER e REGISTRY_TOKEN do repositório no lugar de github.actor e "
            f"GITHUB_TOKEN"
        )
    return fix


def generated_by(text: str) -> str | None:
    """A receita que gerou o arquivo, pela primeira linha que ela grava: `# Receita <nome>`."""
    found = RECIPE_HEADER.match(text.lstrip("\ufeff").lstrip())
    if found is None or found.group("name") not in recipe_names():
        return None
    return found.group("name")


def _recipes_in_bundle(item: Loaded) -> dict[str, str]:
    """Receita presente no bundle -> o primeiro arquivo de resources/ que ela gerou."""
    present: dict[str, str] = {}
    for label in sorted(item.sources.values()):
        recipe = generated_by(read_utf8(item.root / label))
        if recipe is not None:
            present.setdefault(recipe, label)
    return present


def _recipe_findings(item: Loaded) -> list[Finding]:
    present = _recipes_in_bundle(item)
    if not present:
        return []
    optional = _defaulted_names(item)
    declared = _declared_packages(item.root)
    found: list[Finding] = []
    for recipe, label in present.items():
        found += _recipe_env(item, label, recipe, optional)
        found += _recipe_requires(label, recipe, declared)
    return found


def _recipe_env(
    item: Loaded, label: str, recipe: str, optional: set[str]
) -> list[Finding]:
    produced = {artifact.env for artifact in item.manifest.artifacts.values()}
    missing = [
        name
        for name in load_recipe(recipe).env
        if name not in item.env
        and name not in optional
        and name not in produced
        and not name.startswith(FORGE_PROVIDED)
    ]
    return [_recipe_env_finding(label, recipe, missing)] if missing else []


def _recipe_requires(label: str, recipe: str, declared: set[str]) -> list[Finding]:
    spec = load_recipe(recipe)
    if spec.runs_in_image:
        return []
    missing = [
        requirement
        for requirement in spec.requires
        if _package_name(requirement) not in declared
    ]
    return [_recipe_requires_finding(label, recipe, missing)] if missing else []


def _package_name(requirement: str) -> str:
    """Nome normalizado pelo PEP 503, sem extras, versão nem marcador."""
    found = REQUIREMENT_NAME.match(requirement.strip())
    name = found.group(0) if found else requirement.strip()
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared_packages(root: Path) -> set[str]:
    project = _pyproject(root).get("project", {})
    dependencies = project.get("dependencies", []) if isinstance(project, dict) else []
    return {_package_name(str(entry)) for entry in dependencies}


def _recipe_requires_finding(label: str, recipe: str, missing: list[str]) -> Finding:
    command = " ".join(f'"{requirement}"' for requirement in missing)
    return Finding(
        "POL019",
        "warning",
        label,
        f"a receita {recipe} gerou este arquivo e o código dela importa "
        f"{', '.join(missing)}, que não está em [project].dependencies do "
        f"pyproject.toml: o código roda no .venv do repositório e quebra com "
        f"ModuleNotFoundError; rode `uv add {command}`",
    )


def _recipe_env_finding(label: str, recipe: str, missing: list[str]) -> Finding:
    return Finding(
        "POL016",
        "warning",
        label,
        f"a receita {recipe} gerou este arquivo e o código dela lê {', '.join(missing)}, "
        f"que não estão no env do forge.yaml nem do alvo: declare em env "
        f"(segredo como secret://NOME); a lista está em `forge recipes`",
    )


def _defaulted_names(item: Loaded) -> set[str]:
    """Variável com padrão no compose (`${NOME:-x}`) não precisa estar no env."""
    files = sorted({f for service in item.services.values() for f in service.compose})
    paths = compose_files(item.root, files, paths_env(item))[0]
    return {
        match.group("name")
        for path in paths
        for match in DEFAULTED_VARIABLE.finditer(read_utf8(path))
    }


def _job_code(item: Loaded) -> list[Finding]:
    installable = _installable(item.root) or _source_on_pythonpath(item)
    found: list[Finding] = []
    for name, job in item.jobs.items():
        where = item.sources[("job", name)]
        found += _flow_code(item.root, where, job)
        found += [
            finding
            for task in job.tasks
            if task.service is None
            for finding in _task_code(item.root, where, task, installable)
        ]
    return found


def _flow_code(root: Path, where: str, job: Job) -> list[Finding]:
    if job.flow is None:
        return []
    path = job.flow.rsplit(":", 1)[0]
    if (root / path).is_file():
        return []
    return [_missing_code(where, f"o flow {job.flow}", path)]


def _task_code(root: Path, where: str, task: Task, installable: bool) -> list[Finding]:
    argv = task.run
    if len(argv) < 2 or Path(argv[0]).stem.lower() not in PYTHON_PROGRAMS:
        return []
    if argv[1].endswith(".py"):
        exists = (root / argv[1]).is_file()
        return [] if exists else [_missing_code(where, f"a task {task.key}", argv[1])]
    if argv[1] == "-m" and len(argv) > 2:
        return _module_code(root, where, task.key, argv[2], installable)
    return []


def _module_code(
    root: Path, where: str, key: str, module: str, installable: bool
) -> list[Finding]:
    parts = module.split(".")
    owners = [base for base in CODE_ROOTS if _owns(root, base, parts[0])]
    if not owners:
        return []
    homes = [base for base in owners if _module_file(root / base, parts)]
    if not homes:
        relative = "/".join(filter(None, (owners[0], *parts))) + ".py"
        return [_missing_code(where, f"a task {key} (python -m {module})", relative)]
    if "" in homes or installable:
        return []
    return [_not_installed(where, key, module)]


def _owns(root: Path, base: str, package: str) -> bool:
    """Na raiz, pasta sem __init__.py (a dagster/ de uma receita) não é pacote: o instalado
    de mesmo nome vence. Em src/, toda pasta é do projeto, namespace ou não."""
    home = root / base
    if base == SOURCE_ROOT and (home / package).is_dir():
        return True
    return (home / package / "__init__.py").is_file() or (
        home / f"{package}.py"
    ).is_file()


def _module_file(base: Path, parts: list[str]) -> bool:
    path = base.joinpath(*parts)
    return path.with_suffix(".py").is_file() or (path / "__main__.py").is_file()


def _installable(root: Path) -> bool:
    """`uv sync` só instala o projeto (e o src/ dele) com [build-system] ou tool.uv.package."""
    data = _pyproject(root)
    uv = data.get("tool", {}).get("uv", {})
    return "build-system" in data or uv.get("package") is True


def _source_on_pythonpath(item: Loaded) -> bool:
    """`PYTHONPATH: src` no env do alvo deixa o módulo de src/ importável sem instalar."""
    wanted = (item.root / SOURCE_ROOT).resolve()
    entries = item.env.get("PYTHONPATH", "").split(os.pathsep)
    return any(entry and (item.root / entry).resolve() == wanted for entry in entries)


def _pyproject(root: Path) -> dict[str, Any]:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return {}
    try:
        return tomllib.loads(read_utf8(pyproject))
    except tomllib.TOMLDecodeError as error:
        raise ForgeError(
            f"pyproject.toml: TOML inválido ({error}); corrija o arquivo e rode de novo"
        ) from error


def _missing_code(where: str, what: str, path: str) -> Finding:
    return Finding(
        "POL017",
        "error",
        where,
        f"{what} aponta para {path}, que não existe no repositório: "
        f"crie o arquivo ou corrija o caminho no job",
    )


def _not_installed(where: str, key: str, module: str) -> Finding:
    return Finding(
        "POL018",
        "warning",
        where,
        f"a task {key} roda python -m {module}, que está em src/, mas o pyproject.toml "
        f"não declara [build-system]: o projeto não é instalado no .venv e o módulo "
        f"não importa a partir da raiz; declare [build-system] (e o pacote em src/) "
        f"no pyproject.toml e rode `uv sync`, ou declare PYTHONPATH: src no env do alvo "
        f"no forge.yaml",
    )


def loaded_compose(path: Path) -> dict[str, object]:
    data = yaml.safe_load(read_utf8(path)) or {}
    return data if isinstance(data, dict) else {}


def interpolated(text: str, env: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        found = env.get(match.group("name"), "")
        default = match.group("default")
        return found or (default if default is not None else match.group(0))

    return INCLUDED_VARIABLE.sub(replace, text)


def _included(path: Path, env: Mapping[str, str]) -> list[Path]:
    found: list[Path] = []
    for entry in loaded_compose(path).get("include") or []:
        named = entry.get("path") if isinstance(entry, dict) else entry
        for relative in named if isinstance(named, list) else [named]:
            found.append(path.parent / interpolated(str(relative), env))
    return found


def compose_files(
    root: Path, files: Sequence[str], env: Mapping[str, str] | None = None
) -> tuple[list[Path], list[str]]:
    """Os arquivos compose e os que eles incluem, como o compose os lê: o caminho do
    `include:` é relativo ao arquivo que inclui, com `${NOME:-padrão}` resolvido pelo env."""
    found: list[Path] = []
    missing: list[str] = []
    pending = [root / relative for relative in files]
    while pending:
        path = pending.pop(0)
        if path in found:
            continue
        if not path.is_file():
            missing.append(Path(os.path.relpath(path, root)).as_posix())
            continue
        found.append(path)
        pending += _included(path, env or {})
    return found, missing


def compose_service_names(
    root: Path, files: Sequence[str], env: Mapping[str, str] | None = None
) -> tuple[set[str], list[str]]:
    paths, missing = compose_files(root, files, env)
    names = {
        name for path in paths for name in loaded_compose(path).get("services") or {}
    }
    return names, missing


def paths_env(item: Loaded) -> dict[str, str]:
    return {**item.env, "FORGE_TARGET": item.target_name}


def _shared_state(loaded: Sequence[Loaded]) -> list[Finding]:
    seen: dict[tuple[str, str, str], str] = {}
    found: list[Finding] = []
    for item in loaded:
        key = (
            item.env.get("DOCKER_HOST", "local"),
            item.project,
            item.state_path.as_posix(),
        )
        if key in seen:
            found.append(
                Finding(
                    "POL005",
                    "error",
                    f"targets.{item.target_name}",
                    f"mesmo host, project e state_path do alvo {seen[key]}",
                )
            )
        seen.setdefault(key, item.target_name)
    return found


def excused(finding: Finding, debt: Sequence[DebtEntry]) -> DebtEntry | None:
    return next(
        (entry for entry in debt if entry.excuses(finding.rule, finding.where)), None
    )
