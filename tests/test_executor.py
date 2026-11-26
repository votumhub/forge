from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from forge import executor
from forge.executor import LocalExecutor, StepError

PY = sys.executable


def test_output_masks_hidden_values(tmp_path: Path) -> None:
    executor = LocalExecutor()
    executor.hide(["s3cr3t-value"])
    result = executor.run([PY, "-c", "print('token=s3cr3t-value ok')"], cwd=tmp_path)
    assert result.ok
    assert result.output.strip() == "token=*** ok"


def test_the_forge_reads_the_output_unmasked(tmp_path: Path) -> None:
    executor = LocalExecutor()
    executor.hide(["s3cr3t-value"])
    script = "import os; print('token=' + os.environ['V'] + ' ok')"
    result = executor.run([PY, "-c", script], cwd=tmp_path, env={"V": "s3cr3t-value"})
    assert result.output.strip() == "token=*** ok"
    assert result.data.strip() == "token=s3cr3t-value ok"
    assert "s3cr3t-value" not in repr(result)


def test_env_is_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEAKY_PARENT_VAR", "leak")
    script = "import os; print(os.environ.get('LEAKY_PARENT_VAR'), os.environ['MINE'])"
    result = LocalExecutor().run([PY, "-c", script], cwd=tmp_path, env={"MINE": "1"})
    assert result.output.strip() == "None 1"


def test_nonzero_exit_is_reported_not_raised(tmp_path: Path) -> None:
    result = LocalExecutor().run([PY, "-c", "import sys; sys.exit(4)"], cwd=tmp_path)
    assert result.code == 4 and not result.ok


def test_timeout_raises_step_error(tmp_path: Path) -> None:
    with pytest.raises(StepError, match="passou de 0.5s"):
        LocalExecutor().run(
            [PY, "-c", "import time; time.sleep(5)"], cwd=tmp_path, timeout=0.5
        )


def test_missing_command(tmp_path: Path) -> None:
    with pytest.raises(StepError, match="não encontrado"):
        LocalExecutor().run(["definitely-not-a-command-xyz"], cwd=tmp_path)


def completed(code: int, output: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout=output)


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (FileNotFoundError(2, "No such file"), "docker não foi encontrado no PATH"),
        (
            completed(
                1,
                "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
                "Is the docker daemon running?\n",
            ),
            "daemon não respondeu",
        ),
        (
            completed(
                1,
                "error during connect: Get http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine"
                "/v1.47/info: open //./pipe/dockerDesktopLinuxEngine: The system cannot "
                "find the file specified.\n",
            ),
            "daemon não respondeu",
        ),
        (
            OSError(193, "%1 não é um aplicativo Win32 válido"),
            "não conseguiu executar",
        ),
    ],
)
def test_platform_failures_become_step_errors_with_next_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: BaseException | subprocess.CompletedProcess[str],
    expected: str,
) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(StepError, match=expected) as caught:
        LocalExecutor(["s3cr3t"]).run(["docker", "info", "s3cr3t"], cwd=tmp_path)
    assert "s3cr3t" not in str(caught.value)
    if not isinstance(outcome, OSError) or isinstance(outcome, FileNotFoundError):
        assert "`docker info`" in str(caught.value)


def test_windows_runs_the_pathext_program_not_the_shell_script_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python 3.12.0 no Windows: shutil.which devolvia o script sem extensão que mora ao
    lado do docker.cmd, e o subprocess morria em WinError 193."""
    (tmp_path / "docker").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "docker.cmd").write_text("@echo off\n", encoding="utf-8")
    started: list[str] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        started.append(argv[0])
        return completed(0, "")

    monkeypatch.setattr(executor, "WINDOWS", True)
    monkeypatch.setattr(shutil, "which", lambda name, path=None: str(tmp_path / name))
    monkeypatch.setattr(subprocess, "run", fake_run)
    env = {"PATH": str(tmp_path), "PATHEXT": ".com;.exe;.bat;.cmd"}
    LocalExecutor().run(["docker", "info"], cwd=tmp_path, env=env)
    assert started == [str(tmp_path / "docker.cmd")]
