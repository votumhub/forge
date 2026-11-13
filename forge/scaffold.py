"""Motor do init: perguntas, render de arquétipo e receitas, e nunca sobrescrever nada."""

from __future__ import annotations

import re
import subprocess
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined, TemplateError

from forge import VERSION
from forge.debt import DEBT_PATH, DEBT_TEMPLATE
from forge.errors import ForgeError, read_utf8
from forge.model import ArchetypeSpec, Recipe, parse

PACKAGE_DIR = Path(__file__).resolve().parent
ARCHETYPES = PACKAGE_DIR / "archetypes"
RECIPES = PACKAGE_DIR / "recipes"
BASE_FILES = ARCHETYPES / "_base" / "files"
PACKAGE_TOKEN = "__package__"
IGNORE_LINES = (".forge/", "__pycache__/", ".venv/", ".pytest_cache/")
"""O que o forge gera (.forge/), o que o worker gera ao importar o código do repo
(__pycache__/) e o que o uv e o pytest criam (.venv/, .pytest_cache/): fora do git, senão a
árvore fica suja e production recusa o deploy."""
BASE_QUESTIONS: dict[str, dict[str, str]] = {
    "name": {"prompt": "nome do bundle (minúsculas, - ou _)"},
    "python": {"prompt": "versão do Python", "default": "3.12"},
    "dev_branch": {"prompt": "branch que faz deploy em dev", "default": "develop"},
}
NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
FORGE_PACKAGE = "forge"
FORGE_REPOSITORY = "github.com/votumhub/forge"
FORGE_GROUP = "forge"
GITHUB_OWNER = re.compile(r"github\.com[:/]+([A-Za-z0-9-]+)/")

Ask = Callable[[str], str]


def forge_source() -> str:
    """De onde o projeto vizinho instala este forge: a tag e a versao da biblioteca."""
    return f"{FORGE_PACKAGE} @ git+https://{FORGE_REPOSITORY}@v{VERSION}"


def forge_group(vendored: bool) -> str:
    """Vendorizado, o forge mora em ./forge: o pyproject não leva o pin do pacote."""
    return "" if vendored else f'{FORGE_GROUP} = ["{forge_source()}"]'


class ScaffoldError(ForgeError):
    pass


@dataclass
class Outcome:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"  criado     {p}" for p in self.created]
        out += [f"  atualizado {p}" for p in self.updated]
        out += [
            f"  pulado     {p} (já existe; nada foi sobrescrito)" for p in self.skipped
        ]
        return out


def archetype_names() -> list[str]:
    return sorted(
        p.name for p in ARCHETYPES.iterdir() if (p / "archetype.yaml").is_file()
    )


def questions(archetype: str) -> dict[str, dict[str, str]]:
    own = {
        key: q.model_dump(exclude_none=True)
        for key, q in archetype_spec(archetype).questions.items()
    }
    return {**BASE_QUESTIONS, **own}


def archetype_spec(archetype: str) -> ArchetypeSpec:
    path = ARCHETYPES / archetype / "archetype.yaml"
    if not path.is_file():
        raise ScaffoldError(
            f"arquétipo {archetype} não existe ({', '.join(archetype_names())})"
        )
    label = f"forge/archetypes/{archetype}/archetype.yaml"
    return parse(ArchetypeSpec, yaml.safe_load(path.read_text(encoding="utf-8")), label)


def default_name(root: Path) -> str:
    """O [project].name do pyproject.toml quando existe; senão, a pasta."""
    for candidate in (_project_name(root), root.resolve().name):
        slug = re.sub(r"[^a-z0-9_-]+", "-", candidate.lower()).strip("-_")
        if NAME.match(slug):
            return slug
    return "app"


def _project_name(root: Path) -> str:
    path = root / "pyproject.toml"
    if not path.is_file():
        return ""
    try:
        project = tomllib.loads(read_utf8(path)).get("project", {})
    except tomllib.TOMLDecodeError as error:
        raise ScaffoldError(
            f"{path}: TOML inválido ({error}); o init lê dali o nome do projeto. "
            "Corrija o arquivo ou passe o nome com --config-file (name: <nome>)"
        ) from error
    name = project.get("name") if isinstance(project, dict) else None
    return name if isinstance(name, str) else ""


def default_registry(root: Path) -> str | None:
    """ghcr.io/<dono> quando o remoto origin está no GitHub; None sem remoto."""
    if not root.is_dir():
        return None
    try:
        done = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    found = GITHUB_OWNER.search(done.stdout) if done.returncode == 0 else None
    return f"ghcr.io/{found.group(1).lower()}" if found else None


def derived_defaults(
    root: Path, wanted: Mapping[str, object], given: Mapping[str, object]
) -> dict[str, str]:
    """Padrões lidos do repositório; só o que o config-file não respondeu."""
    derived: dict[str, str] = {}
    if "name" not in given:
        derived["name"] = default_name(root)
    registry = (
        default_registry(root) if "registry" in set(wanted) - set(given) else None
    )
    if registry is not None:
        derived["registry"] = registry
    return derived


