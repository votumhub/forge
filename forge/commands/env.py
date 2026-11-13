"""forge env: escreve o envelope do alvo, lista os nomes ou roda um comando dentro dele."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from forge.artifacts import artifact_env
from forge.commands._common import add_target_args, load_from
from forge.envelope import (
    Envelope,
    envelope_for,
    forge_values,
    secret_refs,
    write_envelope,
)
from forge.errors import ForgeError
from forge.executor import program_path
from forge.load import Loaded
from forge.procenv import base_env
from forge.state import read_state


def register(verbs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = verbs.add_parser(
        "env", help="escreve o envelope do alvo ou roda um comando dentro dele"
    )
    add_target_args(parser)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--output", type=Path, help="onde escrever (padrão .forge/env/<alvo>.env)"
    )
    group.add_argument("--names", action="store_true", help="só os nomes, sem valores")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirma o envelope de production, que leva segredo de produção",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- comando a rodar")
    parser.set_defaults(handler=run)


def deployed_images(loaded: Loaded) -> dict[str, str]:
    """Env dos artifacts da release atual, para que o comando local veja a mesma imagem."""
    current = read_state(loaded.state_path, loaded.target_name).current_release()
    digests = current.digests if current else {}
    return artifact_env(loaded.manifest.artifacts, digests)


def run(args: argparse.Namespace) -> int:
    loaded = load_from(args)
    command = _command(args.command)
    if args.names:
        print("\n".join(names(loaded)))
        return 0
    if loaded.mode == "production" and not args.yes:
        raise ForgeError(
            f"alvo {loaded.target_name} é production: o envelope leva segredo de produção para "
            f"um arquivo ou um comando; confirme com --yes (só os nomes: --names)"
        )
    envelope = envelope_for(loaded, deployed_images(loaded))
    path = write_envelope(envelope, args.output or loaded.env_file)
    if command:
        return run_inside(envelope, command)
    print(
        f"envelope de {loaded.target_name} em {path} ({len(envelope.values)} nomes, "
        f"{len(envelope.secret_keys)} segredo(s))"
    )
    return 0


def _command(parts: list[str]) -> list[str]:
    return parts[1:] if parts and parts[0] == "--" else parts


def names(loaded: Loaded) -> list[str]:
    refs = secret_refs(loaded.env, f"env do alvo {loaded.target_name}")
    every = {*loaded.env, *deployed_images(loaded), *forge_values(loaded)}
    return [f"{name} (segredo)" if name in refs else name for name in sorted(every)]


def run_inside(envelope: Envelope, command: list[str]) -> int:
    env = {**base_env(), **envelope.values}
    program = program_path(command[0], env)
    try:
        return subprocess.run([program, *command[1:]], env=env, check=False).returncode
    except FileNotFoundError as error:
        raise ForgeError(
            f"comando {command[0]} não encontrado no PATH do envelope; confira o nome ou "
            f"passe o caminho completo depois de `--`"
        ) from error
    except OSError as error:
        raise ForgeError(
            f"o sistema não conseguiu executar {program} ({error.strerror or error}); "
            f"confira se é um executável desta plataforma"
        ) from error
