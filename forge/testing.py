"""Dublês para testar bundles e adaptadores sem rodar nada de verdade."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from forge.contracts import STEP_TIMEOUT, RunResult
from forge.errors import ForgeError
from forge.secrets import mask


@dataclass(frozen=True)
class Call:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    timeout: float


@dataclass(frozen=True)
class Response:
    prefix: tuple[str, ...]
    code: int
    output: str
    error: ForgeError | None


@dataclass
class FakeExecutor:
    """Registra cada chamada; responde pela regra mais recente cujo prefixo bate com o argv."""

    calls: list[Call] = field(default_factory=list)
    responses: list[Response] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)

    def respond(
        self,
        prefix: Sequence[str],
        *,
        code: int = 0,
        output: str = "",
        error: ForgeError | None = None,
    ) -> None:
        self.responses.append(Response(tuple(prefix), code, output, error))

    def hide(self, values: Sequence[str]) -> None:
        self.hidden.extend(values)

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: float = STEP_TIMEOUT,
    ) -> RunResult:
        argv = tuple(argv)
        self.calls.append(Call(argv, cwd, dict(env or {}), timeout))
        for response in reversed(self.responses):
            if argv[: len(response.prefix)] == response.prefix:
                if response.error is not None:
                    raise response.error
                return RunResult(
                    argv,
                    response.code,
                    mask(response.output, self.hidden),
                    response.output,
                )
        return RunResult(argv, 0, "")

    def argvs(self) -> list[tuple[str, ...]]:
        return [call.argv for call in self.calls]
