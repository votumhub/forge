"""O forge se instala como biblioteca: o wheel roda num venv limpo, sem a pasta do repo.

Constroi o wheel, instala num venv novo e roda os verbos num projeto gerado pelo init.
Nada aqui enxerga a arvore do repositorio do forge, que e o ponto: um projeto vizinho
declara o forge no grupo de dependencias proprio e roda o verbo de dentro do venv dele.
Pula quando nao ha uv no PATH.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from forge.scaffold import ARCHETYPES, RECIPES

REPO = Path(__file__).resolve().parents[1]
ROOM = REPO.parent.parent / (REPO.name + "-room")
UV = shutil.which("uv")
pytestmark = pytest.mark.skipif(UV is None, reason="uv indisponivel")

VENV_BIN = "Scripts" if sys.platform == "win32" else "bin"
EXE = ".exe" if sys.platform == "win32" else ""
"""O venv do Windows guarda os executaveis em Scripts/ e com .exe; o do Linux, em bin/."""

BUILD_TIMEOUT = 300
INSTALL_TIMEOUT = 300
RUN_TIMEOUT = 60


def build_wheel(tmp_path: Path) -> Path:
    out = tmp_path / "dist"
    made = subprocess.run(
        [str(UV), "build", "--wheel", "--out-dir", str(out)],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=BUILD_TIMEOUT,
    )
    assert made.returncode == 0, made.stdout + made.stderr
    wheels = sorted(out.glob("*.whl"))
    assert len(wheels) == 1, f"esperava um wheel, veio {wheels}"
    return wheels[0]


def clean_python(tmp_path: Path) -> Path:
    place = ROOM / tmp_path.name / "clean"
    shutil.rmtree(place, ignore_errors=True)
    place.mkdir(parents=True)
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    made = subprocess.run(
        [str(UV), "venv", "--python", version, ".venv"],
        cwd=place,
        capture_output=True,
        text=True,
        check=False,
        timeout=INSTALL_TIMEOUT,
    )
    assert made.returncode == 0, made.stdout + made.stderr
    installed = subprocess.run(
        [
            str(UV),
            "pip",
            "install",
            "--python",
            str(venv_python(place)),
            str(build_wheel(tmp_path)),
        ],
        cwd=place,
        capture_output=True,
        text=True,
        check=False,
        timeout=INSTALL_TIMEOUT,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    return venv_python(place)


def venv_python(place: Path) -> Path:
    return place / ".venv" / VENV_BIN / f"python{EXE}"


def test_wheel_carries_recipes_and_archetypes(tmp_path: Path) -> None:
    with zipfile.ZipFile(build_wheel(tmp_path)) as archive:
        names = archive.namelist()
    recipes = {p.name for p in RECIPES.iterdir() if (p / "recipe.yaml").is_file()}
    archetypes = {
        p.name for p in ARCHETYPES.iterdir() if (p / "archetype.yaml").is_file()
    }
    carried = {
        name.split("recipes/")[1].split("/")[0] for name in names if "recipes/" in name
    }
    also = {
        name.split("archetypes/")[1].split("/")[0]
        for name in names
        if "archetypes/" in name
    }
    assert carried == recipes
    assert also == archetypes | {"_base"}
    assert any(name.endswith("archetypes/_base/files/FORGE.md.j2") for name in names)


def test_wheel_declares_the_console_script(tmp_path: Path) -> None:
    with zipfile.ZipFile(build_wheel(tmp_path)) as archive:
        entries = [
            name for name in archive.namelist() if name.endswith("entry_points.txt")
        ]
        assert entries, "o wheel nao declara entry points"
        body = archive.read(entries[0]).decode("utf-8")
    assert "forge = forge.cli:main" in body


def test_clean_install_inits_validates_and_lists_recipes(tmp_path: Path) -> None:
    python = clean_python(tmp_path)
    project = tmp_path / "vizinho"
    project.mkdir()

    def forge(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(python), "-m", "forge", "--root", str(project), *argv],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=RUN_TIMEOUT,
        )

    made = forge("init", "app", "--no-input")
    assert made.returncode == 0, made.stdout + made.stderr
    validated = forge("validate")
    assert validated.returncode == 0, validated.stdout + validated.stderr
    assert "POL015" in validated.stdout, validated.stdout + validated.stderr
    catalog = forge("recipes")
    assert catalog.returncode == 0, catalog.stdout + catalog.stderr
    assert "receita(s)" in catalog.stdout


def test_clean_install_reads_the_catalog_from_the_wheel(tmp_path: Path) -> None:
    python = clean_python(tmp_path)
    catalog = subprocess.run(
        [str(python.parent / f"forge{EXE}"), "recipes"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=RUN_TIMEOUT,
    )
    assert catalog.returncode == 0, catalog.stdout + catalog.stderr
    assert "postgres" in catalog.stdout
    assert not (tmp_path / "forge").exists()
