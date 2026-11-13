# Pesquisa: Databricks Asset Bundle (base do forge 2)

Resumo da pesquisa de 23/09/2026 que fundamentou o redesenho. Fontes oficiais: docs.databricks.com
(dev-tools/bundles: settings, reference, deployment-modes, variables, overrides, direct, templates,
run-as, ci-cd/github) e os repositórios `databricks/cli` e `databricks/bundle-examples`. O DAB foi
renomeado para Declarative Automation Bundles.

## O modelo do DAB

- Um `databricks.yml` na raiz; `include` puxa `resources/*.yml` (um arquivo por recurso,
  `<nome>.<tipo>.yml`; caminhos relativos ao arquivo que declara).
- Chaves de topo: `bundle`, `include`, `variables`, `targets`, `resources` (40+ tipos),
  `artifacts`, `sync`, `workspace`, `permissions`, `run_as`, `presets`, `git`, `scripts`,
  `experimental` (ganchos pre/post deploy, marcados como sujeitos a mudança).
- Alvos: um `default: true`; o alvo sobrepõe quase tudo. Mescla: mapas profundos, escalares
  substituem, listas por chave natural (`task_key`...).
- `mode: development`: prefixo `[dev usuário]`, agendas pausadas, concorrência liberada, sem lock.
  `mode: production`: bloqueia deploy se a branch não bate, se caminhos são pessoais, se falta
  `run_as`/permissões. `presets` quebram o modo em ajustes individuais.
- Variáveis: `--var` > `BUNDLE_VAR_x` > arquivo de overrides > alvo > default; `lookup` troca nome
  por ID na plataforma; substituições `${bundle.*}`, `${workspace.*}`, `${resources.*.id}`, `${var.*}`.
- Ciclo: `init` (template com schema JSON de perguntas, Go templates), `validate --strict`,
  `plan` (salvável e reaplicável), `deploy`, `run`, `destroy`, `summary`, `generate`/`bind`.
- Estado por alvo (IDs + foto da config) no workspace; diff em dois passos (config × foto,
  remoto × foto = drift); lock de deploy no workspace.
- CI/CD: workspaces separados, principal de deploy separado do de execução, OIDC no GitHub,
  PR → dev, main → prd.

## O que é da plataforma (não copiado)

Workspace e caminhos, notebooks, clusters, Unity Catalog e grants, níveis de permissão por
recurso, lookups contra API, motor Terraform, prefixo por usuário (workspace compartilhado).

## O que foi copiado (sem plataforma)

Arquivo raiz + incluídos, overlay do alvo, cadeia de precedência de variáveis, modos com checks
que bloqueiam prd, plan antes de deploy, estado por alvo, lock, identidade de execução separada,
templates de init, ganchos de ciclo de vida.

## Onde o forge diverge de propósito

- Listas do alvo substituem inteiras (mesclar por chave é caro e surpreende).
- Estado e lock num arquivo no host (`state_path`), fora do checkout.
- Credencial por provider de segredo do alvo (env, dotenv, exec), não por perfil da plataforma.
- Quatro tipos de recurso (service, job, storage, route) no lugar de 40+; gatilho de deploy é
  cidadão de primeira classe (no DAB é experimental).
- prd só por disparo aprovado sobre tag; a tag roda só `plan`.
- `rollback` e `restore` como verbos; débito técnico com gatilho; `FORGE.md` para LLM.
