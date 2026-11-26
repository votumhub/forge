"""Executor local: argv sem shell, env explícito, saída mascarada."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from forge.contracts import STEP_TIMEOUT, RunResult
from forge.errors import ForgeError
from forge.procenv import base_env
from forge.secrets import mask

OUTPUT_TAIL = 2000
WINDOWS = os.name == "nt"
DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"
DOCKER = "docker"
DOCKER_UNREACHABLE = re.compile(
    r"connect to the docker (daemon|api)|error during connect", re.IGNORECASE
)


class StepError(ForgeError):
    pass


def show(argv: Sequence[str]) -> str:
    return " ".join(argv)


def program_path(name: str, env: Mapping[str, str]) -> str:
    """O executável que o sistema roda: no Windows só arquivo com extensão do PATHEXT,
    nunca o script sem extensão que mora ao lado do .exe/.cmd."""
    path = env.get("PATH")
    if not WINDOWS or Path(name).name != name:
        return shutil.which(name, path=path) or name
    extensions = [e for e in (env.get("PATHEXT") or DEFAULT_PATHEXT).split(";") if e]
    if any(name.upper().endswith(e.upper()) for e in extensions):
        return shutil.which(name, path=path) or name
    for folder in filter(None, (path or "").split(os.pathsep)):
        for extension in extensions:
            candidate = Path(folder) / f"{name}{extension}"
            if candidate.is_file():
                return str(candidate)
    return name


class LocalExecutor:
    def __init__(self, hidden: Sequence[str] = ()) -> None:
        self._hidden: list[str] = list(hidden)

    def hide(self, values: Sequence[str]) -> None:
        self._hidden.extend(values)

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: float = STEP_TIMEOUT,
    ) -> RunResult:
        full_env = {**base_env(), **(env or {})}
        shown = mask(show(argv), self._hidden)
        program = program_path(argv[0], full_env)
        try:
            done = subprocess.run(
                [program, *argv[1:]],
                cwd=cwd,
                env=full_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise StepError(
                f"`{shown}` passou de {timeout:g}s e foi interrompido"
            ) from error
        except OSError as error:
            raise StepError(_not_started(shown, argv[0], program, error)) from error
        output = mask(done.stdout, self._hidden)
        _refuse_unreachable_docker(shown, argv[0], done.returncode, output)
        return RunResult(tuple(argv), done.returncode, output, done.stdout)


def _not_started(shown: str, name: str, program: str, error: OSError) -> str:
    if isinstance(error, FileNotFoundError) and _is_docker(name):
        return (
            f"`{shown}`: o comando docker não foi encontrado no PATH; o forge opera o alvo "
            f"pelo docker: confira com `docker info` neste terminal"
        )
    if isinstance(error, FileNotFoundError):
        return f"`{shown}`: comando {name} não encontrado; confira se está no PATH"
    return (
        f"`{shown}`: o sistema não conseguiu executar {program} "
        f"({error.strerror or error}); confira se é um executável desta plataforma"
    )


def _refuse_unreachable_docker(shown: str, name: str, code: int, output: str) -> None:
    if code == 0 or not _is_docker(name):
        return
    cause = next(
        (
            line.strip()
            for line in output.splitlines()
            if DOCKER_UNREACHABLE.search(line)
        ),
        None,
    )
    if cause is not None:
        raise StepError(
            f"`{shown}`: o docker está instalado, mas o daemon não respondeu ({cause}); "
            f"o serviço do docker precisa estar rodando e acessível a este usuário: "
            f"confira com `docker info`"
        )


def _is_docker(name: str) -> bool:
    return Path(name).stem.lower() == DOCKER


def require_ok(result: RunResult, what: str) -> RunResult:
    if not result.ok:
        raise StepError(
            f"{what} falhou em `{show(result.argv)}` (código {result.code}):\n"
            f"{result.output[-OUTPUT_TAIL:]}"
        )
    return result
