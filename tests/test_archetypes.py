from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from forge.cli import main
from forge.load import load
from forge.scaffold import ARCHETYPES, RECIPES


def generate(tmp_path: Path, archetype: str, origin: str | None = None) -> Path:
    root = tmp_path / f"my-{archetype}"
    if origin is not None:
        root.mkdir()
        for argv in (["init", "-q"], ["remote", "add", "origin", origin]):
            subprocess.run(["git", *argv], cwd=root, check=True)
    assert main(["--root", str(root), "init", archetype, "--no-input"]) == 0
    return root


def run_generated_tests(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "archetype,passed", [("app", 3), ("pipeline", 3), ("automation", 2)]
)
def test_generated_repo_validates_and_its_tests_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], archetype: str, passed: int
) -> None:
    root = generate(tmp_path, archetype, origin="https://github.com/acme/loja.git")
    capsys.readouterr()
    assert main(["--root", str(root), "validate", "--strict"]) == 0, (
        capsys.readouterr().out
    )
    done = run_generated_tests(root)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"{passed} passed" in done.stdout


def test_pipeline_summary_names_its_job_and_worker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = generate(tmp_path, "pipeline")
    capsys.readouterr()
    assert main(["--root", str(root), "summary", "-t", "dev", "-o", "json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["jobs"]["ingest"]["deployment"] == "ingest-dev"
    assert summary["services"]["prefect"]["release_services"] == ["prefect-worker"]


def test_pipeline_worker_reads_the_envelope(tmp_path: Path) -> None:
    root = generate(tmp_path, "pipeline")
    compose = (root / "compose" / "prefect.yaml").read_text(encoding="utf-8")
    assert "env_file: ${FORGE_ENV_FILE}" in compose
    loaded = load(root, "dev", environ={}, release="r1")
    assert loaded.jobs["ingest"].work_pool == "pipeline"
    assert loaded.env["PREFECT_WORK_POOL"] == "pipeline"


def test_the_prefect_server_has_time_to_migrate_before_its_health_counts(
    tmp_path: Path,
) -> None:
    """Numa VPS de 2 vCPU a migracao passou das 12 tentativas e o worker nao subiu."""
    root = generate(tmp_path, "pipeline")
    compose = yaml.safe_load(
        (root / "compose" / "prefect.yaml").read_text(encoding="utf-8")
    )
    health = compose["services"]["prefect-server"]["healthcheck"]
    assert health["start_period"] == "120s"


def test_automation_is_a_reduced_job(tmp_path: Path) -> None:
    root = generate(tmp_path, "automation")
    assert not (root / "compose").exists()
    loaded = load(root, "dev", environ={}, release="r1")
    assert list(loaded.jobs) == ["automation"] and loaded.services == {}


IMAGE_FLOW = Path(".github") / "workflows" / "imagem.yml"
PINNED = re.compile(r"uses: ([\w./-]+)@[0-9a-f]{40} # v\d")
USES = re.compile(r"uses: ([\w./-]+)@")


def test_the_app_publishes_the_image_its_production_pulls(tmp_path: Path) -> None:
    root = generate(tmp_path, "app")
    flow = yaml.safe_load((root / IMAGE_FLOW).read_text(encoding="utf-8"))
    loaded = load(root, "prd", environ={}, release="v1.2.3")
    pulled = loaded.manifest.artifacts["app"].pull or ""
    image = f"{flow['env']['REGISTRO']}/{flow['env']['IMAGEM']}"
    assert pulled == f"{image}:v1.2.3"
    assert flow.get("on", flow.get(True))["push"]["tags"] == ["v*"]
    steps = flow["jobs"]["publicar"]["steps"]
    built = next(step for step in steps if step.get("id") == "imagem")
    assert built["with"]["platforms"] == "linux/amd64,linux/arm64"
    assert built["with"]["tags"] == (
        "${{ env.REGISTRO }}/${{ env.IMAGEM }}:${{ github.ref_name }}"
    )
    login = next(step for step in steps if "login-action" in step.get("uses", ""))
    assert login["with"]["registry"] == "${{ env.REGISTRO }}"
    assert login["with"]["password"] == "${{ secrets.GITHUB_TOKEN }}"


def test_every_generated_action_is_pinned_by_commit() -> None:
    sources = [ARCHETYPES, RECIPES]
    lines = [
        f"{path.relative_to(source.parent)}: {line.strip()}"
        for source in sources
        for path in sorted(source.rglob("*.j2"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if USES.search(line)
    ]
    loose = [line for line in lines if not PINNED.search(line)]
    assert lines and loose == []


def test_a_registry_outside_ghcr_logs_in_with_the_project_secrets(
    tmp_path: Path,
) -> None:
    config = tmp_path / "respostas.yaml"
    answers = ["name: shop", "registry: docker.io/acme", 'app_port: "8000"']
    config.write_text(chr(10).join(answers) + chr(10), encoding="utf-8")
    root = tmp_path / "shop"
    assert (
        main(
            [
                "--root",
                str(root),
                "init",
                "app",
                "--config-file",
                str(config),
                "--no-input",
            ]
        )
        == 0
    )
    flow = yaml.safe_load((root / IMAGE_FLOW).read_text(encoding="utf-8"))
    login = next(
        s
        for s in flow["jobs"]["publicar"]["steps"]
        if "login-action" in s.get("uses", "")
    )
    assert login["with"] == {
        "registry": "${{ env.REGISTRO }}",
        "username": "${{ secrets.REGISTRY_USER }}",
        "password": "${{ secrets.REGISTRY_TOKEN }}",
    }
    assert flow["env"] == {"REGISTRO": "docker.io", "IMAGEM": "acme/shop"}


def test_the_pipeline_has_no_image_to_publish(tmp_path: Path) -> None:
    assert not (generate(tmp_path, "pipeline") / IMAGE_FLOW).exists()