def answers_for(
    archetype: str, root: Path, given: Mapping[str, object], ask: Ask | None
) -> dict[str, str]:
    """Config-file e padrões; `ask` (None em --no-input) pergunta o que o arquivo não trouxe."""
    wanted = questions(archetype)
    unknown = sorted(set(given) - set(wanted))
    if unknown:
        raise ScaffoldError(
            f"--config-file: chave(s) desconhecida(s): {', '.join(unknown)}"
        )
    derived = derived_defaults(root, wanted, given)
    answers: dict[str, str] = {}
    for key, spec in wanted.items():
        default = str(given.get(key, derived.get(key, spec.get("default", ""))))
        answers[key] = (
            _asked(ask, spec["prompt"], default) if key not in given else default
        )
    if not NAME.match(answers["name"]):
        raise ScaffoldError(
            f"nome {answers['name']!r} inválido: use minúsculas, dígitos, - e _"
        )
    answers["package"] = answers["name"].replace("-", "_")
    return answers


def _asked(ask: Ask | None, prompt: str, default: str) -> str:
    if ask is None:
        return default
    return ask(f"{prompt} [{default}]: ").strip() or default


def read_config(path: Path) -> dict[str, object]:
    try:
        data = yaml.safe_load(read_utf8(path)) or {}
    except (OSError, yaml.YAMLError) as error:
        raise ScaffoldError(f"--config-file {path}: ilegível ({error})") from error
    if not isinstance(data, dict):
        raise ScaffoldError(f"--config-file {path}: esperado um mapa chave: valor")
    return data


def renderer() -> Environment:
    """Delimitadores [[ ]] e [% %]: não brigam com ${{ }} do Actions nem com {{ }} do dbt."""
    return Environment(
        variable_start_string="[[",
        variable_end_string="]]",
        block_start_string="[%",
        block_end_string="%]",
        comment_start_string="[#",
        comment_end_string="#]",
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )


def render_tree(source: Path, answers: Mapping[str, object]) -> dict[str, str]:
    """{caminho relativo de destino: conteúdo} para cada arquivo de `source`."""
    engine = renderer()
    rendered: dict[str, str] = {}
    paths = sorted(
        p for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts
    )
    for path in paths:
        relative = (
            path.relative_to(source)
            .as_posix()
            .replace(PACKAGE_TOKEN, str(answers["package"]))
        )
        text = path.read_text(encoding="utf-8")
        if relative.endswith(".j2"):
            relative = relative[: -len(".j2")]
            try:
                text = engine.from_string(text).render(**answers)
            except TemplateError as error:
                raise ScaffoldError(f"{path}: template quebrado ({error})") from error
        rendered[relative] = text
    return rendered


def write_new(root: Path, files: Mapping[str, str], outcome: Outcome) -> None:
    for relative, text in files.items():
        target = root / relative
        if target.exists():
            outcome.skipped.append(relative)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        outcome.created.append(relative)


def ensure_ignored(root: Path, outcome: Outcome) -> None:
    path = root / ".gitignore"
    lines = read_utf8(path).splitlines() if path.is_file() else []
    present = {line.strip().strip("/") for line in lines}
    missing = [line for line in IGNORE_LINES if line.strip("/") not in present]
    if not missing:
        return
    existed = path.is_file()
    body = "\n".join([*lines, *missing]) + "\n"
    path.write_text(body, encoding="utf-8", newline="\n")
    (outcome.updated if existed else outcome.created).append(".gitignore")


def vendor(root: Path, outcome: Outcome) -> None:
    """Copia o próprio forge para <root>/forge, para rodar sem instalar nada além das deps."""
    files = {
        f"forge/{p.relative_to(PACKAGE_DIR).as_posix()}": p.read_text(encoding="utf-8")
        for p in sorted(PACKAGE_DIR.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }
    write_new(root, files, outcome)


def scaffold(
    root: Path,
    archetype: str | None,
    answers: Mapping[str, str],
    *,
    vendored: bool,
    recipes: list[str] | None = None,
) -> Outcome:
    """`archetype` None = só receitas num bundle que já existe."""
    outcome = Outcome()
    root.mkdir(parents=True, exist_ok=True)
    context = {**answers, "vendored": vendored, "forge_group": forge_group(vendored)}
    files: dict[str, str] = {}
    if archetype is not None:
        files |= render_tree(BASE_FILES, context)
        files[DEBT_PATH.as_posix()] = DEBT_TEMPLATE
        files |= render_tree(ARCHETYPES / archetype / "files", context)
    bundled = archetype_spec(archetype).recipes if archetype is not None else []
    for name in [*bundled, *(recipes or [])]:
        load_recipe(name)
        files |= render_tree(RECIPES / name / "files", context)
    write_new(root, files, outcome)
    ensure_ignored(root, outcome)
    if vendored:
        vendor(root, outcome)
    return outcome


def recipe_names() -> list[str]:
    return sorted(p.name for p in RECIPES.iterdir() if (p / "recipe.yaml").is_file())


def load_recipe(name: str) -> Recipe:
    path = RECIPES / name / "recipe.yaml"
    if not path.is_file():
        raise ScaffoldError(f"receita {name} não existe; veja `forge recipes`")
    label = f"forge/recipes/{name}/recipe.yaml"
    return parse(Recipe, yaml.safe_load(path.read_text(encoding="utf-8")), label)
