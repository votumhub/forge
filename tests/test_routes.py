from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from forge.adapters.proxy import RouteError, render
from forge.commands.destroy import destroy
from forge.commands.plan import plan_lines
from forge.deploy import context, deploy
from forge.load import Loaded, load
from forge.model import ManifestError
from forge.state import read_state
from forge.testing import FakeExecutor
from tests.support import Writer, manifest

CADDY = """# gerado pelo forge (bundle toy, alvo dev, route site); não editar à mão
app.dev.local {
\t@forge_blocked not remote_ip 10.0.0.0/8 100.64.0.0/10
\trespond @forge_blocked 403
\treverse_proxy web:8000
}
"""
NGINX = """# gerado pelo forge (bundle toy, alvo prd, route site); não editar à mão
server {
    listen 80;
    server_name app.example.com;
    return 301 https://$host$request_uri;
}
server {
    listen 443 ssl;
    server_name app.example.com;
    ssl_certificate /etc/tls/app.crt;
    ssl_certificate_key /etc/tls/app.key;
    location / {
        proxy_pass http://web:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
"""
ROUTE = {
    "service": "web",
    "port": 8000,
    "host": "app.dev.local",
    "access": "private",
    "allow": ["10.0.0.0/8", "100.64.0.0/10"],
    "targets": {
        "prd": {
            "host": "app.example.com",
            "access": "public",
            "allow": [],
            "tls": {"cert": "/etc/tls/app.crt", "key": "/etc/tls/app.key"},
        }
    },
}


def bundle(write_bundle: Writer, route: dict[str, object] | None = None) -> Path:
    data = manifest(include=["resources/*.yaml"])
    data["targets"]["dev"]["proxy"] = {
        "kind": "caddy",
        "config_dir": "proxy",  # type: ignore[index]
        "reload": ["caddy", "reload"],
    }
    data["targets"]["prd"]["proxy"] = {
        "kind": "nginx",
        "config_dir": "proxy",  # type: ignore[index]
        "reload": ["nginx", "-s", "reload"],
    }
    return write_bundle(
        data,
        {
            "compose.yaml": "services:\n  web: {image: x}\n",
            "resources/web.service.yaml": {"compose": ["compose.yaml"]},
            "resources/site.route.yaml": route or ROUTE,
            "resources/daily.job.yaml": {
                "trigger": {"manual": True},
                "flow": "a.py:daily",
                "work_pool": "p",
            },
        },
    )


def dev(root: Path) -> Loaded:
    return load(root, "dev", environ={}, release="r1")


