"""O envelope do alvo: env declarado + segredos resolvidos + variáveis FORGE_*."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from forge.contracts import SecretsProvider
from forge.errors import ForgeError
from forge.load import Loaded, secret_name
from forge.secrets import provider_for

ENVELOPE_MODE = 0o600
RESERVED_PREFIX = "FORGE_"


class EnvelopeError(ForgeError):
    pass


@dataclass(frozen=True)
class Envelope:
    target: str
    values: dict[str, str]
    secret_keys: frozenset[str]

    def secret_values(self) -> list[str]:
        return [self.values[key] for key in sorted(self.secret_keys)]

    def body(self) -> str:
        broken = sorted(k for k, v in self.values.items() if "\n" in v or "\r" in v)
        if broken:
            raise EnvelopeError(
                f"envelope de {self.target}: {', '.join(broken)} tem quebra de linha, "
                f"que um arquivo de env não guarda"
            )
        return "".join(f"{key}={self.values[key]}\n" for key in sorted(self.values))


def secret_refs(env: Mapping[str, str], where: str) -> dict[str, str]:
    """{chave do env: nome do segredo} para cada valor secret://NOME."""
    refs: dict[str, str] = {}
    for key, value in env.items():
        name = secret_name(value)
        if name is None:
            continue
        if not name:
            raise EnvelopeError(f"{where}.{key}: secret:// sem nome depois")
        refs[key] = name
    return refs


def forge_values(loaded: Loaded) -> dict[str, str]:
    return {
        "FORGE_TARGET": loaded.target_name,
        "FORGE_RELEASE": loaded.release,
        "FORGE_ENV_FILE": loaded.env_file.as_posix(),
        "FORGE_ROOT": loaded.root.as_posix(),
        "FORGE_STATE_PATH": loaded.state_path.as_posix(),
    }


def build_envelope(
    loaded: Loaded,
    provider: SecretsProvider | None,
    extra: Mapping[str, str] | None = None,
) -> Envelope:
    """`extra` são valores que o forge produz no deploy (ex.: imagem do artifact)."""
    where = f"forge.yaml: env do alvo {loaded.target_name}"
    refs = secret_refs(loaded.env, where)
    literals = {k: v for k, v in loaded.env.items() if k not in refs}
    _refuse_collisions(where, literals, refs, extra or {})
    fetched = _fetch(loaded.target_name, provider, sorted(set(refs.values())))
    values = {
        **literals,
        **{key: fetched[name] for key, name in refs.items()},
        **(extra or {}),
        **forge_values(loaded),
    }
    return Envelope(loaded.target_name, values, frozenset(refs))


def envelope_for(
    loaded: Loaded,
    extra: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Envelope:
    spec = loaded.target.secrets
    provider = (
        None
        if spec is None
        else provider_for(
            spec, target=loaded.target_name, root=loaded.root, environ=environ
        )
    )
    return build_envelope(loaded, provider, extra)


def _refuse_collisions(
    where: str,
    literals: Mapping[str, str],
    refs: Mapping[str, str],
    extra: Mapping[str, str],
) -> None:
    reserved = sorted(
        k for k in (*literals, *refs, *extra) if k.startswith(RESERVED_PREFIX)
    )
    if reserved:
        raise EnvelopeError(
            f"{where}: {', '.join(reserved)} usa o prefixo reservado FORGE_"
        )
    clashing = sorted(set(literals) & set(refs.values()))
    if clashing:
        raise EnvelopeError(
            f"{where}: {', '.join(clashing)} é valor literal e também nome de segredo; "
            f"um nome não pode ser as duas coisas"
        )
    doubled = sorted(set(extra) & (set(literals) | set(refs)))
    if doubled:
        raise EnvelopeError(
            f"{where}: {', '.join(doubled)} já é produzido pelo forge (artifact)"
        )


def _fetch(
    target: str, provider: SecretsProvider | None, names: list[str]
) -> dict[str, str]:
    if not names:
        return {}
    if provider is None:
        raise EnvelopeError(
            f"o alvo {target} pede {', '.join(names)} e não declara secrets: "
            f"sem provider não há de onde ler"
        )
    return provider.fetch(names)


def write_envelope(envelope: Envelope, path: Path) -> Path:
    """Escrita atômica com 0600: temporário no mesmo diretório e os.replace."""
    body = envelope.body()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(body)
        os.chmod(temporary, ENVELOPE_MODE)
        os.replace(temporary, path)
    except OSError as error:
        Path(temporary).unlink(missing_ok=True)
        raise EnvelopeError(
            f"não consegui escrever {path}: {error.strerror}"
        ) from error
    return path
