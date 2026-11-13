"""Providers de segredo: env, dotenv e exec. Nenhum inventa valor; faltou, recusa."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from forge.contracts import SecretsProvider
from forge.errors import ForgeError, read_utf8
from forge.model import DotenvSecrets, EnvSecrets, ExecSecrets
from forge.procenv import base_env

STDERR_TAIL = 600
MASK = "***"


class SecretsError(ForgeError):
    pass


def _pick(held: Mapping[str, str], names: Sequence[str], source: str) -> dict[str, str]:
    missing = sorted(name for name in names if name not in held)
    if missing:
        raise SecretsError(
            f"{source} não tem {', '.join(missing)}: segredo que falta é recusa"
        )
    picked = {name: held[name] for name in names}
    broken = sorted(
        name for name, value in picked.items() if "\n" in value or "\r" in value
    )
    if broken:
        raise SecretsError(
            f"{source}: {', '.join(broken)} tem quebra de linha, que o envelope não guarda"
        )
    return picked


@dataclass(frozen=True)
class EnvProvider:
    environ: Mapping[str, str]

    def fetch(self, names: Sequence[str]) -> dict[str, str]:
        return _pick(self.environ, names, "o ambiente")


@dataclass(frozen=True)
class DotenvProvider:
    path: Path

    def fetch(self, names: Sequence[str]) -> dict[str, str]:
        try:
            text = read_utf8(self.path)
        except FileNotFoundError as error:
            raise SecretsError(
                f"o arquivo de segredos {self.path} não existe: crie-o, fora do git, "
                "com uma linha NOME=valor por segredo"
            ) from error
        except OSError as error:
            raise SecretsError(
                f"não consegui ler {self.path}: {error.strerror}"
            ) from error
        return _pick(parse_dotenv(text), names, str(self.path))


def parse_dotenv(text: str) -> dict[str, str]:
    held: dict[str, str] = {}
    for line in text.splitlines():
        clean = line.strip()
        if clean.startswith("export "):
            clean = clean[len("export ") :].strip()
        if not clean or clean.startswith("#") or "=" not in clean:
            continue
        name, _, value = clean.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        held[name.strip()] = value
    return held


@dataclass(frozen=True)
class ExecProvider:
    command: tuple[str, ...]
    target: str
    cwd: Path
    timeout: float
    passthrough: Mapping[str, str]

    def fetch(self, names: Sequence[str]) -> dict[str, str]:
        done = self._call(names)
        if done.returncode != 0:
            raise SecretsError(
                f"provider exec {self.command[0]} saiu com código {done.returncode}; "
                f"stderr: {self._masked(done.stderr, done.stdout)}"
            )
        return _pick(
            self._decode(done.stdout), names, f"provider exec {self.command[0]}"
        )

    def _call(self, names: Sequence[str]) -> subprocess.CompletedProcess[str]:
        env = {**base_env(), **self.passthrough, "FORGE_TARGET": self.target}
        try:
            return subprocess.run(
                [*self.command, *names],
                cwd=self.cwd,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise SecretsError(
                f"provider exec {self.command[0]} passou de {self.timeout:g}s e foi interrompido"
            ) from error
        except OSError as error:
            raise SecretsError(
                f"provider exec {self.command[0]}: {error.strerror}"
            ) from error

    def _decode(self, stdout: str) -> dict[str, str]:
        refusal = f"provider exec {self.command[0]}: stdout não é um objeto JSON de texto em texto"
        try:
            decoded = json.loads(stdout)
        except json.JSONDecodeError:
            raise SecretsError(refusal) from None
        if not isinstance(decoded, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in decoded.items()
        ):
            raise SecretsError(refusal)
        return decoded

    def _masked(self, stderr: str, stdout: str) -> str:
        hidden = [*self.passthrough.values(), *_json_values(stdout)]
        return mask(stderr.strip()[-STDERR_TAIL:], hidden) or "(vazio)"


def _json_values(stdout: str) -> list[str]:
    try:
        decoded = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    return (
        [v for v in decoded.values() if isinstance(v, str)]
        if isinstance(decoded, dict)
        else []
    )


def mask(text: str, hidden: Sequence[str]) -> str:
    for value in sorted({v for v in hidden if v}, key=len, reverse=True):
        text = text.replace(value, MASK)
    return text


SecretsSpec = EnvSecrets | DotenvSecrets | ExecSecrets


def provider_for(
    spec: SecretsSpec,
    *,
    target: str,
    root: Path,
    environ: Mapping[str, str] | None = None,
) -> SecretsProvider:
    source = os.environ if environ is None else environ
    if isinstance(spec, EnvSecrets):
        return EnvProvider(source)
    if isinstance(spec, DotenvSecrets):
        return DotenvProvider((root / Path(spec.path).expanduser()).resolve())
    return ExecProvider(
        command=tuple(spec.command),
        target=target,
        cwd=root,
        timeout=spec.timeout,
        passthrough={name: source[name] for name in spec.passthrough if name in source},
    )
