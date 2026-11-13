"""Storage: onde o dado mora. Volume o forge cria; bucket e database, o `ensure` do projeto."""

from __future__ import annotations

from forge.contracts import DeployContext
from forge.errors import ForgeError
from forge.executor import require_ok
from forge.model import Storage

MANAGED = "gerido pelo ensure"


class StorageError(ForgeError):
    pass


def volume_name(ctx: DeployContext, name: str, storage: Storage) -> str:
    return storage.name or f"{ctx.project}_{name}"


class StorageAdapter:
    def ensure(self, ctx: DeployContext, name: str, storage: Storage) -> str:
        """Cria se não existir; devolve a linha do relatório."""
        if storage.kind != "volume":
            argv = list(storage.ensure or [])
            require_ok(
                ctx.executor.run(argv, cwd=ctx.root, env=ctx.env), f"ensure do {name}"
            )
            return f"storage {name} ({storage.kind}): ensure ok"
        volume = volume_name(ctx, name, storage)
        if self._exists(ctx, volume):
            return f"storage {name} (volume {volume}): já existe"
        require_ok(
            ctx.executor.run(["docker", "volume", "create", volume], cwd=ctx.root),
            f"criação do volume {volume}",
        )
        return f"storage {name} (volume {volume}): criado"

    def plan(self, ctx: DeployContext, name: str, storage: Storage) -> list[str]:
        if storage.kind != "volume":
            return [f"storage {name} ({storage.kind}): {MANAGED}"]
        volume = volume_name(ctx, name, storage)
        state = "existe" if self._exists(ctx, volume) else "ausente (o deploy cria)"
        return [f"storage {name} (volume {volume}): {state}"]

    def destroy(self, ctx: DeployContext, name: str, storage: Storage) -> str:
        """Só volume é removido; bucket e database não têm comando de remoção no contrato."""
        if storage.kind != "volume":
            return (
                f"storage {name} ({storage.kind}): mantido (o forge só remove volume)"
            )
        volume = volume_name(ctx, name, storage)
        require_ok(
            ctx.executor.run(["docker", "volume", "rm", volume], cwd=ctx.root),
            f"remoção do volume {volume}",
        )
        return f"storage {name} (volume {volume}): removido"

    def restore(self, ctx: DeployContext, name: str, storage: Storage) -> str:
        if storage.backup is None:
            raise StorageError(
                f"storage {name} não declara backup: não há como restaurar"
            )
        result = ctx.executor.run(storage.backup.restore, cwd=ctx.root, env=ctx.env)
        return require_ok(result, f"restore do storage {name}").output

    def _exists(self, ctx: DeployContext, volume: str) -> bool:
        return ctx.executor.run(
            ["docker", "volume", "inspect", volume], cwd=ctx.root
        ).ok
