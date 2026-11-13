"""Contrato do manifesto `forge.yaml` (forge: 2) e dos recursos incluídos."""

from __future__ import annotations

import ipaddress
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    model_validator,
)

from forge.errors import ForgeError

NAME_PATTERN = r"^[a-z][a-z0-9_-]*$"
ENV_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
Name = Annotated[str, Field(pattern=NAME_PATTERN)]
EnvName = Annotated[str, Field(pattern=ENV_NAME_PATTERN)]
Argv = Annotated[list[str], Field(min_length=1)]


class ManifestError(ForgeError):
    pass


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Bundle(Strict):
    name: Name


class Variable(Strict):
    description: str = ""
    default: str | None = None


class EnvSecrets(Strict):
    provider: Literal["env"]


class DotenvSecrets(Strict):
    provider: Literal["dotenv"]
    path: str


class ExecSecrets(Strict):
    provider: Literal["exec"]
    command: Argv
    timeout: float = Field(default=30, gt=0)
    passthrough: list[EnvName] = []


SecretsSpec = Annotated[
    EnvSecrets | DotenvSecrets | ExecSecrets, Field(discriminator="provider")
]


class GitRule(Strict):
    branch: str | None = None
    tag: str | None = None

    @model_validator(mode="after")
    def _one_ref(self) -> Self:
        if (self.branch is None) == (self.tag is None):
            raise ValueError("declare exatamente um entre branch e tag")
        return self


class Proxy(Strict):
    """Proxy do alvo: o forge escreve um arquivo por route em config_dir e roda reload."""

    kind: Literal["caddy", "nginx"]
    config_dir: str
    reload: Argv


class Target(Strict):
    mode: Literal["development", "production"]
    default: bool = False
    project: Name | None = None
    state_path: str | None = None
    secrets: SecretsSpec | None = None
    variables: dict[str, str] = {}
    env: dict[EnvName, str] = {}
    git: GitRule | None = None
    proxy: Proxy | None = None


class Build(Strict):
    dockerfile: str = "Dockerfile"
    context: str = "."
    args: dict[str, str] = {}


class Artifact(Strict):
    env: EnvName
    build: Build | None = None
    pull: str | None = None

    @model_validator(mode="after")
    def _has_source(self) -> Self:
        if self.build is None and self.pull is None:
            raise ValueError("um artifact precisa de build, de pull ou dos dois")
        return self


class Policy(Strict):
    strict: bool = False


class Follow(Strict):
    """O que `forge seguir` mostra do log da rodada no Prefect."""

    logger: Annotated[str, Field(pattern=r"^[^\s]+$")]
    """Logger cujas mensagens aparecem (ele e os filhos); sem o bloco, tudo que não é prefect.*."""
    cabecalho: Annotated[str, Field(pattern=r"^[^\r\n]+$")] | None = None
    """Linha de títulos impressa uma vez, depois de `HORA`, alinhada com as mensagens."""


class Manifest(Strict):
    forge: Literal[2]
    bundle: Bundle
    include: list[str] = []
    variables: dict[str, Variable] = {}
    env: dict[EnvName, str] = {}
    targets: Annotated[dict[Name, Target], Field(min_length=1)]
    artifacts: dict[Name, Artifact] = {}
    policy: Policy = Policy()
    seguir: Follow | None = None


class Health(Strict):
    url: str | None = None
    healthy: list[str] | None = None
    command: Argv | None = None
    timeout: float = Field(default=60, gt=0)

    @model_validator(mode="after")
    def _one_probe(self) -> Self:
        given = [
            probe
            for probe in (self.url, self.healthy, self.command)
            if probe is not None
        ]
        if len(given) != 1:
            raise ValueError("declare exatamente um entre url, healthy e command")
        return self


class Service(Strict):
    compose: Annotated[list[str], Field(min_length=1)]
    project: Name | None = None
    release_services: list[str] = []
    health: Health | None = None


class Schedule(Strict):
    cron: str
    timezone: str = "UTC"


DeployMoment = Literal["before_deploy", "before_up", "after_up"]


class DeployTrigger(Strict):
    when: DeployMoment


class Trigger(Strict):
    """Exatamente uma chave: o que dispara o job."""

    schedule: Schedule | None = None
    event: dict[str, JsonValue] | None = None
    """Repassado como está aos `triggers` do deployment no prefect.yaml."""
    deploy: DeployTrigger | None = None
    manual: Literal[True] | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> Self:
        given = [
            k
            for k in ("schedule", "event", "deploy", "manual")
            if getattr(self, k) is not None
        ]
        if len(given) != 1:
            raise ValueError(
                "declare exatamente um entre schedule, event, deploy e manual"
            )
        return self


class Task(Strict):
    key: Name
    run: Argv
    service: str | None = None
    """Serviço compose em cujo contêiner a task roda (`docker compose run --rm`)."""
    depends_on: list[Name] = []
    timeout: float | None = Field(default=None, gt=0)


FLOW_PATTERN = r"^[^:]+\.py:[A-Za-z_][A-Za-z0-9_]*$"


