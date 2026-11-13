# forge: guia para agentes

Este é o guia que um agente de IA (ou uma pessoa no primeiro dia) lê antes de operar o forge
num repositório. Regra de ouro: leia e confira à vontade; mude o alvo só em development; o que
mexe em produção ou apaga dado só sai com ordem explícita de uma pessoa.

O contrato completo está em [referencia.md](referencia.md). No repositório do projeto, o
`FORGE.md` gerado pelo `init` resume o mesmo para aquele bundle, e `docs/debito-tecnico.md`
lista os atalhos conscientes: leia os dois antes de mexer.

## Ordem segura

```
uv sync --group forge                 # instala o forge no .venv do projeto
uv run forge validate --strict        # contrato, política e débito; não muda nada
uv run forge summary -t dev -o json   # o bundle como o forge o entende, sem segredo
uv run forge plan -t dev              # o que o deploy mudaria; precisa do Docker
uv run forge deploy -t dev            # muda o alvo de development
```

Pare no primeiro passo que falhar e leia a mensagem até o fim: ela diz o próximo passo. Não
pule `validate`: o CI gerado roda `validate --strict` em todo PR, e aviso ali reprova.

## O que cada verbo faz com o alvo

| Verbo | Muda o alvo | Lê segredo | Quem decide |
|---|---|---|---|
| `validate`, `schema`, `recipes` | não | não | você |
| `summary` | não | não (mostra `secret://NOME`, nunca o valor) | você |
| `plan` | não | não (usa marcador no lugar do segredo) | você |
| `seguir` | não | sim, para chegar à API do Prefect | você |
| `env --names` | não | não | você |
| `env` e `env -- <cmd>` em development | não; escreve `.forge/env/<alvo>.env` com segredo | sim | você |
| `init`, `init --recipe` | não; escreve arquivos novos, nunca sobrescreve | não | você |
| `deploy`, `rollback` em development | sim | sim | você |
| `run` em development | sim; cria uma rodada no Prefect | sim | você |
| `destroy` em development, sem `--data` | sim; derruba serviços e remove imagens construídas | sim | você |
| `destroy --data` | sim; apaga volumes | sim | uma pessoa |
| `restore` | sim; sobrescreve dado com o backup | sim | uma pessoa |
| qualquer verbo com `--yes` em production (`deploy`, `rollback`, `run`, `env`) | sim, ou expõe segredo de produção | sim | uma pessoa |
| `--force-unlock` | tira a trava de outro deploy | não | uma pessoa, depois de confirmar que ninguém está fazendo deploy |

O forge já recusa production sem `--yes` e recusa `destroy` em production. O `--yes` existe
para uma pessoa assinar: não o acrescente por conta própria para destravar um comando. O mesmo
vale para disparar o workflow `forge` por dispatch no GitHub: é deploy em produção. A receita
`github-actions-cron` já traz o `--yes` no workflow agendado: quem versiona esse workflow assina
a rodada agendada em produção.

Com a variável de repositório `FORGE_RUNNER` definida, o workflow `forge` gerado faz isto: o
push na branch de desenvolvimento roda `forge deploy -t dev --yes`, a etiqueta `v*` roda
`forge plan -t prd` (só mostra o que mudaria) e o dispatch roda `forge deploy -t prd --yes` no
environment `production`. Nos moldes que trazem `imagem.yml`, a etiqueta `v*` também publica a
imagem, com ou sem `FORGE_RUNNER`. A guarda de git de produção (tag `v*` exata, `targets.prd.git`)
protege contra engano. Quem tem escrita no repositório edita `targets.prd.git` na branch, então
contra essa pessoa a proteção é o environment `production` com revisor obrigatório.

## Descobrir o contrato sem ler o código

| Pergunta | Comando |
|---|---|
| Que campos o `forge.yaml` aceita? | `uv run forge schema` |
| Que campos um recurso aceita? | `uv run forge schema service` (ou `job`, `storage`, `route`) |
| Que receitas existem, que variáveis pedem, que pacotes importam? | `uv run forge recipes -o json` |
| O bundle está coerente? | `uv run forge validate --strict` |
| Que alvos, serviços, jobs e release atual existem? | `uv run forge summary -t <alvo> -o json` |
| Que nomes de variável o programa recebe? | `uv run forge env -t <alvo> --names` |

