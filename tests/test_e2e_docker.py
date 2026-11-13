"""E2E com docker de verdade: bundle de brinquedo, dois deploys, uma release que morre ao
subir, os rollbacks, a limpeza das imagens construídas, plan sem mudança, um storage volume e um
job de gatilho deploy que roda dentro do contêiner do serviço.

Pula quando não há daemon docker acessível (ex.: Docker Desktop desligado).
"""

from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from forge.adapters.compose import BLIND
from forge.commands.destroy import destroy
from forge.commands.plan import plan_lines
from forge.deploy import DeployFailedError, deploy, deploy_and_recover, rollback
from forge.executor import LocalExecutor
from forge.load import load
from forge.state import read_state
from tests.support import Writer


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    done = subprocess.run(
        ["docker", "info"], capture_output=True, timeout=30, check=False
    )
    return done.returncode == 0


pytestmark = pytest.mark.skipif(not docker_available(), reason="docker indisponível")

DOCKERFILE = """FROM python:3.12-alpine
ARG GREETING=oi
RUN echo "$GREETING" > /greeting
CMD ["python", "-m", "http.server", "8000", "--directory", "/"]
"""
DIES = 'CMD ["python", "-c", "raise SystemExit(1)"]\n'
COMPOSE = """services:
  web:
    image: ${APP_IMAGE}
    ports:
      - "${WEB_PORT}:8000"
  reader:
    image: ${APP_IMAGE}
    env_file: ${FORGE_ENV_FILE}
    volumes:
      - data:/data
volumes:
  data:
    external: true
    name: forge-e2e-toy-dev_data
"""
SEED = "open('/data/seed', 'w').write('ok')"
CHECK = "assert open('/data/seed').read() == 'ok'"
VOLUME = "forge-e2e-toy-dev_data"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def toy(write_bundle: Writer, greeting: str, *, dies: bool = False) -> Path:
    manifest = {
        "forge": 2,
        "bundle": {"name": "forge-e2e-toy"},
        "include": ["resources/*.yaml"],
        "env": {"WEB_PORT": str(free_port()), "TOY_TOKEN": "secret://TOY_TOKEN"},
        "targets": {
            "dev": {
                "mode": "development",
                "default": True,
                "secrets": {"provider": "env"},
            }
        },
        "artifacts": {
            "app": {"env": "APP_IMAGE", "build": {"args": {"GREETING": greeting}}}
        },
    }
    return write_bundle(
        manifest,
        {
            "Dockerfile": DOCKERFILE + (DIES if dies else ""),
            "compose.yaml": COMPOSE,
            ".gitignore": ".forge/\n",
            "resources/web.service.yaml": {
                "compose": ["compose.yaml"],
                "release_services": ["web"],
                "health": {
                    "url": "http://localhost:${WEB_PORT}/greeting",
                    "timeout": 15 if dies else 60,
                },
            },
            "resources/data.storage.yaml": {"kind": "volume"},
            "resources/seed.job.yaml": {
                "trigger": {"deploy": {"when": "before_up"}},
                "tasks": [
                    {"key": "seed", "service": "reader", "run": ["python", "-c", SEED]}
                ],
            },
            "resources/check.job.yaml": {
                "trigger": {"deploy": {"when": "after_up"}},
                "tasks": [
                    {
                        "key": "check",
                        "service": "reader",
                        "run": ["python", "-c", CHECK],
                    }
                ],
            },
        },
    )


def test_two_deploys_rollback_and_quiet_plan(
    write_bundle: Writer, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = toy(write_bundle, "um")
    monkeypatch.setenv("TOY_TOKEN", "segredo-de-brinquedo")
    try:
        lines: list[str] = []
        first = deploy(
            load(root, release="r1", environ={}), LocalExecutor(), report=lines.append
        )
        assert f"storage data (volume {VOLUME}): criado" in lines
        assert "job seed: task seed ok (before_up)" in lines
        assert "job check: task check ok (after_up)" in lines
        toy(write_bundle, "dois")
        second = deploy(
            load(root, release="r2", environ={}), LocalExecutor(), report=print
        )
        assert first.digests["app"] != second.digests["app"]
        toy(write_bundle, "tres", dies=True)
        with pytest.raises(DeployFailedError, match="forge rollback -t dev"):
            deploy_and_recover(
                load(root, release="r3", environ={}),
                LocalExecutor(),
                reload=lambda r: load(root, release=r, environ={}),
                report=print,
            )
        toy(write_bundle, "dois")
        lines = []
        again = rollback(
            load(root, release="r4", environ={}),
            LocalExecutor(),
            reload=lambda r: load(root, release=r, environ={}),
            report=lines.append,
        )
        assert again.id == "r2" and again.digests == second.digests
        assert "imagem forge-e2e-toy-app:r3: removida" in lines
        back = rollback(
            load(root, release="r3", environ={}),
            LocalExecutor(),
            reload=lambda r: load(root, release=r, environ={}),
            report=print,
        )
        assert back.id == "r1" and back.digests == first.digests
        assert read_state(root / ".forge" / "state", "dev").current == "r1"
        monkeypatch.delenv("TOY_TOKEN")
        lines = plan_lines(load(root, release="r1", environ={}), LocalExecutor())
        assert "  web: sem mudança" in lines
        assert f"  reader: {BLIND}" in lines
        assert f"storage data (volume {VOLUME}): existe" in lines
        monkeypatch.setenv("TOY_TOKEN", "segredo-de-brinquedo")
        destroy(load(root, release="r1", environ={}), LocalExecutor(), data=True)
        gone = subprocess.run(
            ["docker", "volume", "inspect", VOLUME], capture_output=True, check=False
        )
        assert gone.returncode != 0
        images = subprocess.run(
            ["docker", "image", "ls", "-q", "forge-e2e-toy-app"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert images.stdout.strip() == ""
    finally:
        subprocess.run(
            ["docker", "compose", "-p", "forge-e2e-toy-dev", "down", "-v"],
            cwd=root,
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["docker", "image", "rm", *(f"forge-e2e-toy-app:r{n}" for n in (1, 2, 3))],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["docker", "volume", "rm", VOLUME], capture_output=True, check=False
        )
