# forge: referência

Referência técnica completa do forge 3.0.2 (formato de manifesto `forge: 2`), para consulta e
para LLMs. A visão geral e o primeiro deploy estão no [README](../README.md); a operação por
agentes, em [para-agentes.md](para-agentes.md). Onde esta página e o código divergirem, o código
vence: `forge/model.py` é a fonte do formato, e `forge schema` imprime o JSON Schema derivado
dele (§13).

## 1. Estrutura de um bundle

```
projeto/
├── forge.yaml                  manifesto (obrigatório)
├── resources/<nome>.<tipo>.yaml   tipo: service | job | storage | route
├── src/, compose*.yaml, Dockerfile   código do projeto (o init gera um ponto de partida; depois é seu)
├── FORGE.md                    guia de uma página gerado pelo init
├── docs/debito-tecnico.md      débito técnico
├── .github/workflows/forge.yml CI gerado pelo init
└── .forge/                     gerado, fora do git: env/, state/, build/, secrets/
```

Nome de recurso é único por tipo (`web.service.yaml` e `web.route.yaml` convivem). `include` que
não casa com nenhum arquivo é erro.

## 2. forge.yaml

| Campo | Tipo | Padrão | Significado |
|---|---|---|---|
| `forge` | `2` | obrigatório | versão do formato |
| `bundle.name` | nome | obrigatório | nome do bundle |
| `include` | lista de globs | `[]` | arquivos de recurso, ex. `[resources/*.yaml]` |
| `variables.<nome>` | `{description, default}` | nenhum | valores para o próprio manifesto |
| `env.<NOME>` | texto | nenhum | variáveis de ambiente que o programa lê; aceita `secret://NOME` |
| `targets.<alvo>` | ver §3 | ao menos um | alvos |
| `artifacts.<nome>` | ver §7.5 | nenhum | imagens |
| `policy.strict` | bool | `false` | aviso também reprova |
| `seguir.logger` | nome de logger | nenhum | o que `run --seguir` e `seguir` mostram: o logger e os filhos; sem o bloco, tudo que não é `prefect.*` (§7.2) |
| `seguir.cabecalho` | texto de uma linha | nenhum | títulos impressos uma vez, depois de `HORA`, alinhados com as mensagens |

Campo desconhecido em qualquer nível é erro, com arquivo e caminho do campo.

## 3. Alvos

| Campo | Padrão | Significado |
|---|---|---|
| `mode` | obrigatório | `development` ou `production` (§10) |
| `default` | `false` | alvo usado sem `-t`; no máximo um |
| `project` | `<bundle>-<alvo>` | nome do projeto compose |
| `state_path` | `.forge/state` | pasta do state e do lock (§9) |
| `secrets` | nenhum | provider de segredo (§5) |
| `variables` | `{}` | valores de variáveis para este alvo |
| `env` | `{}` | env deste alvo, sobre o env comum |
| `git` | nenhum | `{branch: glob}` ou `{tag: glob}`; obrigatório em production |
| `proxy` | nenhum | `{kind: caddy\|nginx, config_dir, reload: [argv]}`; obrigatório se houver route |

Overlay: o que o alvo declara vale sobre a parte comum; mapas se somam, escalares e listas do
alvo substituem inteiros.

## 4. Variáveis e interpolação

`variables` configuram o manifesto; `env` é o que o programa lê. Precedência do valor de uma
variável, do mais forte ao mais fraco:

1. `--var nome=valor` na linha de comando
2. `FORGE_VAR_nome` no ambiente de quem roda o forge
3. `targets.<alvo>.variables.nome`
4. `variables.nome.default`

Variável não declarada, `FORGE_VAR_x` não declarada, ou sem valor no alvo: erro.

Interpolação, resolvida ao carregar:

| Referência | Valor |
|---|---|
| `${var.nome}` | valor pela precedência acima; ciclo é erro |
| `${bundle.name}`, `${bundle.target}` | nome do bundle e alvo escolhido |
| `${bundle.release}` | `--release`; senão a tag exata no HEAD; senão o sha curto (`-dirty` em development com árvore suja); sem git, `unversioned` (só development) |
| `${bundle.root}`, `${git.sha}` | raiz do bundle; commit completo |
| `${NOME}` sem ponto | passa intacto (é do compose, resolvido com o envelope) |
| `$${...}` | escape: sai `${...}` literal |

## 5. Segredos

Valor sensível é escrito `secret://NOME` em `env` (comum ou do alvo). O provider do alvo resolve
na hora; segredo que falta é erro.

| Provider | Configuração | Lê de |
|---|---|---|
| `env` | `{provider: env}` | ambiente de quem roda o forge (ex.: CI) |
| `dotenv` | `{provider: dotenv, path}` | arquivo `NOME=valor`; precisa estar fora do git (POL004) |
| `exec` | `{provider: exec, command: [argv], timeout: 30, passthrough: []}` | stdout de um comando |