`forge schema` e `forge recipes` rodam fora de um bundle. O schema sai do mesmo modelo que o
forge usa para carregar o `forge.yaml`: ele diz os campos, os tipos e, fora de `route` (que não
exige campo nenhum, porque a raiz e o alvo se completam), o que é obrigatório; campo
desconhecido em qualquer nível é erro. Regra que cruza campos (route `private` exige `allow`;
nome de alvo em `targets` de uma route que o `forge.yaml` não declara) passa no schema: quem
recusa é o `forge validate`, ao carregar o bundle.

A saída do `forge env --names` num repositório recém-criado com o molde `app`:

```
APP_BIND
APP_PORT
FORGE_ENV_FILE
FORGE_RELEASE
FORGE_ROOT
FORGE_STATE_PATH
FORGE_TARGET
```

Segredo aparece como `NOME (segredo)`.

## Como ler um erro

Todo erro esperado sai numa linha (ou poucas) no stderr, começando por `erro:`, com código de
saída 2. A mensagem diz o que falhou, por quê e o próximo passo:

```
erro: não há forge.yaml em /caminho/vazio: rode `forge init` nesta pasta para criar um, ou aponte a pasta do bundle com `forge --root <pasta> <verbo>`
```

```
erro: alvo prd é production: o envelope leva segredo de produção para um arquivo ou um comando; confirme com --yes (só os nomes: --names)
```

Erro com traceback e a linha `erro inesperado: ...` é defeito do forge, não do repositório: a
mensagem traz o endereço para relatar. Não tente contornar.

| Código | Significado | O que fazer |
|---|---|---|
| 0 | ok | seguir |
| 1 | `validate` reprovou; ou a rodada acompanhada terminou em estado que não é `Completed` | ler os achados (`ERRO`, `AVISO`) e corrigir; ou ler o log da rodada |
| 2 | erro do forge ou argumento inválido | ler a mensagem: ela traz o próximo passo |
| 3 | `--timeout` venceu com a rodada ainda em curso | não é falha da rodada; acompanhe de novo com `forge seguir` |
| 130 | Ctrl-C ao acompanhar | a rodada continua no Prefect |
| o do comando | `forge env -t <alvo> -- <cmd>` | é o código do seu comando |

Um `validate` que reprova, com um segredo escrito por engano no `env` do `forge.yaml`:

```
alvos: dev, prd
  ERRO  POL002 targets.dev.env.DB_PASSWORD: literal com cara de segredo; use secret://NOME
  ERRO  POL002 targets.prd.env.DB_PASSWORD: literal com cara de segredo; use secret://NOME
2 reprovando, 0 aviso(s), 0 exceção(ões)
```

