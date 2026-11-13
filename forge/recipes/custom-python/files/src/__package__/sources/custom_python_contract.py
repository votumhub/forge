# Receita custom-python (ingestion) - gerada por `forge init --recipe custom-python`.
# Como conecta: só biblioteca padrão do Python (3.12+), sem pacote extra. Cada fonte
#   implementa `Extractor`; o job (resources/<job>.job.yaml apontando para um flow Prefect ou um
#   script) chama `run_batch(nome, extractor, carregar)`.
# Variáveis (env do alvo no forge.yaml): CUSTOM_PYTHON_STATE_DIR (onde os cursores vivem, padrão
#   var/state). A connection string da fonte é outra variável do alvo, como secret://NOME,
#   lida por quem monta o extractor - nunca escrita dentro dele.
# dev x prd: cada alvo aponta CUSTOM_PYTHON_STATE_DIR para um diretório próprio no env do alvo,
#   então o cursor de dev nunca avança o de prd.
# Regras que custaram caro:
# - O cursor é lido antes de extrair e gravado só depois que o lote pousou: queda no meio do
#   lote repete o lote em vez de pulá-lo em silêncio.
# - `extract()` lançar exceção é o único sinal de falha que o retry do orquestrador enxerga.
#   Fonte sem linhas devolve iterador vazio; nunca None, nunca engole o erro.
# Documentação: https://typing.python.org/en/latest/spec/protocol.html (Python 3.12)
from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Protocol

Row = dict[str, object]


class Extractor(Protocol):
    def extract(self, cursor: str | None) -> Iterator[Row]: ...

    def next_cursor(self, rows: list[Row], cursor: str | None) -> str | None: ...


def state_dir() -> Path:
    return Path(os.environ.get("CUSTOM_PYTHON_STATE_DIR", "var/state"))


def cursor_path(source: str) -> Path:
    return state_dir() / f"{source}.cursor.json"


def read_cursor(source: str) -> str | None:
    path = cursor_path(source)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["cursor"]


def write_cursor(source: str, cursor: str) -> None:
    path = cursor_path(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"cursor": cursor}), encoding="utf-8")
    temporary.replace(path)


def run_batch(
    source: str, extractor: Extractor, land: Callable[[list[Row]], None]
) -> int:
    """Extrai desde o cursor, pousa o lote e só então avança o cursor. Devolve as linhas."""
    cursor = read_cursor(source)
    rows = list(extractor.extract(cursor))
    if not rows:
        return 0
    land(rows)
    new_cursor = extractor.next_cursor(rows, cursor)
    if new_cursor is not None:
        write_cursor(source, new_cursor)
    return len(rows)