Contrato do `exec`: roda sem shell; recebe os nomes pedidos como argumentos e `FORGE_TARGET` no
ambiente (mais o mínimo do sistema e o que `passthrough` liberar); devolve no stdout um objeto
JSON `{"NOME": "valor"}` só com texto; saída ≠ 0, JSON inválido ou prazo estourado é erro; o
stdout nunca aparece em mensagem; valor com quebra de linha é recusado.

## 6. Envelope

Arquivo de ambiente por alvo, `.forge/env/<alvo>.env`, modo 0600, escrito de forma atômica. Fontes:

1. `env` literal (com overlay do alvo)
2. segredos resolvidos
3. `env` de cada artifact com a imagem fixada por digest
4. `FORGE_TARGET`, `FORGE_RELEASE`, `FORGE_ENV_FILE`, `FORGE_ROOT`, `FORGE_STATE_PATH`

Recusas: nome começando com `FORGE_` declarado pelo projeto; nome que é literal e segredo ao
mesmo tempo; nome que um artifact produz declarado de novo.

`forge env -t <alvo>` escreve o envelope; `--names` lista os nomes (marcando segredos);
`-- cmd ...` roda o comando com o envelope e devolve o código de saída dele (a saída do comando
não é mascarada).

## 7. Recursos

### 7.1 service (`<nome>.service.yaml`)

| Campo | Padrão | Significado |
|---|---|---|
| `compose` | obrigatório | lista de arquivos compose do projeto |
| `project` | o do alvo | projeto compose |
| `release_services` | `[]` | serviços recriados à força a cada release |
| `health` | nenhum | `{url \| healthy: [serviços] \| command: [argv], timeout: 60}` |

Executa `docker compose -p <project> --env-file .forge/env/<alvo>.env -f <arquivos> up -d`, cria
redes externas que faltam, recria os `release_services` e espera o health (a URL é expandida com
o env do alvo). Fora do `init`, que gera um ponto de partida, o forge não gera nem mescla
arquivo compose.

### 7.2 job (`<nome>.job.yaml`)

| Campo | Significado |
|---|---|
| `trigger` | exatamente um: `schedule: {cron, timezone: UTC}`, `event: {...}`, `manual: true`, `deploy: {when: before_deploy\|before_up\|after_up}` |
| `flow` | `arquivo.py:função` (jobs de schedule, event ou manual) |
| `parameters` | parâmetros do flow |
| `work_pool` | work pool do Prefect |
| `tasks` | jobs de deploy: `[{key, run: [argv], service?, depends_on: [], timeout?}]` |

- schedule, event, manual: viram o deployment `<job>-<alvo>` em `.forge/build/<alvo>/prefect.yaml`
  e `prefect deploy`. Agenda e evento nascem desligados em development. O event vira
  `triggers: [{type: event, ..., enabled}]` (formato:
  docs.prefect.io/v3/deploy/flow-deployments/deployment-triggers). Nenhum segredo vai para o
  Prefect: as variáveis do job levam só `FORGE_TARGET`, `FORGE_RELEASE`, `FORGE_ENV_FILE`,
  `FORGE_ROOT`; o worker lê o envelope.
- deploy: executado pelo forge em ordem topológica (ciclo, dependência inexistente ou key
  repetida: erro no load). Com `service`, roda `docker compose ... run --rm <service> <argv>`
  (proibido em `before_deploy`); sem, no executor local. O env inclui `FORGE_HOOK` com o momento.
  Falha = release `failed`, com job e task na mensagem. `forge run` recusa job de deploy.
- acompanhar: `forge run <job> --seguir` dispara e acompanha a rodada que o disparo criou (o id
  sai da linha `UUID:` do `prefect deployment run`; procurar pelo deployment poderia pegar outra
  rodada da agenda). `forge seguir <job>` acompanha a rodada em curso (`PENDING`, `RUNNING`,
  `CANCELLING` ou `PAUSED`; `SCHEDULED` nunca) ou, sem ela, a última que começou; com
  `--continuo`, não para no fim: espera a próxima rodada do job e a acompanha, até Ctrl-C. Os dois leem
  a API do Prefect com o `urllib` do próprio Python, sem dependência nova: a rodada por
  `flow_runs/filter` e os registros de nível INFO para cima por `logs/filter`, página a página
  (com teto de páginas), sem repetir um registro (a identidade é o id do Prefect), com uma
  leitura a mais depois do estado final (o processo da rodada esvazia a fila de log ao sair).
  O endereço é o `PREFECT_API_URL` do envelope resolvido (nunca o `secret://` cru), com a
  autenticação do cliente do Prefect (`PREFECT_API_AUTH_STRING` ou usuário:senha na URL como
  Basic, senão `PREFECT_API_KEY` como Bearer); mensagem de erro leva a URL sem credencial. Na
  tela sai só a mensagem, `HH:MM:SS  <mensagem>`, com a hora no fuso da agenda do job (UTC sem
  agenda), dos registros de `seguir.logger`; o forge não interpreta a mensagem. O que o forge
  diz (a resposta do disparo, qual rodada, o estado final) vai para o stderr. `--timeout` põe
  um teto na espera. A tela e o CLI do Prefect (`PYTHONIOENCODING=utf-8`) escrevem em UTF-8.

