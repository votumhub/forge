"""Lê forge.yaml e os recursos incluídos, escolhe o alvo, resolve variáveis e interpola."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from forge.errors import read_utf8
from forge.gitinfo import FORGE_DIR, GitInfo, read_git
from forge.model import (
    Job,
    Manifest,
    ManifestError,
    Route,
    Service,
    Storage,
    Target,
    parse,
)

MANIFEST_FILE = "forge.yaml"
VAR_PREFIX = "FORGE_VAR_"
SECRET_PREFIX = "secret://"
NO_GIT_RELEASE = "unversioned"
RESOURCE_FILE = re.compile(
    r"^(?P<name>[a-z][a-z0-9_-]*)\.(?P<kind>service|job|storage|route)\.yaml$"
)
REFERENCE = re.compile(r"\$(?P<escape>\$?)\{(?P<body>[^}]*)\}")

GitReader = Callable[[Path], GitInfo | None]


@dataclass(frozen=True)
class Loaded:
    root: Path
    manifest: Manifest
    target_name: str
    target: Target
    release: str
    git: GitInfo | None
    variables: dict[str, str]
    env: dict[str, str]
    services: dict[str, Service]
    jobs: dict[str, Job]
    sources: dict[tuple[str, str], str]
    """(tipo, nome) -> arquivo; o nome é único dentro de cada tipo."""
    storages: dict[str, Storage] = field(default_factory=dict)
    routes: dict[str, Route] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.manifest.bundle.name

    @property
    def mode(self) -> str:
        return self.target.mode

    @property
    def project(self) -> str:
        return self.target.project or f"{self.name}-{self.target_name}"

    @property
    def state_path(self) -> Path:
        declared = self.target.state_path
        return self.root / (declared if declared else f"{FORGE_DIR}/state")

    @property
    def env_file(self) -> Path:
        return self.root / FORGE_DIR / "env" / f"{self.target_name}.env"

    @property
    def flow_jobs(self) -> dict[str, Job]:
        """Os jobs que vão ao Prefect (gatilho schedule, event ou manual)."""
        return {name: job for name, job in self.jobs.items() if job.on_deploy is None}

    @property
    def build_dir(self) -> Path:
        return self.root / FORGE_DIR / "build" / self.target_name


def load(
    root: Path,
    target: str | None = None,
    *,
    cli_vars: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
    release: str | None = None,
    git_reader: GitReader = read_git,
) -> Loaded:
    root = root.resolve()
    raw = _manifest_data(root)
    shape = parse(Manifest, raw, MANIFEST_FILE)
    chosen = pick_target(shape, target)
    git = git_reader(root)
    context = _Context(
        root=root,
        bundle_name=shape.bundle.name,
        target=chosen,
        release=release or _release(git, shape.targets[chosen].mode),
        git=git,
        raw_vars=_variable_values(
            shape, chosen, cli_vars or {}, os.environ if environ is None else environ
        ),
    )
    variables = context.variables()
    manifest = parse(Manifest, context.walk(raw, ""), MANIFEST_FILE)
    found = _resources(root, manifest.include, context, sorted(manifest.targets))
    picked = manifest.targets[chosen]
    return Loaded(
        root=root,
        manifest=manifest,
        target_name=chosen,
        target=picked,
        release=context.release,
        git=git,
        variables=variables,
        env=overlay(manifest.env, picked.env),
        services=found.services,
        jobs=found.jobs,
        sources=found.sources,
        storages=found.storages,
        routes=found.routes,
    )


def read_manifest(root: Path) -> Manifest:
    """O manifesto como está no arquivo, sem alvo escolhido e sem interpolar."""
    return parse(Manifest, _manifest_data(root), MANIFEST_FILE)


def _manifest_data(root: Path) -> object:
    path = root / MANIFEST_FILE
    if not path.is_file():
        raise ManifestError(
            f"não há {MANIFEST_FILE} em {root}: rode `forge init` nesta pasta para criar "
            f"um, ou aponte a pasta do bundle com `forge --root <pasta> <verbo>`"
        )
    return _read_yaml(path, MANIFEST_FILE)


def pick_target(manifest: Manifest, wanted: str | None) -> str:
    if wanted is not None:
        if wanted not in manifest.targets:
            known = ", ".join(sorted(manifest.targets))
            raise ManifestError(
                f"{MANIFEST_FILE}: targets.{wanted} não existe (alvos: {known})"
            )
        return wanted
    defaults = [name for name, target in manifest.targets.items() if target.default]
    if len(defaults) > 1:
        raise ManifestError(
            f"{MANIFEST_FILE}: mais de um alvo com default: true ({', '.join(defaults)})"
        )
    if defaults:
        return defaults[0]
    if len(manifest.targets) == 1:
        return next(iter(manifest.targets))
    raise ManifestError(
        f"{MANIFEST_FILE}: nenhum alvo marcado default; passe -t <alvo>"
    )


def overlay[V](base: Mapping[str, V], top: Mapping[str, V]) -> dict[str, V]:
    """Mapas mesclam recursivamente; escalares e listas do topo substituem."""
    merged: dict[str, V] = dict(base)
    for key, value in top.items():
        below = merged.get(key)
        if isinstance(below, Mapping) and isinstance(value, Mapping):
            merged[key] = overlay(below, value)  # type: ignore[assignment]
        else:
            merged[key] = value
    return merged


def _read_yaml(path: Path, label: str) -> object:
    if not path.is_file():
        raise ManifestError(f"{label}: arquivo não encontrado em {path.parent}")
    try:
        return yaml.safe_load(read_utf8(path))
    except yaml.YAMLError as error:
        raise ManifestError(f"{label}: YAML inválido: {error}") from error


def _release(git: GitInfo | None, mode: str) -> str:
    if git is None:
        return NO_GIT_RELEASE
    if git.tag:
        return git.tag
    suffix = "-dirty" if git.dirty and mode == "development" else ""
    return git.short + suffix


def _variable_values(
    manifest: Manifest,
    target: str,
    cli_vars: Mapping[str, str],
    environ: Mapping[str, str],
) -> dict[str, str]:
    declared = manifest.variables
    from_env = {
        k[len(VAR_PREFIX) :]: v for k, v in environ.items() if k.startswith(VAR_PREFIX)
    }
    from_target = manifest.targets[target].variables
    _refuse_undeclared(
        declared, from_target, f"{MANIFEST_FILE}: targets.{target}.variables."
    )
    _refuse_undeclared(declared, from_env, f"ambiente: {VAR_PREFIX}")
    _refuse_undeclared(declared, cli_vars, "linha de comando: --var ")
    values: dict[str, str] = {}
    for name, variable in declared.items():
        for layer in (cli_vars, from_env, from_target):
            if name in layer:
                values[name] = layer[name]
                break
        else:
            if variable.default is None:
                raise ManifestError(
                    f"{MANIFEST_FILE}: variables.{name} sem valor para o alvo {target} "
                    f"(dê default, targets.{target}.variables.{name}, {VAR_PREFIX}{name} "
                    f"ou --var {name}=...)"
                )
            values[name] = variable.default
    return values


def _refuse_undeclared(
    declared: Mapping[str, object], given: Mapping[str, str], where: str
) -> None:
    for name in given:
        if name not in declared:
            raise ManifestError(
                f"{where}{name}: variável não declarada em {MANIFEST_FILE} (variables.{name})"
            )


@dataclass
class _Context:
    root: Path
    bundle_name: str
    target: str
    release: str
    git: GitInfo | None
    raw_vars: dict[str, str]

    def __post_init__(self) -> None:
        self._resolved: dict[str, str] = {}

    def variables(self) -> dict[str, str]:
        return {
            name: self._var(name, (), f"variables.{name}") for name in self.raw_vars
        }

    def walk(self, node: object, path: str) -> object:
        if isinstance(node, str):
            return self.expand(node, path, ())
        if isinstance(node, dict):
            return {
                k: self.walk(v, f"{path}.{k}" if path else str(k))
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [self.walk(item, f"{path}.{i}") for i, item in enumerate(node)]
        return node

    def expand(self, text: str, path: str, chain: tuple[str, ...]) -> str:
        def replace(match: re.Match[str]) -> str:
            body = match.group("body")
            if match.group("escape"):
                return "${" + body + "}"
            if "." not in body:
                return match.group(0)
            return self._lookup(body.strip(), path, chain)

        return REFERENCE.sub(replace, text)

    def _lookup(self, ref: str, path: str, chain: tuple[str, ...]) -> str:
        space, _, key = ref.partition(".")
        if space == "var":
            return self._var(key, chain, path)
        fixed = {
            "bundle.name": self.bundle_name,
            "bundle.target": self.target,
            "bundle.release": self.release,
            "bundle.root": self.root.as_posix(),
        }
        if ref in fixed:
            return fixed[ref]
        if ref == "git.sha":
            if self.git is None:
                raise ManifestError(
                    f"{path}: ${{git.sha}} usado fora de um repositório git"
                )
            return self.git.sha
        raise ManifestError(f"{path}: referência desconhecida ${{{ref}}}")

    def _var(self, name: str, chain: tuple[str, ...], path: str) -> str:
        if name in chain:
            cycle = " -> ".join(f"var.{n}" for n in (*chain, name))
            raise ManifestError(f"{MANIFEST_FILE}: ciclo de variáveis: {cycle}")
        if name not in self.raw_vars:
            raise ManifestError(f"{path}: ${{var.{name}}} não declarada em variables")
        if name not in self._resolved:
            where = f"variables.{name}"
            self._resolved[name] = self.expand(
                self.raw_vars[name], where, (*chain, name)
            )
        return self._resolved[name]


@dataclass
class _Found:
    target: str
    known_targets: list[str]
    services: dict[str, Service] = field(default_factory=dict)
    jobs: dict[str, Job] = field(default_factory=dict)
    storages: dict[str, Storage] = field(default_factory=dict)
    routes: dict[str, Route] = field(default_factory=dict)
    sources: dict[tuple[str, str], str] = field(default_factory=dict)

    def add(self, kind: str, name: str, data: object, label: str) -> None:
        if kind == "service":
            self.services[name] = parse(Service, data, label)
        elif kind == "job":
            self.jobs[name] = parse(Job, data, label)
        elif kind == "storage":
            self.storages[name] = parse(Storage, data, label)
        else:
            self.routes[name] = parse(Route, self._for_target(data, label), label)

    def _for_target(self, data: object, label: str) -> object:
        """Route: `targets.<alvo>` do arquivo sobrepõe o resto (mapas mesclam)."""
        if not isinstance(data, dict):
            return data
        base = {k: v for k, v in data.items() if k != "targets"}
        per_target = data.get("targets") or {}
        if not isinstance(per_target, dict):
            raise ManifestError(f"{label}: targets deve ser um mapa alvo -> campos")
        unknown = sorted(set(per_target) - set(self.known_targets))
        if unknown:
            raise ManifestError(
                f"{label}: targets.{unknown[0]} não é alvo do {MANIFEST_FILE}"
            )
        return overlay(base, per_target.get(self.target) or {})


def _resources(
    root: Path, patterns: list[str], context: _Context, targets: list[str]
) -> _Found:
    found = _Found(context.target, targets)
    for pattern in patterns:
        matched = sorted(root.glob(pattern))
        if not matched:
            raise ManifestError(
                f"{MANIFEST_FILE}: include {pattern!r} não casou com nenhum arquivo"
            )
        for path in matched:
            label = path.relative_to(root).as_posix()
            name, kind = _resource_name(label, path.name)
            known = found.sources.setdefault((kind, name), label)
            if known != label:
                raise ManifestError(f"{label}: {kind} {name} já declarado em {known}")
            found.add(kind, name, context.walk(_read_yaml(path, label), ""), label)
    return found


def _resource_name(label: str, filename: str) -> tuple[str, str]:
    found = RESOURCE_FILE.match(filename)
    if found is None:
        raise ManifestError(
            f"{label}: nome de recurso inválido; use resources/<nome>.<service|job|storage|route>.yaml"
        )
    return found.group("name"), found.group("kind")


def secret_name(value: str) -> str | None:
    """`secret://NOME` -> NOME; qualquer outro valor -> None."""
    if not value.startswith(SECRET_PREFIX):
        return None
    return value[len(SECRET_PREFIX) :].strip()
