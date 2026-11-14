"""State por alvo (`<state_path>/<alvo>.json`), a árvore de cada release
(`<state_path>/<alvo>.trees.json`) e lock de deploy (`<state_path>/.lock`). Os leitores
ignoram campo que não conhecem: o que um forge mais novo acrescentar não os quebra."""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from forge.errors import ForgeError, read_utf8

STATE_MODE = 0o644
LOCK_FILE = ".lock"
STALE_AFTER_SECONDS = 3600
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_INVALID_PARAMETER = 87
STILL_ACTIVE = 259
_UNRELEASED: set[Path] = set()


class StateError(ForgeError):
    pass


class LockError(ForgeError):
    pass


class Release(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    git_sha: str | None
    at: str
    status: Literal["ok", "failed"]
    digests: dict[str, str] = {}


class Tree(BaseModel):
    """Como estava a árvore no deploy ok de uma release: `dirty` com mudança não commitada,
    `branch` a branch do checkout antes da tentativa. None = desconhecido. `git_sha` e `at`
    amarram a entrada ao registro do state: outro deploy com o mesmo id não a herda."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    git_sha: str | None = None
    at: str | None = None
    dirty: bool | None = None
    branch: str | None = None


TREES = TypeAdapter(dict[str, Tree])


class State(BaseModel):
    model_config = ConfigDict(extra="ignore")

    target: str
    current: str | None = None
    releases: list[Release] = []

    def current_release(self) -> Release | None:
        """A última entrada ok com o id da current: a tentativa falha de mesmo id não conta."""
        return next(
            (
                r
                for r in reversed(self.releases)
                if r.id == self.current and r.status == "ok"
            ),
            None,
        )

    def rollback_candidate(self) -> Release:
        candidate = self._candidate()
        if candidate is None:
            raise StateError(
                f"alvo {self.target}: não há release ok diferente da atual "
                f"({self.current or 'nenhuma'}) para voltar; o rollback só volta a uma "
                f"release que já subiu ok. Corrija e rode forge deploy -t {self.target}"
            )
        return candidate

    def rollback_reach(self) -> set[str]:
        """Ids que o rollback alcança sem deploy novo: a current e a candidata dele."""
        candidate = self._candidate()
        reach = {self.current} if self.current else set()
        return reach | ({candidate.id} if candidate else set())

    def _candidate(self) -> Release | None:
        """Última tentativa falhou: volta à current (o último ok), que o deploy falho pode ter
        deixado pela metade. Sem falha pendente: o ok anterior à current."""
        current = self.current_release()
        if current is not None and self.releases[-1].status == "failed":
            return current
        return next(
            (
                r
                for r in reversed(self.releases)
                if r.status == "ok" and r.id != self.current
            ),
            None,
        )


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def state_file(state_path: Path, target: str) -> Path:
    return state_path / f"{target}.json"


def read_state(state_path: Path, target: str) -> State:
    path = state_file(state_path, target)
    if not path.is_file():
        return State(target=target)
    try:
        return State.model_validate_json(read_utf8(path))
    except ForgeError as error:
        raise StateError(str(error)) from error
    except ValidationError as error:
        raise StateError(
            f"{path}: state ilegível ({error.error_count()} erro(s))"
        ) from error


def record(state_path: Path, target: str, release: Release) -> State:
    state = read_state(state_path, target)
    state.releases.append(release)
    if release.status == "ok":
        state.current = release.id
    _write_atomic(
        state_file(state_path, target), state.model_dump_json(indent=2) + "\n"
    )
    return state


def trees_file(state_path: Path, target: str) -> Path:
    return state_path / f"{target}.trees.json"


def read_trees(state_path: Path, target: str) -> dict[str, Tree]:
    """Release id -> árvore, da mais antiga para a mais recente; sem o arquivo, vazio."""
    path = trees_file(state_path, target)
    if not path.is_file():
        return {}
    try:
        return TREES.validate_json(read_utf8(path))
    except ForgeError as error:
        raise StateError(str(error)) from error
    except ValidationError as error:
        raise StateError(
            f"{path}: arquivo ilegível ({error.error_count()} erro(s))"
        ) from error


def tree_of(trees: dict[str, Tree], release: Release) -> Tree | None:
    """A entrada da release, só quando é do mesmo registro (mesmo commit e mesmo momento)."""
    tree = trees.get(release.id)
    if tree is None or (tree.git_sha, tree.at) != (release.git_sha, release.at):
        return None
    return tree


def record_tree(
    state_path: Path, target: str, release: Release, tree: Tree
) -> list[str]:
    """Grava a entrada da release; o arquivo ilegível é regravado e vira aviso, nunca erro."""
    path, notes = trees_file(state_path, target), []
    try:
        trees = read_trees(state_path, target)
    except StateError as error:
        trees = {}
        notes.append(f"{path.as_posix()} estava ilegível ({error}); regravado do zero")
    trees.pop(release.id, None)
    trees[release.id] = tree.model_copy(
        update={"git_sha": release.git_sha, "at": release.at}
    )
    _write_atomic(path, TREES.dump_json(trees, indent=2).decode("utf-8") + "\n")
    return notes


def _write_atomic(path: Path, body: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    except OSError as error:
        raise StateError(f"não consegui gravar {path}: {error.strerror}") from error
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(body)
        os.chmod(temporary, STATE_MODE)
        os.replace(temporary, path)
    except OSError as error:
        Path(temporary).unlink(missing_ok=True)
        raise StateError(f"não consegui gravar {path}: {error.strerror}") from error


@contextmanager
def deploy_lock(state_path: Path, *, force: bool = False) -> Iterator[Path]:
    """Lock exclusivo (O_EXCL); liberado sempre, inclusive em exceção (ou, sem permissão, avisado)."""
    path = state_path / LOCK_FILE
    state_path.mkdir(parents=True, exist_ok=True)
    if force:
        path.unlink(missing_ok=True)
    _acquire(path)
    try:
        yield path
    finally:
        _release(path)


def _release(path: Path) -> None:
    """O lock que não sai (pasta sem escrita) não pode esconder a causa que já está subindo;
    fica anotado para o próximo deploy deste processo (a volta automática) reassumi-lo."""
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        _UNRELEASED.add(path)
        print(
            f"forge: não consegui remover o lock {path}: {error.strerror}. Conserte a "
            "permissão da pasta; o próximo deploy deste host assume o lock depois que este "
            "processo sair",
            file=sys.stderr,
        )


def _acquire(path: Path) -> None:
    """Cria o lock; se ele existe e o dono está comprovadamente morto, assume-o uma vez."""
    if path in _UNRELEASED and _held_by_me(path):
        _UNRELEASED.discard(path)
        return
    try:
        _create(path)
    except FileExistsError:
        _take_orphan(path)


def _held_by_me(path: Path) -> bool:
    try:
        holder = json.loads(_read_lock(path) or b"")
    except ValueError:
        return False
    mine = {"pid": os.getpid(), "host": socket.gethostname()}
    return isinstance(holder, dict) and all(holder.get(k) == v for k, v in mine.items())


def _create(path: Path) -> None:
    holder = {"pid": os.getpid(), "host": socket.gethostname(), "at": now_iso()}
    handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(holder, stream)


def _take_orphan(path: Path) -> None:
    seen = _read_lock(path)
    pid = _dead_holder(seen)
    if pid is None or not _claim(path, seen):
        raise LockError(_busy(path))
    print(f"forge: lock do pid {pid} (morto) liberado: {path}", file=sys.stderr)
    try:
        _create(path)
    except FileExistsError:
        raise LockError(_busy(path)) from None


def _read_lock(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _dead_holder(seen: bytes | None) -> int | None:
    """Pid do dono quando ele é deste host e comprovadamente morto; senão None."""
    if seen is None:
        return None
    try:
        holder = json.loads(seen)
        pid, host = int(holder["pid"]), holder["host"]
    except (ValueError, KeyError, TypeError):
        return None
    if host != socket.gethostname() or not _is_dead(pid):
        return None
    return pid


def _is_dead(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_exited(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OverflowError):
        return False
    return _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    # O nome do processo vem entre parênteses e pode conter ")"; o estado é o campo seguinte.
    return stat.rpartition(")")[2].split()[:1] == ["Z"]


def _windows_exited(pid: int) -> bool:
    """No Windows, os.kill(pid, 0) manda CTRL_C em vez de sondar: pergunta ao kernel. Sem
    processo com o pid, morto; com handle, morto se já tem código de saída (o par do zumbi);
    acesso negado ou dúvida, vivo."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, wintypes.LPDWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == ERROR_INVALID_PARAMETER
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value != STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _claim(path: Path, seen: bytes) -> bool:
    """Tira o lock órfão do caminho por rename atômico. Se o que saiu não é o lock julgado
    (outro deploy já o assumiu e criou o seu), devolve-o e desiste."""
    moved = path.with_name(f"{path.name}.{os.getpid()}.orphan")
    try:
        os.rename(path, moved)
    except FileNotFoundError:
        return False
    if moved.read_bytes() == seen:
        moved.unlink()
        return True
    os.replace(moved, path)
    return False


def _busy(path: Path) -> str:
    try:
        holder = json.loads(path.read_text(encoding="utf-8"))
        since = datetime.fromisoformat(holder["at"])
        who = f"pid {holder['pid']} em {holder['host']} desde {holder['at']}"
    except (OSError, ValueError, KeyError, TypeError):
        return f"{path} existe e está ilegível; confira e use --force-unlock"
    age = (datetime.now(UTC) - since).total_seconds()
    advice = (
        "parece velho: se ninguém está fazendo deploy, use --force-unlock"
        if age > STALE_AFTER_SECONDS
        else "espere o outro deploy terminar"
    )
    return f"deploy em andamento ({who}); {advice}"