### 7.3 storage (`<nome>.storage.yaml`)

| Campo | Significado |
|---|---|
| `kind` | `bucket`, `volume` ou `database` |
| `ensure` | comando idempotente que cria se faltar; obrigatório para bucket e database, proibido para volume |
| `name` | só volume: nome explícito (padrão `<project>_<nome>`) |
| `backup` | `{job: <nome de job>, restore: [argv]}` |

`ensure` roda depois do envelope, com os segredos. `destroy` nunca remove storage em production;
em development só com `--data` (e só volumes). `forge restore -t <alvo> <storage>` roda o
`restore` sob o lock (production exige `--yes`).

### 7.4 route (`<nome>.route.yaml`)

| Campo | Significado |
|---|---|
| `service`, `port` | destino (nome do serviço no compose) |
| `upstream` | opcional, `host:porta`, para proxy fora do Docker (padrão `<service>:<port>`) |
| `host` | domínio |
| `access` | `public` (padrão) ou `private` |
| `allow` | CIDRs; obrigatório em private, proibido em public |
| `tls` | `{cert, key}`; obrigatório com nginx |
| `targets.<alvo>` | sobrepõe qualquer campo por alvo |

O forge escreve `forge-<bundle>-<alvo>-<route>` em `proxy.config_dir`, roda `proxy.reload` e
remove arquivos órfãos do mesmo bundle e alvo. Caddy: HTTPS automático. nginx: redireciona
80→443.

### 7.5 artifacts (no forge.yaml)

`{env: NOME, build: {dockerfile: Dockerfile, context: ., args: {}}, pull: ref}`. Em development
usa `build` se houver; em production só `pull` (artifact só com build é POL003). A imagem é
fixada por `RepoDigests[0]` (ou `.Id` no build local) e entra no envelope pelo `env`.


A imagem que o forge constrói leva a etiqueta `<bundle>-<artifact>:<release>` (caractere fora de
`A-Za-z0-9_.-` vira `_`). Só imagem com essa etiqueta é do forge para remover (§9).

## 8. Ordem do deploy

1. lock
2. checks de production (§10); no rollback, o código montado do disco volta ao commit da
   release (§9.1)
3. jobs `before_deploy` (sem segredo)
4. artifacts (build ou pull, digest)
5. envelope (segredos)
6. storage `ensure`
7. jobs `before_up`
8. up dos services
9. health
10. routes (config do proxy + reload)
11. jobs do Prefect (`prefect deploy`)
12. jobs `after_up`
13. record `ok` no state
14. limpeza: remove as imagens construídas para releases deste alvo que o rollback de nenhum
    alvo de development alcança mais (§9)

O lock sai sempre; se a pasta não deixa apagá-lo, vira aviso e o próximo deploy deste host o
assume. Falha nos checks: nada é gravado. Falha de 3 a 12: release `failed`, a
`current` não muda. Cada comando roda sem shell, com ambiente explícito, prazo de 1800 s por
passo e saída mascarando valores secretos. Imagem que não sai na limpeza vira linha do relatório
(`imagem <etiqueta>: não removida (...)`), nunca erro.

### 8.1 Falha depois de começar a subir

Do passo 8 em diante o ar já mudou. Se o deploy falha ali:

| Alvo | O que o forge faz | Fim da mensagem |
|---|---|---|
| production | roda o próprio `rollback` (§9) e termina em erro (código 2) | `voltei sozinho para a release <id>: o serviço está de pé nela. Corrija e rode forge deploy -t <alvo> --yes de novo` |
| production, e a volta também falhou | termina em erro | `a volta automática para a release <id> também falhou: ... Confira com forge plan -t <alvo> e tente de novo com forge rollback -t <alvo> --yes` |
| development | não volta sozinho; termina em erro | `o serviço ficou na release <id>, que falhou (development não volta sozinho). Para voltar à <id>: forge rollback -t <alvo>` |
| qualquer, com o state ilegível | não volta sozinho; termina em erro | `o state do alvo não pôde ser lido (...): o forge não sabe em que release o serviço ficou nem para qual voltar, e não volta sozinho. Conserte ou restaure esse arquivo e confira com forge plan -t <alvo>` |
| qualquer, quando o state não consegue gravar o desfecho | não volta sozinho; termina em erro | `o state não registrou esta tentativa e ainda marca a release <id>, mas o que está no ar é a <id>. ... conserte a gravação de <state> (permissão, disco cheio) e rode forge deploy -t <alvo> de novo` |
| qualquer, na primeira release do alvo | não há para onde voltar | production: corrija e rode o deploy de novo; development: `forge destroy -t <alvo>` derruba o que subiu |

