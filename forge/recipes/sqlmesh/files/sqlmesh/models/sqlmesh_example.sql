-- Receita sqlmesh (transform) - modelo de exemplo; um arquivo por modelo em sqlmesh/models/.
-- Como conecta: o SQLMesh lê este MODEL e materializa no DuckDB do gateway do alvo.
-- Variáveis: nenhuma; o banco vem de SQLMESH_DATABASE no env do alvo.
-- dev x prd: o mesmo SQL nos dois alvos; `plan dev` cria o ambiente virtual de teste sem
--   tocar nas tabelas de produção do mesmo gateway.
-- Documentação: https://sqlmesh.readthedocs.io/en/stable/concepts/models/overview/ (sqlmesh 0.236)
MODEL (
  name staging.sqlmesh_example,
  kind FULL,
  grain id
);

select 1 as id, current_date as loaded_on
