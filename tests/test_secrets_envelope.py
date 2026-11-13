from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from forge.envelope import EnvelopeError, build_envelope, envelope_for, write_envelope
from forge.load import load
from forge.secrets import (
    DotenvProvider,
    EnvProvider,
    ExecProvider,
    SecretsError,
    parse_dotenv,
)
from tests.support import Writer, manifest

WINDOWS = sys.platform == "win32"


def exec_provider(
    tmp_path: Path, script: str, timeout: float = 10, **passthrough: str
) -> ExecProvider:
    path = tmp_path / "provider.py"
    path.write_text(script, encoding="utf-8")
    return ExecProvider(
        command=(sys.executable, str(path)),
        target="dev",
        cwd=tmp_path,
        timeout=timeout,
        passthrough=passthrough,
    )


ECHO = """
import json, os, sys
print(json.dumps({name: os.environ["FORGE_TARGET"] + ":" + name for name in sys.argv[1:]}))
"""


def test_exec_receives_target_and_names(tmp_path: Path) -> None:
    assert exec_provider(tmp_path, ECHO).fetch(["A", "B"]) == {
        "A": "dev:A",
        "B": "dev:B",
    }


def test_exec_invalid_json_never_shows_stdout(tmp_path: Path) -> None:
    provider = exec_provider(tmp_path, "print('hunter2-not-json')")
    with pytest.raises(SecretsError, match="não é um objeto JSON") as caught:
        provider.fetch(["A"])
    assert "hunter2" not in str(caught.value)


def test_exec_non_string_values_refused(tmp_path: Path) -> None:
    provider = exec_provider(tmp_path, "print('{\"A\": 1}')")
    with pytest.raises(SecretsError, match="não é um objeto JSON"):
        provider.fetch(["A"])


def test_exec_nonzero_exit_masks_stderr_and_hides_stdout(tmp_path: Path) -> None:
    script = """
import os, sys
print('{"A": "stdout-secret"}')
sys.stderr.write("token " + os.environ["TOKEN"] + " echoed stdout-secret\\n")
sys.exit(3)
"""
    provider = exec_provider(tmp_path, script, TOKEN="tok-value-123")
    with pytest.raises(SecretsError, match="código 3") as caught:
        provider.fetch(["A"])
    message = str(caught.value)
    assert "tok-value-123" not in message
    assert "stdout-secret" not in message
    assert "token ***" in message


def test_exec_timeout(tmp_path: Path) -> None:
    provider = exec_provider(tmp_path, "import time; time.sleep(5)", timeout=0.5)
    with pytest.raises(SecretsError, match="passou de 0.5s"):
        provider.fetch(["A"])


def test_exec_newline_refused(tmp_path: Path) -> None:
    provider = exec_provider(tmp_path, "import json; print(json.dumps({'A': 'x\\ny'}))")
    with pytest.raises(SecretsError, match="quebra de linha"):
        provider.fetch(["A"])


def test_exec_missing_name_refused(tmp_path: Path) -> None:
    with pytest.raises(SecretsError, match="não tem B"):
        exec_provider(tmp_path, 'print(\'{"A": "1"}\')').fetch(["A", "B"])


def test_env_and_dotenv_providers(tmp_path: Path) -> None:
    assert EnvProvider({"A": "1"}).fetch(["A"]) == {"A": "1"}
    with pytest.raises(SecretsError, match="não tem B"):
        EnvProvider({}).fetch(["B"])
    with pytest.raises(
        SecretsError, match="não existe: crie-o, fora do git, com uma linha"
    ):
        DotenvProvider(tmp_path / "falta.env").fetch(["A"])
    dotenv = tmp_path / ".env"
    dotenv.write_text("# c\nexport A='um'\nB=\"dois\"\n\nC=3=3\n", encoding="utf-8")
    assert DotenvProvider(dotenv).fetch(["A", "B", "C"]) == {
        "A": "um",
        "B": "dois",
        "C": "3=3",
    }
    assert parse_dotenv("X") == {}


def envelope_bundle(
    write_bundle: Writer, env: dict[str, str], secrets: dict[str, object] | None
) -> Path:
    data = manifest(env=env)
    if secrets is not None:
        data["targets"]["dev"]["secrets"] = secrets  # type: ignore[index]
    return write_bundle(data)


def test_envelope_resolves_secrets_and_forge_values(write_bundle: Writer) -> None:
    root = envelope_bundle(
        write_bundle, {"DB": "secret://DB_PASS", "LEVEL": "info"}, {"provider": "env"}
    )
    loaded = load(root, environ={}, release="r1")
    made = envelope_for(
        loaded, extra={"APP_IMAGE": "img@sha256:1"}, environ={"DB_PASS": "pw"}
    )
    assert made.values["DB"] == "pw"
    assert made.values["LEVEL"] == "info"
    assert made.values["APP_IMAGE"] == "img@sha256:1"
    assert made.values["FORGE_TARGET"] == "dev"
    assert made.values["FORGE_RELEASE"] == "r1"
    assert made.values["FORGE_ENV_FILE"].endswith(".forge/env/dev.env")
    assert {"FORGE_ROOT", "FORGE_STATE_PATH"} <= set(made.values)
    assert made.secret_values() == ["pw"]


def test_envelope_collision_literal_and_secret(write_bundle: Writer) -> None:
    root = envelope_bundle(
        write_bundle, {"DB_PASS": "x", "DB": "secret://DB_PASS"}, {"provider": "env"}
    )
    with pytest.raises(EnvelopeError, match="literal e também nome de segredo"):
        envelope_for(load(root, environ={}), environ={"DB_PASS": "pw"})


def test_envelope_reserved_prefix_refused(write_bundle: Writer) -> None:
    root = envelope_bundle(write_bundle, {"FORGE_TARGET": "x"}, None)
    with pytest.raises(EnvelopeError, match="prefixo reservado"):
        build_envelope(load(root, environ={}), None)


def test_envelope_secret_without_provider_refused(write_bundle: Writer) -> None:
    root = envelope_bundle(write_bundle, {"DB": "secret://DB_PASS"}, None)
    with pytest.raises(EnvelopeError, match="não declara secrets"):
        build_envelope(load(root, environ={}), None)


def test_envelope_body_refuses_newline(write_bundle: Writer) -> None:
    root = envelope_bundle(write_bundle, {"A": "x"}, None)
    made = build_envelope(load(root, environ={}), None, extra={"B": "1\n2"})
    with pytest.raises(EnvelopeError, match="quebra de linha"):
        made.body()


def test_write_envelope_atomic_and_sorted(write_bundle: Writer, tmp_path: Path) -> None:
    root = envelope_bundle(write_bundle, {"B": "2", "A": "1"}, None)
    loaded = load(root, environ={})
    target = write_envelope(build_envelope(loaded, None), loaded.env_file)
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["A=1", "B=2"]
    assert [p.name for p in target.parent.iterdir()] == ["dev.env"]


@pytest.mark.skipif(WINDOWS, reason="permissão POSIX não existe no Windows")
def test_write_envelope_mode_0600(write_bundle: Writer) -> None:
    root = envelope_bundle(write_bundle, {"A": "1"}, None)
    loaded = load(root, environ={})
    target = write_envelope(build_envelope(loaded, None), loaded.env_file)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
