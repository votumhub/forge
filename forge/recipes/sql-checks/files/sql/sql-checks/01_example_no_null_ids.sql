-- Receita sql-checks (quality) - checagem de exemplo; um arquivo .sql por regra nesta pasta.
-- Como conecta: src/<pacote>/quality/sql_checks.py roda cada *.sql daqui no DuckDB.
-- Variáveis: nenhuma; a pasta vem de SQL_CHECKS_DIR (padrão sql/sql-checks).
-- dev x prd: o mesmo SQL nos dois alvos; muda o banco (DUCKDB_DATABASE) pelo env do alvo.
-- Regra: selecione as linhas QUE VIOLAM a regra; zero linhas = passou. Prefixo numérico
--   (01_, 02_) define a ordem. Troque a tabela de exemplo pela real.
-- Documentação: https://duckdb.org/docs/stable/sql/query_syntax/where (duckdb 1.3)
select *
from (select 1 as id) as orders
where id is null
