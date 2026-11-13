"""Routes: um arquivo por route para o proxy do alvo (caddy ou nginx), copiado e recarregado."""

from __future__ import annotations

import codecs
import shutil
from collections.abc import Mapping
from pathlib import Path

from forge.contracts import DeployContext
from forge.errors import ForgeError, read_utf8
from forge.executor import require_ok
from forge.model import Proxy, Route

HEADER = "# gerado pelo forge (bundle {bundle}, alvo {target}, route {route}); não editar à mão\n"
SUFFIX = {"caddy": ".caddy", "nginx": ".conf"}
HEADER_BYTES = 512
BOMS = (
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


class RouteError(ForgeError):
    pass


def file_name(bundle: str, target: str, route: str, proxy: Proxy) -> str:
    return f"forge-{bundle}-{target}-{route}{SUFFIX[proxy.kind]}"


def render(
    ctx: DeployContext, bundle: str, name: str, route: Route, proxy: Proxy
) -> str:
    head = HEADER.format(bundle=bundle, target=ctx.target, route=name)
    body = _caddy(route) if proxy.kind == "caddy" else _nginx(name, route)
    return head + body


def _caddy(route: Route) -> str:
    """Endereço sem esquema: o caddy emite e renova o certificado sozinho."""
    lines = [f"{route.host} {{"]
    if route.access == "private":
        lines += [
            f"\t@forge_blocked not remote_ip {' '.join(route.allow)}",
            "\trespond @forge_blocked 403",
        ]
    lines += [f"\treverse_proxy {route.target_address}", "}"]
    return "\n".join(lines) + "\n"


def _nginx(name: str, route: Route) -> str:
    if route.tls is None:
        raise RouteError(f"route {name}: nginx exige tls {{cert, key}} no alvo")
    guard = [f"    allow {cidr};" for cidr in route.allow]
    guard += ["    deny all;"] if guard else []
    lines = [
        "server {", "    listen 80;", f"    server_name {route.host};",
        "    return 301 https://$host$request_uri;", "}", "server {", "    listen 443 ssl;",
        f"    server_name {route.host};", f"    ssl_certificate {route.tls.cert};",
        f"    ssl_certificate_key {route.tls.key};", *guard, "    location / {",
        f"        proxy_pass http://{route.target_address};",
        "        proxy_set_header Host $host;",
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "        proxy_set_header X-Forwarded-Proto $scheme;", "    }", "}",
    ]  # fmt: skip
    return "\n".join(lines) + "\n"


def _header(path: Path) -> str:
    """Só o começo decide se o arquivo é deste bundle: o próprio arquivo regravado com BOM ou
    em UTF-16 continua reconhecido, e o binário de terceiro vira texto que não bate com o cabeçalho."""
    with path.open("rb") as stream:
        head = stream.read(HEADER_BYTES)
    for bom, encoding in BOMS:
        if head.startswith(bom):
            return head[len(bom) :].decode(encoding, "ignore")
    return head.decode("utf-8", "ignore")


def config_dir(ctx: DeployContext, proxy: Proxy) -> Path:
    return ctx.root / proxy.config_dir


class RouteAdapter:
    def apply(
        self,
        ctx: DeployContext,
        bundle: str,
        routes: Mapping[str, Route],
        proxy: Proxy | None,
    ) -> list[str]:
        """Renderiza em .forge/build/<alvo>/routes/, copia para config_dir, tira os arquivos de
        routes que saíram do bundle e recarrega o proxy."""
        if proxy is None:
            if routes:
                raise RouteError(f"alvo {ctx.target} tem routes e não declara proxy")
            return []
        wanted = {
            file_name(bundle, ctx.target, n, proxy): render(ctx, bundle, n, r, proxy)
            for n, r in routes.items()
        }
        built = ctx.build_dir / "routes"
        built.mkdir(parents=True, exist_ok=True)
        destination = config_dir(ctx, proxy)
        destination.mkdir(parents=True, exist_ok=True)
        for file, text in wanted.items():
            (built / file).write_text(text, encoding="utf-8", newline="\n")
            shutil.copyfile(built / file, destination / file)
        removed = self._stale(ctx, destination, bundle, proxy, set(wanted))
        if not wanted and not removed:
            return []
        self._reload(ctx, proxy)
        return [
            f"route {file}: publicada em {destination.as_posix()}" for file in wanted
        ] + [f"route {file}: removida (saiu do bundle)" for file in removed]

    def remove(self, ctx: DeployContext, bundle: str, proxy: Proxy | None) -> list[str]:
        if proxy is None:
            return []
        removed = self._stale(ctx, config_dir(ctx, proxy), bundle, proxy, set())
        if removed:
            self._reload(ctx, proxy)
        return [f"route {file}: removida" for file in removed]

    def plan(
        self,
        ctx: DeployContext,
        bundle: str,
        name: str,
        route: Route,
        proxy: Proxy | None,
    ) -> list[str]:
        if proxy is None:
            return [
                f"route {name}: o alvo {ctx.target} não declara proxy (o deploy falha)"
            ]
        current = config_dir(ctx, proxy) / file_name(bundle, ctx.target, name, proxy)
        text = render(ctx, bundle, name, route, proxy)
        if not current.is_file():
            change = "criar"
        else:
            same = read_utf8(current) == text
            change = "sem mudança" if same else "atualizar"
        return [
            f"route {name}: {route.host} -> {route.target_address} "
            f"({route.access}, {proxy.kind}): {change}"
        ]

    def _stale(
        self,
        ctx: DeployContext,
        directory: Path,
        bundle: str,
        proxy: Proxy,
        keep: set[str],
    ) -> list[str]:
        """Arquivos deste bundle e alvo (pelo cabeçalho) que não estão em `keep`."""
        if not directory.is_dir():
            return []
        mine = HEADER.format(bundle=bundle, target=ctx.target, route="").split(
            ", route"
        )[0]
        pattern = f"forge-{bundle}-{ctx.target}-*{SUFFIX[proxy.kind]}"
        stale = sorted(
            p.name
            for p in directory.glob(pattern)
            if p.name not in keep and _header(p).startswith(mine)
        )
        for file in stale:
            (directory / file).unlink()
        return stale

    def _reload(self, ctx: DeployContext, proxy: Proxy) -> None:
        require_ok(
            ctx.executor.run(proxy.reload, cwd=ctx.root, env=ctx.env), "reload do proxy"
        )
