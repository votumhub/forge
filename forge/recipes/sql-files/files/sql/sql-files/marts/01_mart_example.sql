-- Receita sql-files (transform) - script de exemplo da camada marts.
-- Como conecta: roda depois da camada staging (ordem de SQL_FILES_LAYERS).
-- Variáveis: nenhuma.
-- dev x prd: o mesmo SQL nos dois alvos; muda o banco pelo env do alvo.
-- Documentação: https://duckdb.org/docs/stable/sql/statements/create_table (duckdb 1.3)
create or replace table mart_example as
select loaded_on, count(*) as rows_loaded
from stg_example
group by loaded_on