Toda mensagem abre com `a release <id> falhou no alvo <alvo>: <causa>`, salvo a do deploy que
subiu saudável e não se gravou: `a release <id> subiu saudável no alvo <alvo>, mas <causa>`. Falha antes do passo 8
(build, pull, segredo, `ensure`, `before_up`) não toca o que está no ar e não dispara a volta.

## 9. State, lock e rollback

`<state_path>/<alvo>.json`, escrito de forma atômica:

```json
{"target": "prd", "current": "v1.3.0",
 "releases": [{"id": "v1.3.0", "git_sha": "...", "at": "...", "status": "ok",
               "digests": {"app": "registry/app@sha256:..."}}]}
```

Ao lado, `<state_path>/<alvo>.trees.json`, escrito sob o mesmo lock e de forma atômica depois
do state, guarda como estava a árvore em cada deploy `ok`, pelo id da release (o mais recente por
último):

```json
{"v1.3.0": {"git_sha": "...", "at": "...", "dirty": false, "branch": "main"}}
```

`dirty`: a árvore tinha mudança não commitada (também quando o id é uma tag ou foi dado com
`--release`). `branch`: a branch do checkout antes da tentativa. `git_sha` e `at` amarram a
entrada ao registro do state: ela só vale para a release com o mesmo id, o mesmo `git_sha` e o
mesmo `at`. Release sem entrada que valha (gravada antes deste arquivo existir, ou por outro
deploy com o mesmo id) tem `dirty` e `branch` desconhecidos. O arquivo só é lido quando o bundle
tem código montado (§9.1) ou o HEAD está destacado; ilegível, o próximo deploy `ok` o regrava do
zero e avisa no relatório, e uma falha ao gravá-lo vira linha do relatório, nunca erro. Com
código montado e o arquivo ilegível, o rollback de production segue (avisa e conta a release
limpa, porque production só sai de commit limpo) e o de development recusa. Os leitores do state
e deste arquivo ignoram campo que não conhecem.

Só release `ok` muda `current`. Lock em `<state_path>/.lock` (processo, máquina, momento); o
lock de um processo que já morreu nesta máquina é tomado sozinho (Linux, macOS e Windows); acima
de uma hora a mensagem sugere `--force-unlock`. Rollback: se a última tentativa falhou, reaplica
a `current`; senão, a `ok` anterior à `current`. Roda o deploy com os digests dela, sem build nem
pull, e grava o `git_sha` e o `dirty` dela. Sem release `ok` para onde voltar, o rollback
termina em erro dizendo para corrigir e rodar o deploy.

### 9.1 Código montado do disco

Código montado é o bind mount de um serviço (compose, sintaxe curta `./src:/app/src` ou longa
`type: bind`) cuja origem, resolvida a partir da pasta do arquivo compose, fica dentro da raiz do
projeto e fora de `.forge/`. A origem é interpolada com o que o compose enxerga sem os segredos:
o ambiente mínimo do processo (`PATH`, `HOME`, `USER`, `DOCKER_*` e afins, os mesmos que o forge
passa ao compose), o `env` literal do alvo e os `FORGE_*`. Variável que fica sem valor (segredo,
imagem de artifact, nome que o forge não passa ao compose) conta como código: é o lado seguro.
Volume nomeado, volume anônimo e bind mount de fora da raiz são dado: o rollback nunca os toca.

Se o bundle tem código montado, o rollback (o manual e a volta automática de production), sob o
lock e depois dos checks, leva o checkout ao commit da release de volta (`git checkout --detach
<git_sha>`) e carrega o bundle de novo dessa árvore: serviços, arquivos compose, jobs e o código
montado são todos os da release. Assim a imagem antiga sobe com o código e a definição dela.
O state e o lock continuam na pasta onde a tentativa os abriu, mesmo que o `forge.yaml` da
release declare outro `state_path`. Arquivo ignorado fica intacto: o rollback recusa antes de
qualquer checkout que passaria por cima dele (tabela abaixo). Depois, o HEAD fica destacado no
commit da release: a mensagem da volta automática diz `rode git checkout <branch> antes`, com a
branch de onde o checkout saiu (de `<alvo>.trees.json`), e o deploy de production seguinte,
recusado por HEAD fora da branch, repete o comando.

O rollback recusa, antes do checkout e sem mexer em nada (nem no ar nem no state), quando:

