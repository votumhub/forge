from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from forge.state import (
    LockError,
    Release,
    StateError,
    deploy_lock,
    read_state,
    record,
    state_file,
)


def release(id_: str, status: str = "ok", **digests: str) -> Release:
    return Release(
        id=id_,
        git_sha="abc",
        at="2026-01-01T00:00:00+00:00",
        status=status,  # type: ignore[arg-type]
        digests=digests,
    )


def test_record_keeps_history_and_current(tmp_path: Path) -> None:
    record(tmp_path, "dev", release("r1", app="img@sha256:1"))
    state = record(tmp_path, "dev", release("r2", status="failed"))
    assert state.current == "r1"
    assert [r.id for r in read_state(tmp_path, "dev").releases] == ["r1", "r2"]
    assert read_state(tmp_path, "dev").current_release() == release(
        "r1", app="img@sha256:1"
    )


@pytest.mark.parametrize("failed", ["r3", "r2"])
def test_rollback_after_failed_reapplies_current(tmp_path: Path, failed: str) -> None:
    """Com o mesmo id da current, a tentativa falha virava a candidata e a release atual
    (de `forge env` e `forge summary`), com as imagens dela."""
    for item in (
        release("r1", app="d1"),
        release("r2", app="d2"),
        release(failed, "failed", app="d-broken"),
    ):
        record(tmp_path, "dev", item)
    state = read_state(tmp_path, "dev")
    assert state.current == "r2"
    assert state.rollback_candidate().id == "r2"
    assert state.rollback_candidate().digests == {"app": "d2"}
    assert state.current_release() == release("r2", app="d2")


def test_rollback_without_pending_failure_picks_previous_ok(tmp_path: Path) -> None:
    for item in (
        release("r1", app="d1"),
        release("r2", app="d2"),
        release("r3", "failed"),
        release("r2", app="d2"),
    ):
        record(tmp_path, "dev", item)
    state = read_state(tmp_path, "dev")
    assert state.current == "r2"
    assert state.rollback_candidate().digests == {"app": "d1"}


def test_rollback_after_first_deploy_failed_has_nowhere_to_go(tmp_path: Path) -> None:
    record(tmp_path, "dev", release("r1", "failed"))
    with pytest.raises(StateError, match="não há release ok"):
        read_state(tmp_path, "dev").rollback_candidate()


def test_rollback_without_candidate_fails(tmp_path: Path) -> None:
    record(tmp_path, "dev", release("r1"))
    with pytest.raises(StateError, match="não há release ok(.|\n)*forge deploy -t dev"):
        read_state(tmp_path, "dev").rollback_candidate()


def test_empty_state_and_unreadable_state(tmp_path: Path) -> None:
    assert read_state(tmp_path, "dev").releases == []
    state_file(tmp_path, "dev").write_text(
        '{"target": "dev", "later": 1, "releases": [{"id": "r1", "git_sha": null, '
        '"at": "t", "status": "ok", "later": 2}]}',
        encoding="utf-8",
    )
    assert [r.id for r in read_state(tmp_path, "dev").releases] == ["r1"]
    state_file(tmp_path, "dev").write_text('{"releases": 1}', encoding="utf-8")
    with pytest.raises(StateError, match="ilegível"):
        read_state(tmp_path, "dev")


def test_concurrent_lock_is_refused_and_released(tmp_path: Path) -> None:
    with deploy_lock(tmp_path) as held:
        holder = json.loads(held.read_text(encoding="utf-8"))
        assert {"pid", "host", "at"} <= set(holder)
        with (
            pytest.raises(LockError, match="deploy em andamento"),
            deploy_lock(tmp_path),
        ):
            pass
    assert not (tmp_path / ".lock").exists()


def test_lock_released_on_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(RuntimeError), deploy_lock(tmp_path):
        raise RuntimeError("boom")
    assert not (tmp_path / ".lock").exists()

    def denied(*_: object, **__: object) -> None:
        raise PermissionError(13, "Permission denied")

    with pytest.raises(RuntimeError, match="boom"), deploy_lock(tmp_path):
        monkeypatch.setattr(Path, "unlink", denied)
        monkeypatch.setattr("tempfile.mkstemp", denied)
        with pytest.raises(StateError, match="não consegui gravar"):
            record(tmp_path, "dev", release("r1"))
        raise RuntimeError("boom")
    assert "não consegui remover o lock" in capsys.readouterr().err
    with deploy_lock(tmp_path):
        pass
    monkeypatch.undo()
    with deploy_lock(tmp_path):
        pass
    assert not (tmp_path / ".lock").exists()


def test_stale_lock_suggests_force_unlock_and_force_takes_it(tmp_path: Path) -> None:
    (tmp_path / ".lock").write_text(
        json.dumps({"pid": 1, "host": "h", "at": "2000-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    with pytest.raises(LockError, match="--force-unlock"), deploy_lock(tmp_path):
        pass
    with deploy_lock(tmp_path, force=True):
        pass


def write_lock(state_path: Path, pid: int, host: str) -> None:
    (state_path / ".lock").write_text(
        json.dumps({"pid": pid, "host": host, "at": "2000-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )


def test_lock_of_dead_holder_on_this_host_is_taken(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    write_lock(tmp_path, child.pid, socket.gethostname())
    with deploy_lock(tmp_path) as held:
        assert json.loads(held.read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert f"pid {child.pid} (morto)" in capsys.readouterr().err
    assert not (tmp_path / ".lock").exists()


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="zumbi só se lê no /proc")
def test_lock_of_zombie_holder_is_taken(tmp_path: Path) -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    stat_file = Path(f"/proc/{child.pid}/stat")
    deadline = time.monotonic() + 10
    while stat_file.read_text().rpartition(")")[2].split()[0] != "Z":
        assert time.monotonic() < deadline, "o filho não virou zumbi"
        time.sleep(0.01)
    write_lock(tmp_path, child.pid, socket.gethostname())
    with deploy_lock(tmp_path) as held:
        assert json.loads(held.read_text(encoding="utf-8"))["pid"] == os.getpid()
    child.wait()


@pytest.mark.parametrize(
    ("lock_body", "message"),
    [
        (
            lambda: {"pid": os.getpid(), "host": socket.gethostname()},
            "deploy em andamento",
        ),
        (lambda: {"pid": 2**22 + 1, "host": "outro-host"}, "deploy em andamento"),
        (lambda: "{não é json", "ilegível; confira e use --force-unlock"),
    ],
    ids=["dono-vivo", "outro-host", "ilegivel"],
)
def test_lock_not_provably_orphan_is_refused(
    tmp_path: Path, lock_body: Callable[[], object], message: str
) -> None:
    body = lock_body()
    if isinstance(body, dict):
        body = json.dumps({**body, "at": "2000-01-01T00:00:00+00:00"})
    (tmp_path / ".lock").write_text(str(body), encoding="utf-8")
    with pytest.raises(LockError, match=message), deploy_lock(tmp_path):
        pass
    assert (tmp_path / ".lock").read_text(encoding="utf-8") == body


@pytest.mark.skipif(
    sys.platform == "win32", reason="permissão POSIX não existe no Windows"
)
def test_state_file_mode_0644(tmp_path: Path) -> None:
    record(tmp_path, "dev", release("r1"))
    assert stat.S_IMODE(state_file(tmp_path, "dev").stat().st_mode) == 0o644
