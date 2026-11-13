from pathlib import Path


class ForgeError(Exception):
    """Erro esperado: a mensagem vai ao usuário como está, sem traceback."""


def read_utf8(path: Path) -> str:
    """Arquivo do usuário que o forge lê: UTF-8 ou erro que diz o arquivo."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ForgeError(
            f"{path}: o arquivo não está em UTF-8 (byte inválido na posição "
            f"{error.start}); salve-o em UTF-8 (o PowerShell grava UTF-16 com `>` e "
            f"Out-File) e rode de novo"
        ) from error