class Job(Strict):
    trigger: Trigger
    flow: Annotated[str, Field(pattern=FLOW_PATTERN)] | None = None
    parameters: dict[str, JsonValue] = {}
    work_pool: str | None = None
    tasks: list[Task] = []

    @property
    def on_deploy(self) -> DeployMoment | None:
        """O momento do deploy em que o forge roda as tasks; None = job do Prefect."""
        return self.trigger.deploy.when if self.trigger.deploy else None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.on_deploy is None:
            return self._prefect_shape()
        if self.flow or self.work_pool or self.parameters or not self.tasks:
            raise ValueError(
                "gatilho deploy pede tasks e não aceita flow, work_pool nem parameters"
            )
        if self.on_deploy == "before_deploy" and any(t.service for t in self.tasks):
            raise ValueError(
                "task com service não roda em before_deploy: o envelope e as "
                "imagens ainda não existem"
            )
        task_order(self.tasks)
        return self

    def _prefect_shape(self) -> Self:
        if self.flow is None or self.work_pool is None or self.tasks:
            raise ValueError(
                "gatilho schedule, event ou manual pede flow e work_pool, sem tasks"
            )
        return self


def task_order(tasks: list[Task]) -> list[Task]:
    """Ordem topológica estável; dependência inexistente, repetida ou ciclo = erro."""
    by_key = {task.key: task for task in tasks}
    if len(by_key) != len(tasks):
        raise ValueError("tasks com key repetida")
    ordered: list[Task] = []
    for task in tasks:
        _visit(task, by_key, ordered, ())
    return ordered


def _visit(
    task: Task, by_key: dict[str, Task], done: list[Task], chain: tuple[str, ...]
) -> None:
    if task in done:
        return
    if task.key in chain:
        raise ValueError(f"ciclo entre tasks: {' -> '.join((*chain, task.key))}")
    for dep in task.depends_on:
        if dep not in by_key:
            raise ValueError(f"task {task.key} depende de {dep}, que não existe")
        _visit(by_key[dep], by_key, done, (*chain, task.key))
    done.append(task)


class Backup(Strict):
    job: Name
    """O job (normal) que faz o backup; o storage só aponta para ele."""
    restore: Argv


class Storage(Strict):
    kind: Literal["bucket", "volume", "database"]
    ensure: Argv | None = None
    """Comando do projeto que cria se não existir (idempotente); volume o forge cria sozinho."""
    name: str | None = None
    """Só volume: nome explícito no docker; sem ele, `<project>_<nome do recurso>`."""
    backup: Backup | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.kind == "volume" and self.ensure is not None:
            raise ValueError(
                "volume não leva ensure: o forge roda docker volume create"
            )
        if self.kind != "volume" and self.ensure is None:
            raise ValueError(
                f"{self.kind} exige ensure (o comando que cria se não existir)"
            )
        if self.kind != "volume" and self.name is not None:
            raise ValueError("name só vale para volume")
        return self


class Tls(Strict):
    cert: str
    key: str


class Route(Strict):
    """Por onde se entra: `host` -> `service:port`. O arquivo pode sobrepor tudo por alvo em
    `targets.<alvo>`; o que chega aqui já é o valor do alvo escolhido."""

    service: str
    port: Annotated[int, Field(gt=0, lt=65536)]
    host: Annotated[str, Field(pattern=r"^[A-Za-z0-9*][A-Za-z0-9.-]*$")]
    access: Literal["public", "private"] = "public"
    allow: list[str] = []
    tls: Tls | None = None
    """Só nginx: certificado e chave no host do proxy (caddy faz HTTPS sozinho)."""
    upstream: Annotated[str, Field(pattern=r"^[^\s/]+:[0-9]+$")] | None = None
    """Para onde o proxy manda (`host:porta`); padrão `<service>:<port>`, o nome na rede docker.
    Proxy instalado no host usa a porta publicada, ex.: `127.0.0.1:8000`."""

    @property
    def target_address(self) -> str:
        return self.upstream or f"{self.service}:{self.port}"

    @model_validator(mode="after")
    def _access(self) -> Self:
        if self.access == "private" and not self.allow:
            raise ValueError("route private exige allow (lista de CIDR)")
        if self.access == "public" and self.allow:
            raise ValueError("allow só vale para route private")
        bad = [cidr for cidr in self.allow if not _is_cidr(cidr)]
        if bad:
            raise ValueError(f"allow com CIDR inválido: {', '.join(bad)}")
        return self


def _is_cidr(text: str) -> bool:
    try:
        ipaddress.ip_network(text, strict=False)
    except ValueError:
        return False
    return True


class RecipeDocs(Strict):
    url: Annotated[str, Field(pattern=r"^https://")]
    version: str


class Recipe(Strict):
    """Contrato de `forge/recipes/<nome>/recipe.yaml`."""

    description: Annotated[str, Field(pattern=r"^[^\n]+$")]
    family: Name
    env: list[EnvName] = []
    requires: list[
        Annotated[
            str,
            Field(
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?(>=[0-9][0-9.]*)?$"
            ),
        ]
    ] = []
    runs_in_image: bool = False
    docs: RecipeDocs
    provada: bool = False


class Question(Strict):
    prompt: str
    default: str | None = None


class ArchetypeSpec(Strict):
    """Contrato de `forge/archetypes/<nome>/archetype.yaml`."""

    description: str
    questions: dict[str, Question] = {}
    recipes: list[Name] = []


def parse[M: BaseModel](model: type[M], data: object, source: str) -> M:
    """Valida `data` contra `model`; o erro nomeia o arquivo e o caminho do campo."""
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise ManifestError(_describe(error, source)) from error


def _describe(error: ValidationError, source: str) -> str:
    lines = [f"{source}: manifesto inválido"]
    for item in error.errors():
        where = ".".join(str(part) for part in item["loc"]) or "(raiz)"
        reason = (
            "campo desconhecido" if item["type"] == "extra_forbidden" else item["msg"]
        )
        lines.append(f"  {where}: {reason}")
    return "\n".join(lines)
