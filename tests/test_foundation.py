import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from forge.cli import main
from forge.model import Manifest, ManifestError, parse
from forge.scaffold import FORGE_REPOSITORY
from tests.test_routes import ROUTE

REPO = Path(__file__).resolve().parents[1]
HOUSE_NAMES = re.compile("|".join(["vot" + "um", r"\bca" + r"sa\b"]), re.IGNORECASE)

MINIMAL = {
    "forge": 2,
    "bundle": {"name": "toy"},
    "targets": {"dev": {"mode": "development", "default": True}},
}


def test_minimal_manifest_parses() -> None:
    manifest = parse(Manifest, MINIMAL, "forge.yaml")
    assert manifest.targets["dev"].mode == "development"


def test_unknown_field_names_its_path() -> None:
    data = {**MINIMAL, "targets": {"dev": {"mode": "development", "colour": "blue"}}}
    with pytest.raises(ManifestError) as caught:
        parse(Manifest, data, "forge.yaml")
    assert "forge.yaml" in str(caught.value)
    assert "targets.dev.colour: campo desconhecido" in str(caught.value)


def test_unknown_top_level_field_fails() -> None:
    with pytest.raises(ManifestError, match="stack: campo desconhecido"):
        parse(Manifest, {**MINIMAL, "stack": {}}, "forge.yaml")


def test_no_house_name_in_code_or_tests() -> None:
    offenders = [
        f"{path.relative_to(REPO)}:{number}"
        for folder in ("forge", "tests")
        for path in sorted((REPO / folder).rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if HOUSE_NAMES.search(line.replace(FORGE_REPOSITORY, ""))
    ]
    assert offenders == []


def test_help_lists_every_verb() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "forge", "--help"],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert done.returncode == 0
    for verb in (
        "init",
        "validate",
        "plan",
        "deploy",
        "run",
        "destroy",
        "summary",
        "rollback",
        "env",
        "recipes",
        "seguir",
        "schema",
    ):
        assert verb in done.stdout


def test_schema_is_derived_from_the_model_and_needs_no_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["schema"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert {"forge", "bundle", "targets"} <= set(manifest["required"])
    assert main(["schema", "job"]) == 0
    assert {"trigger", "tasks"} <= set(
        json.loads(capsys.readouterr().out)["properties"]
    )
    assert main(["schema", "route"]) == 0
    out = capsys.readouterr().out
    assert out.isascii()
    route = json.loads(out)
    overlay = route["properties"]["targets"]["additionalProperties"]["anyOf"][0]
    per_target = route["$defs"][overlay["$ref"].rsplit("/", 1)[1]]
    assert set(ROUTE) <= set(route["properties"]) and not route.get("required")
    assert set(ROUTE["targets"]["prd"]) <= set(per_target["properties"])
    assert not [name for name, part in route["$defs"].items() if "required" in part]
    assert "null" in route["properties"]["targets"]["type"]
    assert main(["schema", "queue"]) == 2
    error = capsys.readouterr().err
    assert "'queue'" in error and "service, job, storage, route" in error


def test_unexpected_error_says_it_is_a_forge_defect_and_where_to_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken(_: object) -> int:
        raise KeyError("bug")

    monkeypatch.setattr("forge.commands.validate.run", broken)
    assert main(["--root", str(tmp_path), "validate"]) == 2
    error = capsys.readouterr().err
    assert "Traceback" in error
    assert "defeito do forge" in error and "/forge/issues" in error