| Situação | Fim da mensagem |
|---|---|
| há mudança não commitada na raiz (fora `.forge/`), inclusive arquivo não rastreado | `guarde a mudança (git commit, git stash -u, ou mova o arquivo para fora do projeto) e rode de novo forge rollback -t <alvo>` |
| o commit da release rastreia um caminho que o HEAD não rastreia e que está ocupado no disco fora do git (ignorado): no próprio caminho há arquivo, ou diretório com arquivo, que o HEAD não rastreia, como um banco local ou um `.env` tirado do índice com `git rm --cached`; ou um diretório dele é arquivo ou symlink que o HEAD não rastreia, como `src/x` ignorado quando o commit rastreia `src/x/y.py`. A troca entre arquivo e diretório que o HEAD rastreia passa: é o git que a faz | a mesma, com `há arquivo fora do git no caminho do que o commit rastreia (<caminhos>)` |
| a raiz não é repositório git | `Rode forge deploy -t <alvo> a partir do commit que quer no ar` |
| a release de volta não guardou commit (`git_sha` nulo) ou subiu de árvore suja (`dirty` em `<alvo>.trees.json`, ou id terminado em `-dirty`) | `Rode forge deploy -t <alvo> a partir do commit que quer no ar` |
| não se sabe se a release de volta subiu de árvore limpa: ela não tem entrada que valha em `<alvo>.trees.json` e o alvo é development com id que não é `<sha12>` (uma tag ou um `--release`). Sem entrada, o id `<sha12>-dirty` é sujo em qualquer modo; production e o id `<sha12>` são limpos | `Rode forge deploy -t <alvo> a partir do commit que quer no ar` |
| o `git checkout` falha (commit ausente do clone) | `git checkout falhou: <saída do git>` |

Na volta automática de production, a recusa entra na mensagem `a volta automática para a release
<id> também falhou: ...` (§8.1). Bundle sem código montado não precisa de git para o rollback e
não tem o checkout mexido.

Imagens construídas (só development; em production nada é construído, então nada é removido):

| Momento | O que sai |
|---|---|
| deploy `ok` | as imagens das releases deste alvo fora do alcance do rollback; o alcance de cada alvo de development é a `current` e a candidata do rollback dele |
| `destroy` | as imagens das releases deste alvo que o rollback de nenhum outro alvo de development alcança |

Depois de um `destroy`, o `rollback` em development não acha mais a imagem antiga: o caminho é
um deploy novo.

## 10. Modos

| | development | production |
|---|---|---|
| imagem | build se houver | só pull |
| git | livre; árvore suja vira `-dirty` | ref casa com `targets.<alvo>.git`; árvore limpa (ignora `.forge/`) |
| confirmação | nenhuma | `--yes` em deploy, run, rollback, restore e env (o envelope leva segredo) |
| agenda e evento | desligados | ligados |
| falha depois de subir | erro com o comando do rollback | volta sozinho à release anterior, salvo com o state ilegível ou que não grava (§8.1) |
| destroy | permitido | recusado |

Os checks de production, nesta ordem: `--yes`; `targets.<alvo>.git` declarado; árvore limpa;
branch ou tag casa; todo artifact tem `pull`; nenhum artifact puxa do registro de exemplo do
molde (POL015). O que falha recusa o deploy antes de mexer em algo.

## 11. Política (`forge validate`)

`forge validate` carrega todos os alvos (ou só o de `-t`) e aplica as regras abaixo. Não lê
segredo, não fala com o Docker e não muda nada. Erro reprova sempre; aviso reprova com
`--strict` ou `policy.strict: true`. Cada achado sai numa linha
`<NÍVEL> <regra> <onde>: <mensagem>`: `ERRO` e `AVISO` para o que reprova, `aviso` para o que só
informa. A última linha conta `reprovando`, `aviso(s)` e `exceção(ões)`.

