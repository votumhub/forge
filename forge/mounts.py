"""Código montado do disco: bind mount de serviço cuja origem fica dentro da raiz do projeto
(fora de `.forge/`), ou cuja origem tem variável que o forge não resolve sem os segredos (lado
seguro). Volume nomeado, volume anônimo e caminho fora da raiz são dado: nunca voltam com a
release. A volta leva o checkout inteiro ao commit da release, e recusa antes quando o checkout
sobrescreveria arquivo que existe no disco fora do git, no caminho ou num diretório dele."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from forge.envelope import forge_values
from forge.errors import ForgeError
from forge.gitinfo import (
    FORGE_DIR,
    GitInfo,
    checkout_detached,
    read_git,
    shadowed,
)
from forge.load import Loaded, secret_name
from forge.policy import compose_files, interpolated, loaded_compose
from forge.procenv import base_env
from forge.state import Release, StateError, read_trees, tree_of, trees_file

DIRTY_SUFFIX = "-dirty"
UNRESOLVED = "${"


class CodeRestoreError(ForgeError):
    pass


def code_mounts(loaded: Loaded) -> list[str]:
    """Origens, relativas à raiz, dos bind mounts de código de todos os serviços do bundle."""
    env = compose_env(loaded)
    found = {
        inside
        for service in loaded.services.values()
        for path in compose_files(loaded.root, service.compose, env)[0]
        for source in _bind_sources(path, env)
        if (inside := _inside(loaded.root, source)) is not None
    }
    return sorted(found)


def compose_env(loaded: Loaded) -> dict[str, str]:
    """O que o compose enxerga sem os segredos: o ambiente mínimo do processo, o env literal
    do alvo e os valores do forge. Segredo e imagem de artifact ficam sem resolver."""
    literals = {k: v for k, v in loaded.env.items() if secret_name(v) is None}
    return {**base_env(), **literals, **forge_values(loaded)}


def _bind_sources(path: Path, env: Mapping[str, str]) -> Iterator[Path]:
    services = loaded_compose(path).get("services")
    for spec in services.values() if isinstance(services, dict) else []:
        volumes = spec.get("volumes") if isinstance(spec, dict) else None
        for volume in volumes if isinstance(volumes, list) else []:
            source = _bind_source(volume, env)
            if source is not None:
                yield path.parent / os.path.expanduser(source)


def _bind_source(volume: object, env: Mapping[str, str]) -> str | None:
    """Sintaxe longa: `type: bind`. Curta: `origem:destino`, em que origem com cara de caminho
    ou com variável sem valor é bind, e nome solto é volume nomeado."""
    if isinstance(volume, dict):
        source = volume.get("source")
        if volume.get("type") != "bind" or source is None:
            return None
        return interpolated(str(source), env)
    text = interpolated(str(volume), env)
    drive, rest = (text[:2], text[2:]) if text[1:2] == ":" else ("", text)
    source, colon, _ = _split_source(rest)
    if not colon:
        return None
    path_like = "/" in source or "\\" in source or source.startswith((".", "~"))
    return drive + source if path_like or drive or UNRESOLVED in source else None


def _split_source(text: str) -> tuple[str, str, str]:
    """`${NOME:-padrão}` sem valor fica no texto: o `:` dele não separa origem e destino."""
    end = text.find("}") + 1 if text.startswith(UNRESOLVED) else 0
    source, colon, target = text[end:].partition(":")
    return text[:end] + source, colon, target


def _inside(root: Path, source: Path) -> str | None:
    try:
        relative = Path(os.path.normpath(source)).relative_to(root)
    except ValueError:
        return None
    if relative.parts[:1] == (FORGE_DIR,):
        return None
    return relative.as_posix()


@dataclass(frozen=True)
class Restored:
    """`moved`: o checkout mudou. `dirty`: o que se sabe da árvore da release (None = nada)."""

    moved: bool
    dirty: bool | None


def restore_code(
    loaded: Loaded, back: Release, report: Callable[[str], None]
) -> Restored:
    """Antes de subir a release de volta, o código montado volta ao commit dela. Só com código
    montado o arquivo de árvores é lido. Falha fechado quando não há commit para onde voltar
    ou quando o checkout apagaria mudança local ou arquivo fora do git."""
    mounts = code_mounts(loaded)
    if not mounts:
        return Restored(moved=False, dirty=None)
    where = f"o código montado do disco ({', '.join(mounts)})"
    dirty = _built_dirty(loaded, back, _recorded_dirty(loaded, back, report))
    sha = _commit(loaded, back, dirty, where)
    git = read_git(loaded.root)
    if git is None:
        raise CodeRestoreError(
            f"{loaded.root.as_posix()} não é um repositório git: {where} não tem como "
            f"voltar ao commit {sha[:12]} da release {back.id}, e a volta não sobe imagem "
            f"antiga com código novo. {_redeploy(loaded)}"
        )
    if not _needs_checkout(loaded, git, back, where, sha):
        report(f"{where}: já está no commit {sha[:12]}")
        return Restored(moved=False, dirty=False)
    checkout_detached(loaded.root, sha)
    report(f"{where}: checkout do commit {sha[:12]} (HEAD destacado)")
    return Restored(moved=True, dirty=False)


def _recorded_dirty(
    loaded: Loaded, back: Release, report: Callable[[str], None]
) -> bool | None:
    """Arquivo de árvores ilegível: em production a release conta como sem entrada (e é
    limpa, porque production só sai de commit limpo); em development a volta recusa."""
    try:
        trees = read_trees(loaded.state_path, loaded.target_name)
    except StateError as error:
        if loaded.mode != "production":
            raise
        report(
            f"{error}; production só sai de commit limpo, a release {back.id} conta limpa"
        )
        return None
    known = tree_of(trees, back)
    return known.dirty if known else None


def _built_dirty(loaded: Loaded, back: Release, dirty: bool | None) -> bool | None:
    """Sem registro (release gravada antes do arquivo de árvores): production sempre sai de
    commit limpo, e o id `<sha12>` ou `<sha12>-dirty` diz sozinho; tag ou id dado à mão não."""
    if dirty is not None:
        return dirty
    if back.id.endswith(DIRTY_SUFFIX):
        return True
    if loaded.mode == "production" or back.id == (back.git_sha or "")[:12]:
        return False
    return None


def _commit(loaded: Loaded, back: Release, dirty: bool | None, where: str) -> str:
    if back.git_sha is None:
        raise CodeRestoreError(
            f"a release {back.id} não guardou o commit (subiu fora de um repositório git), "
            f"e {where} não tem para onde voltar. {_redeploy(loaded)}"
        )
    if dirty is None:
        raise CodeRestoreError(
            f"o state não diz se a release {back.id} subiu de árvore limpa (o registro dela não está em "
            f"{trees_file(loaded.state_path, loaded.target_name).name}, e o id dela não diz): "
            f"{where} pode não estar em commit nenhum. {_redeploy(loaded)}"
        )
    if dirty:
        raise CodeRestoreError(
            f"a release {back.id} subiu de árvore suja: {where} dela não está em commit "
            f"nenhum e não tem para onde voltar. {_redeploy(loaded)}"
        )
    return back.git_sha


def _needs_checkout(
    loaded: Loaded, git: GitInfo, back: Release, where: str, sha: str
) -> bool:
    if git.dirty:
        raise CodeRestoreError(
            _refusal(loaded, back, where, "há mudança não commitada")
        )
    if git.sha == sha:
        return False
    found = shadowed(loaded.root, sha)
    if found:
        outside = (
            f"há arquivo fora do git no caminho do que o commit rastreia "
            f"({', '.join(found)})"
        )
        raise CodeRestoreError(_refusal(loaded, back, where, outside))
    return True


def _refusal(loaded: Loaded, back: Release, where: str, what: str) -> str:
    confirm = " --yes" if loaded.mode == "production" else ""
    sha = (back.git_sha or "")[:12]
    return (
        f"{what} em {loaded.root.as_posix()}, e a volta para a release {back.id} faz "
        f"checkout do commit {sha} para {where} voltar junto com as imagens; o checkout passaria "
        f"por cima. Nada foi mexido: guarde a mudança (git commit, git stash -u, ou "
        f"mova o arquivo para fora do projeto) e rode de novo forge rollback -t "
        f"{loaded.target_name}{confirm}"
    )


def _redeploy(loaded: Loaded) -> str:
    return (
        f"Rode forge deploy -t {loaded.target_name} a partir do commit que quer no ar"
    )
