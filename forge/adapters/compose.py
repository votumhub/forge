"""Adaptador compose: sobe os arquivos do serviço com -p e --env-file; nada é gerado."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field


from forge.contracts import DeployContext, RunResult
from forge.errors import ForgeError
from forge.executor import OUTPUT_TAIL, require_ok
from forge.model import Health, Service
from forge.policy import compose_files, loaded_compose

POLL_SECONDS = 2.0
BLIND = "não comparado (lê segredo ou o envelope, que o plan não resolve)"
ENV_REFERENCE = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::?-(?P<default>[^}]*))?\}"
)


class ComposeError(ForgeError):
    pass


class HealthError(ComposeError):
    pass


def expand(text: str, env: Mapping[str, str], where: str) -> str:
    """Expande ${NOME} e ${NOME:-padrão} com o env do alvo; nome sem valor é erro."""

    def replace(match: re.Match[str]) -> str:
        name, default = match.group("name"), match.group("default")
        if name in env and env[name] != "":
            return env[name]
        if default is not None:
            return default
        raise HealthError(f"{where}: ${{{name}}} não tem valor no env do alvo")

    return ENV_REFERENCE.sub(replace, text)


def project_of(ctx: DeployContext, service: Service) -> str:
    return service.project or ctx.project


def compose_argv(ctx: DeployContext, service: Service) -> list[str]:
    argv = [
        "docker",
        "compose",
        "-p",
        project_of(ctx, service),
        "--env-file",
        ctx.env_file.as_posix(),
    ]
    for path in service.compose:
        argv += ["-f", path]
    return argv


def external_networks(ctx: DeployContext, service: Service) -> list[str]:
    """As redes externas de todos os arquivos que o compose lê: segue o `include:` com o
    caminho interpolado pelo env do alvo, sem chamar o compose e sem escrever nada."""
    return sorted(
        {
            expand(str(spec.get("name", key)), ctx.env, f"rede {key}")
            for path in compose_files(ctx.root, service.compose, ctx.env)[0]
            for key, spec in (loaded_compose(path).get("networks") or {}).items()
            if isinstance(spec, dict) and spec.get("external")
        }
    )


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - URL do manifesto
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


@dataclass
class ComposeAdapter:
    probe_url: Callable[[str], bool] = http_ok
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    created_networks: list[str] = field(default_factory=list)
    siblings: Mapping[str, frozenset[str]] = field(default_factory=dict)
    """project -> serviços compose que algum resource do bundle declara nele: vários
    resources dividem o project padrão, e o contêiner de um não é "remover" no plan do outro."""

    def plan(self, ctx: DeployContext, name: str, service: Service) -> list[str]:
        """Compara `config --hash` com a label config-hash dos containers do project; o
        serviço cujo hash muda com o `twin` depende de segredo ou do envelope e não é comparado."""
        wanted = desired_hashes(ctx, service)
        varied = desired_hashes(ctx.twin, service) if ctx.twin else wanted
        running = running_hashes(ctx, service)
        lines = [f"serviço {name} (project {project_of(ctx, service)}):"]
        for svc, digest in sorted(wanted.items()):
            blind = varied.get(svc) != digest
            lines.append(f"  {svc}: {_change(running.get(svc), digest, blind)}")
        claimed = set(wanted) | self.siblings.get(project_of(ctx, service), frozenset())
        lines += [
            f"  {svc}: remover (não está mais no compose)"
            for svc in sorted(set(running) - claimed)
        ]
        return lines

    def up(self, ctx: DeployContext, name: str, service: Service) -> None:
        self._networks(ctx, service)
        base = compose_argv(ctx, service)
        require_ok(
            ctx.executor.run([*base, "up", "-d"], cwd=ctx.root, env=ctx.env),
            f"up do serviço {name}",
        )
        if service.release_services:
            recreate = [
                *base,
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                *service.release_services,
            ]
            require_ok(
                ctx.executor.run(recreate, cwd=ctx.root, env=ctx.env),
                f"recriação da release em {name}",
            )

    def _networks(self, ctx: DeployContext, service: Service) -> None:
        for network in external_networks(ctx, service):
            found = ctx.executor.run(
                ["docker", "network", "inspect", network], cwd=ctx.root
            )
            if not found.ok:
                require_ok(
                    ctx.executor.run(
                        ["docker", "network", "create", network], cwd=ctx.root
                    ),
                    f"criação da rede {network}",
                )
                self.created_networks.append(network)

    def health(self, ctx: DeployContext, name: str, service: Service) -> None:
        if service.health is None:
            return
        check = self._check(ctx, service, service.health)
        deadline = self.clock() + service.health.timeout
        while not check():
            if self.clock() >= deadline:
                raise HealthError(
                    f"serviço {name} não ficou saudável em {service.health.timeout:g}s "
                    f"({_describe(service.health)})"
                )
            self.sleep(POLL_SECONDS)

    def _check(
        self, ctx: DeployContext, service: Service, health: Health
    ) -> Callable[[], bool]:
        if health.url is not None:
            url = expand(health.url, ctx.env, "health.url")
            return lambda: self.probe_url(url)
        if health.command is not None:
            argv = health.command
            return lambda: ctx.executor.run(argv, cwd=ctx.root, env=ctx.env).ok
        wanted = list(health.healthy or [])
        return lambda: all_healthy(ctx, service, wanted)

    def destroy(
        self, ctx: DeployContext, name: str, service: Service, *, data: bool = False
    ) -> None:
        """`data` remove também os volumes declarados no compose (externos ficam: são
        storage, que o StorageAdapter remove)."""
        argv = [*compose_argv(ctx, service), "down", *(["-v"] if data else [])]
        require_ok(
            ctx.executor.run(argv, cwd=ctx.root, env=ctx.env), f"down do serviço {name}"
        )


def desired_hashes(ctx: DeployContext, service: Service) -> dict[str, str]:
    argv = [*compose_argv(ctx, service), "config", "--hash", "*"]
    return _pairs(
        require_ok(ctx.executor.run(argv, cwd=ctx.root, env=ctx.env), "config --hash")
    )


def running_hashes(ctx: DeployContext, service: Service) -> dict[str, str]:
    label = "com.docker.compose"
    argv = [
        "docker", "ps", "-a",
        "--filter", f"label={label}.project={project_of(ctx, service)}",
        "--format", f'{{{{.Label "{label}.service"}}}} {{{{.Label "{label}.config-hash"}}}}',
    ]  # fmt: skip
    return _pairs(
        require_ok(ctx.executor.run(argv, cwd=ctx.root, env=ctx.env), "docker ps")
    )


def _pairs(result: RunResult) -> dict[str, str]:
    """Lê a saída sem máscara; o erro mostra só a mascarada."""
    rows = [line.split() for line in result.data.splitlines() if line.strip()]
    if any(len(parts) != 2 for parts in rows):
        raise ComposeError(
            f"saída inesperada do docker:\n{result.output[-OUTPUT_TAIL:]}"
        )
    return {parts[0]: parts[1] for parts in rows}


def _change(current: str | None, desired: str, blind: bool) -> str:
    if current is None:
        return "criar"
    if blind:
        return BLIND
    return "sem mudança" if current == desired else "atualizar"


def all_healthy(ctx: DeployContext, service: Service, wanted: list[str]) -> bool:
    argv = [*compose_argv(ctx, service), "ps", "--format", "json", *wanted]
    result = ctx.executor.run(argv, cwd=ctx.root, env=ctx.env)
    if not result.ok:
        return False
    try:
        rows = parse_rows(result.data)
    except ComposeError as error:
        raise ComposeError(
            f"docker compose ps devolveu algo ilegível: {result.output[:200]}"
        ) from error.__cause__
    states = {row.get("Service"): row.get("Health") for row in rows}
    return all(states.get(name) == "healthy" for name in wanted)


def parse_rows(output: str) -> list[dict[str, str]]:
    """`ps --format json` sai como array (compose antigo) ou uma linha JSON por container."""
    text = output.strip()
    try:
        if text.startswith("["):
            return list(json.loads(text))
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    except json.JSONDecodeError as error:
        raise ComposeError(
            f"docker compose ps devolveu algo ilegível: {text[:200]}"
        ) from error


def _describe(health: Health) -> str:
    if health.url is not None:
        return f"url {health.url}"
    if health.command is not None:
        return f"command {' '.join(health.command)}"
    return f"healthy {', '.join(health.healthy or [])}"