| Regra | Nível | Acusa | Como resolver |
|---|---|---|---|
| POL001 | erro | alvo production sem `git` | declare `targets.<alvo>.git: {tag: "v*"}` ou `{branch: ...}` |
| POL002 | erro | literal com cara de segredo em `env` ou `variables`: pelo nome (PASS, SECRET, TOKEN, API_KEY, PRIVATE, CREDENTIAL) ou pelo valor (chave privada PEM, token do GitHub, chave AWS, `sk-...`, token do Slack, URL com usuário e senha) | troque o valor por `secret://NOME` e ponha o valor no provider do alvo (§5) |
| POL003 | erro | alvo production com artifact só de `build` | declare `pull` no artifact |
| POL004 | erro | envelope (`.forge/env/<alvo>.env`) ou arquivo do provider `dotenv` fora do `.gitignore` | acrescente o caminho ao `.gitignore` (o `init` já põe `.forge/`) |
| POL005 | erro | dois alvos com o mesmo `DOCKER_HOST`, `project` e `state_path` | dê `project` ou `state_path` próprio a um deles |
| POL006 | erro | serviço em `release_services` ou `health.healthy` que não existe nos arquivos compose | corrija o nome ou o compose |
| POL007 | erro | arquivo compose que não existe (inclusive por `include:`) | crie o arquivo ou corrija `compose:` |
| POL008 | erro | o mesmo serviço compose em dois recursos service do mesmo project | deixe o serviço num recurso só, ou separe os projects |
| POL009 | aviso | artifact cujo `env` não aparece em nenhum arquivo compose | use `${<ENV>}` na `image:` do compose, ou remova o artifact |
| POL010 | erro | route para serviço compose que não existe | corrija `service` na route |
| POL011 | erro | `backup.job` de storage que não existe entre os jobs | crie o job ou corrija o nome |
| POL012 | erro | route num alvo sem `proxy` | declare `targets.<alvo>.proxy` |
| POL013 | erro | alvo com proxy nginx e route sem `tls` | declare `tls: {cert, key}` na route |
| POL014 | erro | task com `service` que não existe nos arquivos compose | corrija `service` na task |
| POL015 | aviso | alvo production que puxa imagem do registro de exemplo do molde (`ghcr.io/example`) | troque `variables.registry` no `forge.yaml` pelo registro real, e `REGISTRO` e `IMAGEM` de `.github/workflows/imagem.yml` se ele existir (e o passo de login dele, se o host mudar entre `ghcr.io` e outro). O deploy de production recusa mesmo sem `--strict` |
| POL016 | aviso | recurso gerado por receita sem, no `env` do `forge.yaml` ou do alvo, as variáveis que o código da receita lê | declare cada uma em `env` (segredo como `secret://NOME`); a lista está em `forge recipes`. Variável com padrão no compose (`${NOME:-x}`), variável que um artifact preenche (`artifacts.<nome>.env`) e nome `FORGE_*` não contam |
| POL017 | erro | job cujo `flow`, ou task cujo `python arquivo.py` ou `python -m modulo`, aponta para arquivo que não existe; o módulo só é conferido quando o pacote é do projeto (em `src/`, qualquer pasta; na raiz, pasta com `__init__.py` ou `<nome>.py`), e pacote namespace na raiz não é conferido | crie o arquivo ou corrija o caminho no job |
| POL018 | aviso | task `python -m modulo` cujo módulo só existe em `src/`, num projeto sem `[build-system]` no `pyproject.toml` (o `uv sync` não instala o projeto e o módulo não importa) | declare `[build-system]` (e o pacote em `src/`) no `pyproject.toml` e rode `uv sync`, ou declare `PYTHONPATH: src` no `env` do alvo |
| POL019 | aviso | recurso gerado por receita cujo código importa pacote ausente de `[project].dependencies` | rode o `uv add` que a mensagem traz. Receita com `runs_in_image: true` fica de fora (§14) |
| DEB001 | erro | entrada de débito sem `regra`, `motivo` ou `mitigacao` | complete a entrada em `docs/debito-tecnico.md` |
| DEB002 | aviso | gatilho de débito com data vencida | pague o débito ou mude o gatilho |

POL016 e POL019 reconhecem a receita pela primeira linha não vazia do arquivo que ela gera em
`resources/`: `# Receita <nome>`. Arquivo sem esse cabeçalho (escrito à mão, ou com a linha
apagada) não é cobrado. Receita que não gera arquivo ali (por exemplo `postgres`, `duckdb`,
`polars`) também não é cobrada pelo `validate`: o `init --recipe` imprime o que declarar e o que
instalar, e a conferência fica com você.

Achado excusado por entrada do débito aparece à parte e não reprova.

## 12. Débito técnico (`docs/debito-tecnico.md`)

```
## POL002 - título curto
- regra: o que a regra exige
- motivo: por que não dá para cumprir agora
- mitigacao: o que se faz enquanto isso
- dono: opcional
- gatilho: opcional (data AAAA-MM-DD ou evento)
- instancia: opcional (o `where` do achado; sem ele a regra inteira fica excusada)
```

Como tratar: registrar o atalho no ato; ler o débito antes de mexer na área; pagar ao passar pelo
código quando couber; gatilho vencido passa na frente de feature; pago sai do arquivo.

## 13. CLI

`forge <verbo>` e `python -m forge <verbo>` são o mesmo comando. Opções globais, antes do verbo:
`--root <pasta>` (raiz do bundle, padrão `.`) e `--version`. Os verbos que tocam um alvo aceitam
`-t/--target` (sem ele, o alvo com `default: true`, ou o único alvo), `--var NOME=VALOR` (repetível) e
`--release`.

