-- Receita sql-files (transform) - script de exemplo da camada staging.
-- Como conecta: src/<pacote>/transform/sql_files.py aplica cada *.sql desta pasta em ordem.
-- Variáveis: nenhuma; a raiz vem de SQL_FILES_DIR e as camadas de SQL_FILES_LAYERS.
-- dev x prd: o mesmo SQL nos dois alvos; muda o banco (DUCKDB_DATABASE) pelo env do alvo.
-- Regra: idempotente sempre (CREATE OR REPLACE); troque o select pela fonte real, ex.:
--   read_parquet('s3://<bucket>/<dataset>/data=*/*.parquet').
-- Documentação: https://duckdb.org/docs/stable/sql/statements/create_table (duckdb 1.3)
create or replace table stg_example as
select 1 as id, current_date as loaded_on
