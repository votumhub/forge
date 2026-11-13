"""Fronteiras do forge: quem fornece segredo, quem executa comando, quem sobe recurso."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

STEP_TIMEOUT = 1800.0
HOOK_NAMES = ("before_deploy", "before_up", "after_up")
Mode = Literal["development", "production"]


class SecretsProvider(Protocol):
    """Devolve exatamente os nomes pedidos, ou levanta erro; nunca inventa valor."""

    def fetch(self, names: Sequence[str], /) -> dict[str, str]: ...


@dataclass(frozen=True)
class RunResult:
    argv: tuple[str, ...]
    code: int
    output: str

    @property
    def ok(self) -> bool:
        return self.code == 0


class Executor(Protocol):
    """Roda argv sem shell, com env explícito, e mascara na saída o que foi escondido."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: float = STEP_TIMEOUT,
    ) -> RunResult: ...

    def hide(self, values: Sequence[str], /) -> None: ...


@dataclass(frozen=True)
class DeployContext:
    root: Path
    target: str
    mode: Mode
    release: str
    project: str
    env: Mapping[str, str]
    env_file: Path
    build_dir: Path
    executor: Executor
    notes: list[str] = field(default_factory=list)
    twin: DeployContext | None = None
    """Só no plan: o mesmo contexto com outro marcador no lugar de cada segredo e outro
    arquivo de envelope. O que muda entre os dois depende do que o plan não resolve."""


class Adapter[R](Protocol):
    """Leva um tipo de recurso ao alvo. `plan` não muda nada; `up` aplica; `health` espera."""

    def plan(self, ctx: DeployContext, name: str, resource: R, /) -> list[str]: ...

    def up(self, ctx: DeployContext, name: str, resource: R, /) -> None: ...

    def health(self, ctx: DeployContext, name: str, resource: R, /) -> None: ...

    def destroy(
        self, ctx: DeployContext, name: str, resource: R, /, *, data: bool = False
    ) -> None:
        """Derruba; `data` (destroy --data) leva junto o dado que o próprio recurso declara."""


def hook_env(ctx: DeployContext, hook: str) -> dict[str, str]:
    """Env de um gancho: o envelope inteiro mais o nome do gancho em FORGE_HOOK."""
    if hook not in HOOK_NAMES:
        raise ValueError(f"gancho desconhecido: {hook}")
    return {**ctx.env, "FORGE_HOOK": hook}