| Verbo | Opções | Muda o alvo | Faz |
|---|---|---|---|
| `init [arquétipo]` | `--recipe` (repetível), `--config-file`, `--no-input`, `--vendor` | não; escreve arquivos no repositório | cria o esqueleto; nunca sobrescreve; com `--recipe` num repo que já tem `forge.yaml`, só acrescenta a receita |
| `validate` | `--strict`, `-t` | não | manifesto, política e débito (§11) |
| `plan` | `-t` | não; precisa do Docker | por service: criar, atualizar, sem mudança, remover, não comparado; não lê segredo |
| `deploy` | `-t`, `--yes`, `--force-unlock` | sim | §8 |
| `run <job>` | `-t`, `--yes`, `--seguir`, `--timeout` | sim; cria uma rodada no Prefect | dispara o deployment do Prefect; `--seguir` acompanha a rodada criada (§7.2) |
| `seguir <job>` | `-t`, `--timeout`, `--continuo` | não | acompanha a rodada em curso do job, ou a última que começou (§7.2) |
| `destroy` | `-t`, `--data`, `--force-unlock` | sim | só development; derruba deployments, routes e services e remove as imagens construídas (§9); storage só sai com `--data` |
| `rollback` | `-t`, `--yes`, `--force-unlock` | sim | §9 |
| `restore <storage>` | `-t`, `--yes`, `--force-unlock` | sim; sobrescreve dado | §7.3 |
| `summary` | `-t`, `-o text\|json` | não | foto estática, sem segredo; lista débito e gatilhos |
| `env` | `-t`, `--output` ou `--names`, `-- cmd`, `--yes` em production | não; escreve o envelope local, com segredo | §6 |
| `recipes` | `-o text\|json` | não | lista as receitas; roda fora de um bundle |
| `schema [tipo]` | tipo: `service`, `job`, `storage` ou `route` | não | imprime o JSON Schema do `forge.yaml` (sem tipo) ou do recurso; roda fora de um bundle |

`forge schema` sai do mesmo modelo que o loader usa: campos, tipos e, fora de `route`, o que é
obrigatório. Regra que cruza campos (route `private` exige `allow`) e conversão de tipo (o
loader aceita `port: "8000"`, o schema pede número) ficam com o `forge validate`. O de `route`
não exige campo nenhum, nem dentro de `tls`, porque a raiz e o alvo se completam, e aceita
`targets`, o mapa de alvo para os campos que só aquele alvo declara. A saída é JSON só com
caracteres ASCII: letra acentuada sai escapada, no formato `\uXXXX`. Tipo desconhecido é erro:

```
$ forge schema foo
erro: tipo de recurso 'foo' desconhecido: o forge só lê resources/<nome>.<tipo>.yaml com tipo service, job, storage, route; rode `forge schema <tipo>` com um deles, ou `forge schema` para o forge.yaml
```

Sem `forge.yaml` na raiz, todo verbo que lê o bundle diz onde procurou e o próximo passo:

```
$ forge validate
erro: não há forge.yaml em /caminho/vazio: rode `forge init` nesta pasta para criar um, ou aponte a pasta do bundle com `forge --root <pasta> <verbo>`
```

Com o Docker instalado e o daemon parado, o verbo que precisa dele (`plan`, `deploy`,
`rollback`, `destroy`, `restore`) termina numa linha que diz que o daemon não respondeu e manda
conferir com `docker info`.

### Códigos de saída

| Código | Quando |
|---|---|
| 0 | ok |
| 1 | `validate` reprovou; ou a rodada acompanhada por `run --seguir` ou `seguir` terminou em estado final que não é `Completed` |
| 2 | erro do forge (linha `erro: ...` no stderr); argumento inválido; erro inesperado, que sai com traceback e o endereço para relatar |
| 3 | o teto do `--timeout` venceu com a rodada ainda em curso (parou de acompanhar; a rodada não falhou) |
| 130 | Ctrl-C em `run --seguir` ou `seguir` (para de acompanhar; a rodada continua no Prefect) |
| o do comando | `forge env -t <alvo> -- <cmd>` devolve o código de saída do comando |

### Instalação

O forge se instala como biblioteca: o projeto declara
`forge = ["forge @ git+https://github.com/votumhub/forge@v<versão>"]` em `[dependency-groups]`
no `pyproject.toml` e o materializa com `uv sync --group forge`; daí em diante roda
`uv run forge ...`. O `init` escreve esse grupo, apontando para a versão do forge que o rodou. O
grupo não é padrão: `uv sync` sem `--group forge` remove o forge do `.venv` (o `uv run` sozinho
não remove). `--vendor` continua existindo (copia o forge para `./forge`, não grava o grupo, e o
CI gerado roda `uv run --no-sync python -m forge`), mas não é o caminho recomendado.
`deploy --run` é aceito e ignorado (compatibilidade).

## 14. init, arquétipos e receitas

| Arquétipo | O que gera |
|---|---|
| `app` (padrão) | um service com imagem própria (`Dockerfile`, `compose.yaml`) e health em `/health`; porta só em `127.0.0.1` (`APP_BIND`); `pyproject.toml` com `[build-system]`; um teste que passa; `.github/workflows/imagem.yml`, que publica a imagem a cada tag `v*` |
| `pipeline` | Prefect server e worker como services, um job agendado, passos puros testáveis |
| `automation` | um job curto num Prefect que já existe |

Todo arquétipo gera `FORGE.md`, `docs/debito-tecnico.md`, `.github/workflows/forge.yml` (§15) e
`pyproject.toml` com o grupo `forge`, e acrescenta `.forge/`, `__pycache__/`, `.venv/` e
`.pytest_cache/` ao `.gitignore`. Templates usam os delimitadores `[[ ]]` e `[% %]`.

