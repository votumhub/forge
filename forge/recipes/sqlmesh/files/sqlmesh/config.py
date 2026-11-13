# Receita sqlmesh (transform) - gerada por `forge init --recipe sqlmesh`.
# Como conecta: sqlmesh sobre DuckDB (pip install "sqlmesh>=0.135"); o projeto vive em
#   sqlmesh/ e roda sempre dentro do envelope, com o gateway igual ao alvo do forge:
#   python -m forge env -t dev -- sqlmesh -p sqlmesh plan dev
#   python -m forge env -t prd -- sqlmesh -p sqlmesh run
# Variáveis (env do alvo no forge.yaml): SQLMESH_DATABASE (arquivo analítico, ex.:
#   data/warehouse.duckdb), SQLMESH_STATE_DATABASE (arquivo do estado, ex.:
#   data/sqlmesh_state.duckdb). Credencial do lago (AWS_*) vem do mesmo env do engine; aqui
#   só há caminhos, nunca segredo.
# dev x prd: o gateway se chama FORGE_TARGET e os arquivos vêm do env do alvo. Em prd o
#   deploy só roda `sqlmesh run`; um `plan` que quebra modelos é decisão humana, nunca passo
#   automático.
# Regra: o estado fica em arquivo diferente do banco analítico, para a contabilidade do
#   SQLMesh nunca disputar o mesmo handle com as leituras e escritas do pipeline. Estado em
#   DuckDB serve a um único processo; com execuções concorrentes, mova o estado para Postgres.
# Documentação: https://sqlmesh.readthedocs.io/en/stable/integrations/engines/duckdb/ (sqlmesh 0.236)
from __future__ import annotations

import os

from sqlmesh.core.config import (
    Config,
    DuckDBConnectionConfig,
    GatewayConfig,
    ModelDefaultsConfig,
)


def _gateway() -> GatewayConfig:
    return GatewayConfig(
        connection=DuckDBConnectionConfig(
            database=os.environ.get("SQLMESH_DATABASE", "data/warehouse.duckdb")
        ),
        state_connection=DuckDBConnectionConfig(
            database=os.environ.get(
                "SQLMESH_STATE_DATABASE", "data/sqlmesh_state.duckdb"
            )
        ),
    )


TARGET = os.environ.get("FORGE_TARGET", "dev")

config = Config(
    default_gateway=TARGET,
    gateways={TARGET: _gateway()},
    model_defaults=ModelDefaultsConfig(dialect="duckdb"),
)