Cada achado do `validate` sai como `<NÍVEL> <regra> <onde>: <mensagem>`. `ERRO` e `AVISO`
reprovam, `aviso` só informa. A tabela de todas as regras, com o que acusa e como resolver, está
em [referencia.md §11](referencia.md#11-política-forge-validate). Um atalho consciente que não dá
para pagar agora vai para `docs/debito-tecnico.md` (formato em §12 da referência); nunca
desligue uma regra de outra forma.

## Armadilhas reais

1. **Registro de exemplo.** Sem remoto `origin` no GitHub, o `init` do molde `app` grava
   `ghcr.io/example` como registro de produção. `validate` passa, `validate --strict` reprova
   com POL015 e o deploy de produção recusa. Para trocar o registro depois do `init`, mude três
   coisas. No `forge.yaml`, `variables.registry` (por exemplo `ghcr.io/minha-org`). Em
   `.github/workflows/imagem.yml`, `REGISTRO` (só o host, como `ghcr.io`) e `IMAGEM` (o resto do
   caminho do registro mais o nome do bundle, como `minha-org/meu-servico`). E o passo de login
   do mesmo arquivo: em `ghcr.io` ele usa `github.actor` e `secrets.GITHUB_TOKEN`; em qualquer
   outro host, `secrets.REGISTRY_USER` e `secrets.REGISTRY_TOKEN`, que você cria em Settings >
   Secrets and variables > Actions. O `init` escolheu um dos dois pelo host da hora; se o host
   muda entre `ghcr.io` e outro, troque também essas duas linhas.
2. **`uv sync` sem `--group forge` tira o forge do `.venv`.** Depois disso, `uv run forge`
   falha dizendo que não achou o programa `forge` (o texto exato muda por sistema). Rode `uv sync --group forge` de novo. `uv run` sozinho não
   remove o forge.
3. **Receita sem arquivo em `resources/` não é cobrada.** POL016 (variável faltando) e POL019
   (pacote faltando) só reconhecem a receita pela primeira linha do arquivo que ela gera em
   `resources/` (`# Receita <nome>`): não apague essa linha. `postgres`, `duckdb`, `polars` e
   outras não geram arquivo ali: o `validate` fica verde mesmo sem `POSTGRES_URL` declarado.
   Leia o que o `forge init --recipe <nome>` imprime e aplique.
4. **CI verde não quer dizer deploy feito.** Sem a variável de repositório `FORGE_RUNNER`, o
   workflow gerado não faz deploy nenhum: o job `sem-runner` passa e escreve "Deploy pulado" no
   resumo do run.
5. **Release e git.** Sem commit, a release é `unversioned`; com árvore suja em development,
   `<sha>-dirty`. Produção exige commit limpo (fora `.forge/`), ref que casa `targets.prd.git`
   e, no molde, tag `v*` exata no HEAD.
6. **Falha em produção volta sozinha, mas o comando falha.** Se o deploy de produção falha
   depois de começar a subir, o forge roda o rollback e termina com código 2. A mensagem diz
   para qual release voltou. Exceção: com o state ilegível ou que não grava, ele não volta, e a
   mensagem diz em que release o state parou, qual está no ar e o arquivo a consertar. Não rode
   outro deploy em cima sem ler.
7. **Falha em development não volta sozinha.** A mensagem traz o `forge rollback -t <alvo>`.
8. **`destroy` apaga as imagens construídas.** Depois dele, `rollback` em development não acha
   mais a imagem antiga: o caminho é um deploy novo.
9. **Trava de deploy.** `deploy`, `rollback`, `destroy` e `restore` no mesmo alvo não rodam
   juntos. A trava de um processo
   que já morreu nesta máquina sai sozinha; `--force-unlock` é só para quando uma pessoa confirma
   que nenhum deploy está rodando.
10. **`forge env -- <cmd>` não mascara a saída do comando.** Se o comando imprime um segredo,
    ele aparece. O deploy mascara; o `env` não.
11. **Prefixo `FORGE_` é do forge.** Variável sua com esse prefixo é recusada.
12. **O `init` nunca sobrescreve.** Linha `pulado <arquivo> (já existe; nada foi sobrescrito)`
    quer dizer que o seu arquivo ficou como estava; o conteúdo novo não entrou.
13. **Porta só no loopback.** O molde publica a porta em `127.0.0.1` (`APP_BIND`). Para abrir
    para fora, use uma route com proxy, não `0.0.0.0` direto.
14. **Expiração de bucket é opcional.** `aws-s3`, `minio` e `cloudflare-r2` só gravam regra de
    expiração com a variável `*_LIFECYCLE_DAYS` declarada; sem ela, nada expira. Com a variável
    declarada, o bootstrap substitui as regras de ciclo de vida que o bucket já tem.
15. **O arquivo de segredo de dev não nasce pronto.** O molde aponta o alvo `dev` para
    `.forge/secrets/dev.env`, mas o `init` não cria o arquivo. Enquanto o `env` não tem nenhum
    `secret://`, tudo funciona; no primeiro `secret://NOME`, todo verbo que monta o envelope
    falha com `erro: o arquivo de segredos .../.forge/secrets/dev.env não existe: ...`.
    Crie o arquivo com uma linha `NOME=valor` por segredo (ele já está fora do git por
    `.forge/`).
16. **Versione o `uv.lock`.** O `uv sync --group forge` cria o `uv.lock` se ele não está no git.
    Sem ele versionado, o CI o recria, a árvore fica suja e o deploy de produção recusa com
    `erro: alvo prd: árvore suja; production só sai de commit limpo`. Depois do primeiro
    `uv sync --group forge`, faça commit do `uv.lock`.

## Segredo

Valor sensível entra no `forge.yaml` só como `secret://NOME` no `env`. O provider do alvo
resolve na hora: `env` (ambiente de quem roda o forge, o caso do CI), `dotenv` (arquivo fora do
git; no molde, `.forge/secrets/dev.env`) ou `exec` (um comando que devolve JSON). Segredo que
falta é erro, nunca valor vazio. Nunca escreva o valor de um segredo no `forge.yaml`, em arquivo
versionado ou em mensagem: o `validate` recusa literal com cara de segredo (POL002).