def test_route_overlay_per_target(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    assert dev(root).routes["site"].host == "app.dev.local"
    prd = load(root, "prd", environ={}, release="r1").routes["site"]
    assert (prd.host, prd.access, prd.allow) == ("app.example.com", "public", [])
    assert prd.tls is not None and prd.tls.cert == "/etc/tls/app.crt"


def test_deploy_publishes_after_health_and_before_prefect(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    fake = FakeExecutor()
    (root / "proxy").mkdir()
    (root / "proxy" / "forge-toy-dev-old.caddy").write_text(
        "# gerado pelo forge (bundle toy, alvo dev, route old); não editar à mão\n",
        encoding="utf-8",
    )
    (root / "proxy" / "forge-toy-api-dev-x.caddy").write_text(
        "# gerado pelo forge (bundle toy-api, alvo dev, route x); não editar à mão\n",
        encoding="utf-8",
    )
    lines: list[str] = []
    deploy(dev(root), fake, report=lines.append)
    assert (root / "proxy" / "forge-toy-dev-site.caddy").read_text(
        encoding="utf-8"
    ) == CADDY
    assert (
        root / ".forge" / "build" / "dev" / "routes" / "forge-toy-dev-site.caddy"
    ).is_file()
    assert not (root / "proxy" / "forge-toy-dev-old.caddy").exists()
    assert (root / "proxy" / "forge-toy-api-dev-x.caddy").exists()
    argvs = fake.argvs()
    up = next(i for i, a in enumerate(argvs) if a[-2:] == ("up", "-d"))
    reload = argvs.index(("caddy", "reload"))
    prefect = next(i for i, a in enumerate(argvs) if a[0] == "prefect")
    assert up < reload < prefect
    assert "route forge-toy-dev-old.caddy: removida (saiu do bundle)" in lines


def test_nginx_render_and_tls_required(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    prd = load(root, "prd", environ={}, release="r1")
    lines = plan_lines(prd, FakeExecutor())
    assert "route site: app.example.com -> web:8000 (public, nginx): criar" in lines
    ctx = context(prd, FakeExecutor(), {})
    assert render(ctx, "toy", "site", prd.routes["site"], prd.target.proxy) == NGINX  # type: ignore[arg-type]
    bare = prd.routes["site"].model_copy(update={"tls": None})
    with pytest.raises(RouteError, match="nginx exige tls"):
        render(ctx, "toy", "site", bare, prd.target.proxy)  # type: ignore[arg-type]


def test_plan_says_no_change_after_deploy(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    deploy(dev(root), FakeExecutor(), report=lambda _: None)
    lines = plan_lines(dev(root), FakeExecutor())
    assert (
        "route site: app.dev.local -> web:8000 (private, caddy): sem mudança" in lines
    )


def test_route_without_proxy_fails_the_deploy(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    loaded = dev(root)
    bare = loaded.target.model_copy(update={"proxy": None})
    with pytest.raises(RouteError, match="não declara proxy"):
        deploy(
            dataclasses.replace(loaded, target=bare),
            FakeExecutor(),
            report=lambda _: None,
        )
    assert read_state(root / ".forge" / "state", "dev").releases[-1].status == "failed"


def test_destroy_removes_route_files_and_reloads(write_bundle: Writer) -> None:
    root = bundle(write_bundle)
    deploy(dev(root), FakeExecutor(), report=lambda _: None)
    fake = FakeExecutor()
    done = destroy(dev(root), fake)
    assert "route forge-toy-dev-site.caddy: removida" in done
    assert not (root / "proxy" / "forge-toy-dev-site.caddy").exists()
    assert ("caddy", "reload") in fake.argvs()


@pytest.mark.parametrize(
    ("route", "message"),
    [
        (
            {"service": "web", "port": 80, "host": "a.b", "access": "private"},
            "private exige allow",
        ),
        (
            {"service": "web", "port": 80, "host": "a.b", "allow": ["10.0.0.0/8"]},
            "allow só vale para route private",
        ),
        (
            {
                "service": "web",
                "port": 80,
                "host": "a.b",
                "access": "private",
                "allow": ["x"],
            },
            "CIDR inválido: x",
        ),
        (
            {"service": "web", "port": 80, "host": "a.b", "targets": {"qa": {}}},
            "targets.qa não é alvo",
        ),
        (
            {"service": "web", "port": 80, "host": "a.b", "upstream": "http://x"},
            "upstream",
        ),
    ],
)
def test_route_shape_errors_name_the_file(
    write_bundle: Writer, route: dict[str, object], message: str
) -> None:
    root = bundle(write_bundle, route)
    with pytest.raises(
        ManifestError, match=rf"resources/site.route.yaml[\s\S]*{message}"
    ):
        dev(root)


def test_upstream_overrides_the_docker_name(write_bundle: Writer) -> None:
    route = {**ROUTE, "upstream": "127.0.0.1:18000"}
    root = bundle(write_bundle, route)
    deploy(dev(root), FakeExecutor(), report=lambda _: None)
    text = (root / "proxy" / "forge-toy-dev-site.caddy").read_text(encoding="utf-8")
    assert "\treverse_proxy 127.0.0.1:18000\n" in text
    lines = plan_lines(dev(root), FakeExecutor())
    assert (
        "route site: app.dev.local -> 127.0.0.1:18000 (private, caddy): sem mudança"
        in lines
    )


def test_other_target_files_survive_in_a_shared_config_dir(
    write_bundle: Writer,
) -> None:
    root = bundle(write_bundle)
    (root / "proxy").mkdir()
    other = root / "proxy" / "forge-toy-prd-site.caddy"
    other.write_text(
        "# gerado pelo forge (bundle toy, alvo prd, route site); não editar à mão\n",
        encoding="utf-8",
    )
    alien = root / "proxy" / "forge-toy-dev-alheio.caddy"
    alien.write_bytes(b"# escrito a mao\n\xff\xfe binario\n")
    deploy(dev(root), FakeExecutor(), report=lambda _: None)
    mine = root / "proxy" / "forge-toy-dev-site.caddy"
    mine.write_text(mine.read_text(encoding="utf-8"), encoding="utf-16")
    destroy(dev(root), FakeExecutor())
    assert other.is_file()
    assert alien.is_file()
    assert not (root / "proxy" / "forge-toy-dev-site.caddy").exists()


def test_names_are_unique_per_kind(write_bundle: Writer) -> None:
    root = bundle(write_bundle, {"service": "web", "port": 80, "host": "a.b"})
    (root / "resources" / "site.route.yaml").rename(
        root / "resources" / "web.route.yaml"
    )
    loaded = dev(root)
    assert "web" in loaded.routes and "web" in loaded.services
    assert loaded.sources[("route", "web")] == "resources/web.route.yaml"
    (root / "more").mkdir()
    (root / "more" / "web.route.yaml").write_text(
        "service: web\nport: 81\nhost: c.d\n", encoding="utf-8"
    )
    text = (root / "forge.yaml").read_text(encoding="utf-8")
    (root / "forge.yaml").write_text(
        text.replace("- resources/*.yaml", "- resources/*.yaml\n- more/*.yaml"),
        encoding="utf-8",
    )
    with pytest.raises(
        ManifestError, match="more/web.route.yaml: route web já declarado em"
    ):
        dev(root)


def test_destroy_reloads_the_proxy_before_taking_services_down(
    write_bundle: Writer,
) -> None:
    root = bundle(write_bundle)
    deploy(dev(root), FakeExecutor(), report=lambda _: None)
    fake = FakeExecutor()
    destroy(dev(root), fake)
    argvs = fake.argvs()
    down = next(
        i for i, a in enumerate(argvs) if a[:2] == ("docker", "compose") and "down" in a
    )
    assert argvs.index(("caddy", "reload")) < down
