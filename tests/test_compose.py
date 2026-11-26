from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge.adapters.compose import ComposeAdapter, HealthError, expand, parse_rows
from forge.contracts import DeployContext
from forge.model import Health, Service
from forge.testing import FakeExecutor

COMPOSE = """services:
  web: {image: "${APP_IMAGE}"}
  db: {image: postgres}
networks:
  edge:
    external: true
    name: ${EDGE_NET:-edge}
  shared:
    external: true
  internal: {}
"""


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def context(tmp_path: Path, fake: FakeExecutor) -> DeployContext:
    (tmp_path / "compose.yaml").write_text(COMPOSE, encoding="utf-8")
    return DeployContext(
        root=tmp_path,
        target="dev",
        mode="development",
        release="r1",
        project="toy-dev",
        env={"APP_PORT": "8080", "FORGE_TARGET": "dev"},
        env_file=tmp_path / ".forge/env/dev.env",
        build_dir=tmp_path / "build",
        executor=fake,
    )


def base(tmp_path: Path) -> tuple[str, ...]:
    env_file = (tmp_path / ".forge/env/dev.env").as_posix()
    return (
        "docker",
        "compose",
        "-p",
        "toy-dev",
        "--env-file",
        env_file,
        "-f",
        "compose.yaml",
    )


def test_up_creates_missing_networks_and_recreates_release(tmp_path: Path) -> None:
    fake = FakeExecutor()
    fake.respond(["docker", "network", "inspect", "edge"], code=1, output="not found")
    ctx = context(tmp_path, fake)
    service = Service(compose=["compose.yaml"], release_services=["web"])
    ComposeAdapter().up(ctx, "app", service)
    assert fake.argvs() == [
        ("docker", "network", "inspect", "edge"),
        ("docker", "network", "create", "edge"),
        ("docker", "network", "inspect", "shared"),
        (*base(tmp_path), "up", "-d"),
        (*base(tmp_path), "up", "-d", "--no-deps", "--force-recreate", "web"),
    ]


def test_a_network_of_a_piece_included_by_target_is_created(tmp_path: Path) -> None:
    """O include com `${FORGE_TARGET:-dev}` no caminho: o `config --no-interpolate` da 2.2.0
    nao resolvia o caminho, e a entrega parou antes do up."""
    fake = FakeExecutor()
    fake.respond(["docker", "network", "inspect"], code=1, output="not found")
    ctx = context(tmp_path, fake)
    (tmp_path / "compose.yaml").write_text(
        "include:\n  - path: pecas/rede.${FORGE_TARGET:-x}.yaml\n", encoding="utf-8"
    )
    (tmp_path / "pecas").mkdir()
    (tmp_path / "pecas/rede.dev.yaml").write_text(
        "networks:\n  borda: {external: true, name: borda-publica}\n", encoding="utf-8"
    )
    ComposeAdapter().up(ctx, "app", Service(compose=["compose.yaml"]))
    assert ("docker", "network", "create", "borda-publica") in fake.argvs()


def test_every_compose_argv_has_project_and_env_file(tmp_path: Path) -> None:
    fake = FakeExecutor()
    ctx = context(tmp_path, fake)
    service = Service(compose=["compose.yaml"], project="custom")
    adapter = ComposeAdapter()
    adapter.up(ctx, "app", service)
    adapter.destroy(ctx, "app", service)
    for argv in fake.argvs():
        if argv[:2] == ("docker", "compose"):
            assert argv[2:6] == ("-p", "custom", "--env-file", ctx.env_file.as_posix())


def test_health_url_expanded_with_target_env(tmp_path: Path) -> None:
    seen: list[str] = []
    replies = iter([False, False, True])

    def probe(url: str) -> bool:
        seen.append(url)
        return next(replies)

    clock = Clock()
    adapter = ComposeAdapter(probe_url=probe, sleep=clock.sleep, clock=clock)
    health = Health(url="http://localhost:${APP_PORT}/health", timeout=30)
    adapter.health(
        context(tmp_path, FakeExecutor()),
        "app",
        Service(compose=["compose.yaml"], health=health),
    )
    assert seen == ["http://localhost:8080/health"] * 3


def test_health_times_out(tmp_path: Path) -> None:
    clock = Clock()
    adapter = ComposeAdapter(probe_url=lambda _: False, sleep=clock.sleep, clock=clock)
    service = Service(
        compose=["compose.yaml"], health=Health(url="http://x/h", timeout=5)
    )
    with pytest.raises(HealthError, match="não ficou saudável em 5s"):
        adapter.health(context(tmp_path, FakeExecutor()), "app", service)


def test_health_healthy_uses_compose_ps(tmp_path: Path) -> None:
    fake = FakeExecutor()
    rows = [
        {"Service": "web", "Health": "healthy"},
        {"Service": "db", "Health": "starting"},
    ]
    fake.respond(["docker", "compose"], output="\n".join(json.dumps(r) for r in rows))
    clock = Clock()
    adapter = ComposeAdapter(sleep=clock.sleep, clock=clock)
    ctx = context(tmp_path, fake)
    adapter.health(
        ctx, "app", Service(compose=["compose.yaml"], health=Health(healthy=["web"]))
    )
    assert fake.argvs()[0] == (*base(tmp_path), "ps", "--format", "json", "web")
    with pytest.raises(HealthError, match="healthy web, db"):
        adapter.health(
            ctx,
            "app",
            Service(
                compose=["compose.yaml"],
                health=Health(healthy=["web", "db"], timeout=3),
            ),
        )


def test_a_secret_inside_a_service_name_does_not_hide_its_health(
    tmp_path: Path,
) -> None:
    fake = FakeExecutor()
    fake.hide(["web"])
    fake.respond(
        ["docker", "compose"],
        output=json.dumps({"Service": "web", "Health": "healthy"}),
    )
    clock = Clock()
    adapter = ComposeAdapter(sleep=clock.sleep, clock=clock)
    service = Service(
        compose=["compose.yaml"], health=Health(healthy=["web"], timeout=3)
    )

    adapter.health(context(tmp_path, fake), "app", service)

    assert clock.now == 0.0


def test_health_command(tmp_path: Path) -> None:
    fake = FakeExecutor()
    fake.respond(["check"], code=0)
    ComposeAdapter().health(
        context(tmp_path, fake),
        "app",
        Service(compose=["compose.yaml"], health=Health(command=["check"])),
    )
    assert fake.argvs() == [("check",)]


def test_expand_and_rows() -> None:
    assert expand("${A}-${B:-x}", {"A": "1"}, "w") == "1-x"
    with pytest.raises(HealthError, match=r"\$\{C\} não tem valor"):
        expand("${C}", {}, "w")
    assert parse_rows('[{"Service": "a"}]') == [{"Service": "a"}]
    assert parse_rows("") == []