Respostas do `init`: `name` (padrão: o `[project].name` do `pyproject.toml` que já existe, senão
o nome da pasta), `python` (`3.12`), `dev_branch` (`develop`) e as do arquétipo. No `app`,
`registry` vem de `ghcr.io/<dono>` quando o remoto `origin` está no GitHub, senão
`ghcr.io/example`. `--no-input` aceita os padrões; `--config-file respostas.yaml` responde por
arquivo (`chave: valor`; chave desconhecida é erro).

Receita: `forge/recipes/<nome>/recipe.yaml` mais os arquivos que ela gera uma vez, cada um com
comentário de topo (como conecta, variáveis, dev e prd, link da documentação).

| Campo do `recipe.yaml` | Significado |
|---|---|
| `description`, `family` | o que aparece em `forge recipes` |
| `env` | variáveis que o código da receita lê; o `validate` cobra (POL016) |
| `requires` | pacotes que o código da receita importa, no formato `nome[extra]>=versão`; o `init --recipe` imprime o `uv add` pronto e o `validate` cobra (POL019) |
| `runs_in_image` | `true` quando o código da receita roda dentro da imagem do contêiner, não no `.venv` do repositório (`airflow`, `bytewax`, `dagster`, `fastapi`): o `requires` vale para a imagem e o POL019 não se aplica |
| `docs` | `{url, version}` da documentação da ferramenta |
| `provada` | a receita já rodou contra o serviço real: `dbt-core`, `duckdb`, `mssql`, `postgres`, `prefect` |

Receitas que precisam de passo no deploy trazem `resources/<receita>-<ação>.job.yaml` com
gatilho deploy (por exemplo, o bootstrap de bucket).

A expiração de objeto nas receitas de bucket é opcional: o job de bootstrap cria o bucket do
alvo que falta e só grava a regra de expiração se a variável vier no `env` do alvo, com um
inteiro de dias, 1 ou mais. Sem a variável, nenhuma regra é gravada, e a regra que o bucket já
tenha não é removida. Com a variável declarada, o bootstrap substitui as regras de ciclo de vida
que o bucket já tem.

| Receita | Variável de expiração |
|---|---|
| `aws-s3` | `AWS_S3_LIFECYCLE_DAYS` |
| `minio` | `MINIO_LIFECYCLE_DAYS` |
| `cloudflare-r2` | `R2_LIFECYCLE_DAYS` |

A receita `github-actions-cron` instala o forge como o `forge.yml` (uv e o grupo `forge` do
`pyproject.toml`; vendorizado, roda o que mora em `./forge`) e roda em `vars.FORGE_RUNNER` ou,
sem ela, em `ubuntu-latest`. O comando agendado leva `--yes`: quem versiona esse workflow assina a
rodada agendada em produção.

## 15. CI gerado (`.github/workflows/forge.yml`)

| Evento | Job | Onde roda | O que faz |
|---|---|---|---|
| pull request | `validar` | `vars.FORGE_CI_RUNNER`, ou `ubuntu-latest` sem ela; PR de fork vai a `ubuntu-latest` | `forge validate --strict` |
| push na branch de dev (`dev_branch`) | `dev` | `vars.FORGE_RUNNER` | `forge deploy -t dev --yes` |
| push de tag `v*` | `dev` | `vars.FORGE_RUNNER` | `forge plan -t prd` |
| Run workflow (dispatch) | `prd` | `vars.FORGE_RUNNER`, environment `production` | `forge deploy -t prd --yes`; o forge recusa ref que não casa `targets.prd.git` |
| push ou dispatch sem `FORGE_RUNNER` | `sem-runner` | `ubuntu-latest` | não faz deploy: escreve "Deploy pulado" no resumo do run |

| Variável de repositório | Sem ela |
|---|---|
| `FORGE_RUNNER`: rótulo de um runner self-hosted numa máquina que alcança o host do deploy | o CI não faz deploy nenhum, nem dev nem prd (numa máquina do GitHub o deploy não teria onde entregar) |
| `FORGE_CI_RUNNER`: rótulo do runner que valida os PRs | a validação roda em `ubuntu-latest` |

As duas ficam em Settings > Secrets and variables > Actions > Variables. O workflow tem
`permissions: contents: read`, actions fixadas por SHA e `concurrency` por ref.

PR de fork: a guarda que manda o PR de fork para `ubuntu-latest` mora no próprio arquivo de
workflow, e o PR de fork pode editá-lo. Em repositório público, ou não registre runner próprio,
ou exija aprovação antes de rodar workflow de PR de fork (Settings > Actions > General). O
environment `production` só pede aprovação se você configurar a proteção em Settings >
Environments. Segredo de alvo com provider `env` entra no CI pelo `env:` do passo de deploy.

O `imagem.yml` do arquétipo `app` publica a imagem para amd64 e arm64 a cada tag `v*`, em
`ubuntu-latest`. Em `ghcr.io` ele entra com o `GITHUB_TOKEN`; em outro registro, com os segredos
de repositório `REGISTRY_USER` e `REGISTRY_TOKEN`.
