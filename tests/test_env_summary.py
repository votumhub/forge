from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from forge.cli import main
from forge.state import Release, record
from tests.support import Writer, manifest

REPO = Path(__file__).resolve().parents[1]


def secret_bundle(write_bundle: Writer) -> Path:
    data = manifest(
        env={"DB": "secret://DB_PASS", "LEVEL": "info"},
        artifacts={"app": {"env": "APP_IMAGE", "pull": "ghcr.io/o/app:1"}},
    )
    data["targets"]["dev"]["secrets"] = {"provider": "dotenv", "path": "secrets.env"}  # type: ignore[index]
    return write_bundle(
        data,
        {
            "secrets.env": "DB_PASS=pw-very-secret\n",
            ".gitignore": ".forge/\nsecrets.env\n",
        },
    )


def forge(root: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "forge", "--root", str(root), *argv],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_env_runs_command_inside_envelope(write_bundle: Writer) -> None:
    root = secret_bundle(write_bundle)
    done = forge(
        root,
        "env",
        "-t",
        "dev",
        "--",
        sys.executable,
        "-c",
        "import os;print(os.environ['FORGE_TARGET'], os.environ['DB'])",
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "dev pw-very-secret"


def test_env_returns_child_exit_code(write_bundle: Writer) -> None:
    root = secret_bundle(write_bundle)
    done = forge(
        root, "env", "-t", "dev", "--", sys.executable, "-c", "raise SystemExit(7)"
    )
    assert done.returncode == 7


def test_env_command_the_system_cannot_run_ends_in_one_line(
    write_bundle: Writer, capsys: pytest.CaptureFixture[str]
) -> None:
    root = secret_bundle(write_bundle)
    text = root / "notas.txt"
    text.write_text("isto não é um programa\n", encoding="utf-8")
    cases = {
        "comando-que-nao-existe-no-path": "não encontrado no PATH",
        str(text): "não conseguiu executar",
    }
    for command, expected in cases.items():
        code = main(["--root", str(root), "env", "-t", "dev", "--", command])
        assert code != 0
        assert expected in capsys.readouterr().err


def test_env_of_production_refuses_without_yes_and_runs_nothing(
    write_bundle: Writer, capsys: pytest.CaptureFixture[str]
) -> None:
    root = secret_bundle(write_bundle)
    code = main(
        [
            "--root",
            str(root),
            "env",
            "-t",
            "prd",
            "--",
            sys.executable,
            "-c",
            "print('rodou')",
        ]
    )
    out, err = capsys.readouterr()
    assert code != 0
    assert "production" in err and "--yes" in err
    assert "rodou" not in out


def test_env_of_production_names_need_no_confirmation(write_bundle: Writer) -> None:
    root = secret_bundle(write_bundle)
    assert forge(root, "env", "-t", "prd", "--names").returncode == 0


def test_env_names_without_values(
    write_bundle: Writer, capsys: pytest.CaptureFixture[str]
) -> None:
    root = secret_bundle(write_bundle)
    assert main(["--root", str(root), "env", "-t", "dev", "--names"]) == 0
    out = capsys.readouterr().out
    assert "DB (segredo)" in out and "FORGE_TARGET" in out and "LEVEL" in out
    assert "pw-very-secret" not in out and "info" not in out


def test_env_output_writes_file_with_deployed_image(
    write_bundle: Writer, tmp_path: Path
) -> None:
    root = secret_bundle(write_bundle)
    record(
        root / ".forge" / "state",
        "dev",
        Release(
            id="r1",
            git_sha=None,
            at="2026-01-01T00:00:00+00:00",
            status="ok",
            digests={"app": "ghcr.io/o/app@sha256:1"},
        ),
    )
    target = tmp_path / "out" / "x.env"
    assert main(["--root", str(root), "env", "-t", "dev", "--output", str(target)]) == 0
    body = target.read_text(encoding="utf-8")
    assert "APP_IMAGE=ghcr.io/o/app@sha256:1\n" in body
    assert "DB=pw-very-secret\n" in body


def test_summary_json_is_static_and_secret_free(
    write_bundle: Writer, capsys: pytest.CaptureFixture[str]
) -> None:
    root = secret_bundle(write_bundle)
    (root / "docs").mkdir()
    (root / "docs" / "debito-tecnico.md").write_text(
        "## POL002 - token literal\n- regra: r\n- motivo: m\n- mitigacao: x\n"
        "- gatilho: 2000-01-01\n",
        encoding="utf-8",
    )
    (root / "secrets.env").unlink()
    assert main(["--root", str(root), "summary", "-t", "dev", "-o", "json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["env"]["DB"] == "secret://DB_PASS"
    assert data["debt"] == [
        {
            "rule": "POL002",
            "title": "token literal",
            "trigger": "2000-01-01",
            "overdue": True,
        }
    ]
    assert "pw-very-secret" not in out


def test_summary_text(write_bundle: Writer, capsys: pytest.CaptureFixture[str]) -> None:
    root = secret_bundle(write_bundle)
    assert main(["--root", str(root), "summary", "-t", "dev"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("toy -> dev (development)")
    assert "pw-very-secret" not in out


def test_summary_lists_the_four_types(
    write_bundle: Writer, capsys: pytest.CaptureFixture[str]
) -> None:
    data = manifest(include=["resources/*.yaml"])
    data["targets"]["dev"]["proxy"] = {
        "kind": "caddy",
        "config_dir": "p",  # type: ignore[index]
        "reload": ["caddy", "reload"],
    }
    root = write_bundle(
        data,
        {
            "resources/web.service.yaml": {"compose": ["compose.yaml"]},
            "resources/data.storage.yaml": {"kind": "volume"},
            "resources/site.route.yaml": {"service": "web", "port": 80, "host": "a.b"},
            "resources/setup.job.yaml": {
                "trigger": {"deploy": {"when": "before_up"}},
                "tasks": [{"key": "m", "run": ["m"]}],
            },
        },
    )
    assert main(["--root", str(root), "summary", "-t", "dev", "-o", "json"]) == 0
    data_out = json.loads(capsys.readouterr().out)
    assert data_out["storages"] == {"data": {"kind": "volume"}}
    assert data_out["routes"]["site"]["host"] == "a.b" and data_out["proxy"] == "caddy"
    assert data_out["jobs"]["setup"] == {
        "trigger": {"deploy": {"when": "before_up"}},
        "tasks": ["m"],
    }
    assert main(["--root", str(root), "summary", "-t", "dev"]) == 0
    out = capsys.readouterr().out
    assert "proxy caddy" in out and "storages: 1" in out and "routes: 1" in out
