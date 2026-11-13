# forge

O forge leva um repositório do zero ao deploy em dev e em produção, num host com Docker.
Você descreve o projeto num `forge.yaml` e em `resources/*.yaml`; o forge gera o esqueleto,
confere o contrato, monta o ambiente de cada alvo e opera o `docker compose`, com release
registrada, rollback e destroy. O código do projeto só lê variáveis de ambiente.

## Para quem serve

| Serve | Não serve |
|---|---|
| Projeto que roda em `docker compose` num host que você controla | Kubernetes, ECS, Cloud Run e outras plataformas gerenciadas |
| Serviço HTTP, pipeline de dados com Prefect, automação curta | Orquestrar vários hosts de uma vez (um alvo é um host) |
| Quem quer dev e prd do mesmo manifesto, prd só por tag git | Deploy sem Docker: o forge não instala unit systemd nem processo solto |
| Agentes de IA que operam o repositório sozinhos: saída em texto ou JSON, código de saída estável, erro em uma linha com o próximo passo | Projeto que guarda credencial no repositório: o `validate` recusa literal com cara de segredo |

O esqueleto que o `init` gera é Python. O forge opera qualquer serviço que tenha um arquivo
compose.

## Pré-requisitos

| Ferramenta | Para quê |
|---|---|
| [uv](https://docs.astral.sh/uv/) | instala o forge e roda os verbos (`uv run forge ...`) |
| Python 3.12 ou 3.13 | o forge exige `>=3.12,<3.14`; o uv baixa se faltar |
| Docker com o plugin compose (`docker compose`) | `plan`, `deploy`, `rollback`, `destroy`, `restore` |
| git | a release sai do commit ou da tag; produção exige git |

No Windows, com o cache do uv ou o projeto numa pasta funda, a instalação pelo git pode falhar
com `Filename too long`: o forge tem arquivos com perto de 100 caracteres de caminho. Rode
`git config --global core.longpaths true` uma vez e instale de novo.

## Do repositório vazio ao primeiro deploy

O repositório começa vazio, com git. O `uvx` roda o `init` sem instalar nada antes.

```
mkdir meu-servico && cd meu-servico
git init -b develop
git remote add origin https://github.com/minha-org/meu-servico.git
uvx --from "git+https://github.com/votumhub/forge@v3.0.0" forge init --no-input
```

O `remote` é opcional e serve para uma coisa: o `init` lê dali o registro de imagens de
produção (`ghcr.io/minha-org`). Sem ele, o padrão é o registro de exemplo `ghcr.io/example`, e o
`validate --strict` acusa POL015 até você trocar `variables.registry` no `forge.yaml`. Para trocar o registro depois
do `init`, mude também `REGISTRO` e `IMAGEM` em `.github/workflows/imagem.yml` e, se o host passa de
`ghcr.io` para outro (ou o contrário), o passo de login do mesmo arquivo: `ghcr.io` usa
`github.actor` e `secrets.GITHUB_TOKEN`, qualquer outro host usa `secrets.REGISTRY_USER` e
`secrets.REGISTRY_TOKEN`.

O `init` imprime cada arquivo criado e nunca sobrescreve um que já existe:

```
bundle meu-servico (app) em /caminho/meu-servico
  criado     .github/workflows/forge.yml
  criado     FORGE.md
  criado     docs/debito-tecnico.md
  criado     .dockerignore
  criado     .github/workflows/imagem.yml
  criado     compose.yaml
  criado     Dockerfile
  criado     forge.yaml
  criado     pyproject.toml
  criado     resources/app.service.yaml
  criado     src/meu_servico/__init__.py
  criado     src/meu_servico/app.py
  criado     tests/test_app.py
  criado     .gitignore
próximo passo: leia FORGE.md e rode `uv run forge validate`
```

Instale o forge no `.venv` do projeto e confira o contrato:

```
uv sync --group forge
uv run forge validate --strict
```

Versione o `uv.lock` que o uv cria. Sem ele no git, o CI o recria e o deploy de produção recusa
por árvore suja.

```
alvos: dev, prd
0 reprovando, 0 aviso(s), 0 exceção(ões)
```

Suba em dev. O deploy constrói a imagem, escreve o ambiente do alvo em `.forge/env/dev.env`,
roda `docker compose up`, espera `GET /health` responder e registra a release. Ele imprime uma
linha por passo e termina em `release <id> ok`.

```
uv run forge deploy -t dev
curl http://127.0.0.1:8000/health
```

A porta fica só no loopback (`APP_BIND: 127.0.0.1` no `forge.yaml`). Para desfazer tudo em
dev, inclusive as imagens que o forge construiu:

```
uv run forge destroy -t dev
```

O `destroy` só existe em alvo `development` e não apaga volume sem `--data`.

## Instalar num projeto que já existe

O forge é uma dependência do projeto, num grupo próprio do `pyproject.toml`. É esta a linha
que o `init` grava:

```toml
[dependency-groups]
forge = ["forge @ git+https://github.com/votumhub/forge@v3.0.0"]
```

```
uv sync --group forge
uv run forge init --no-input
```

O grupo não é padrão: `uv sync` sem `--group forge` tira o forge do `.venv`. Num repositório que
já tem `forge.yaml`, `forge init --recipe <nome>` só acrescenta os arquivos da receita.

## Produção em uma frase

Produção sai só de commit limpo, com ref que casa `targets.prd.git` (no molde, uma tag `v*`),
com `--yes`, e puxa a imagem do registro em vez de construir. Se o deploy de produção falha
depois de começar a subir, o forge volta sozinho à release anterior e termina em erro (se o
state está ilegível ou não consegue gravar o desfecho, ele não volta e diz onde o serviço ficou). Os
detalhes estão em [docs/referencia.md](docs/referencia.md).

## Documentação

| Arquivo | Para quem e quando |
|---|---|
| [docs/para-agentes.md](docs/para-agentes.md) | agente de IA ou pessoa que vai operar o forge: ordem segura, o que muda o alvo, como ler erro |
| [docs/referencia.md](docs/referencia.md) | contrato completo: `forge.yaml`, recursos, verbos, regras do `validate`, CI gerado, códigos de saída |
| `FORGE.md` (gerado no seu repositório) | a página de uma folha que o `init` deixa no projeto |
| [llms.txt](llms.txt) | índice curto para LLMs |
| [CHANGELOG.md](CHANGELOG.md) | o que mudou da 2.2.4 para a 3.0.0 |
| [SECURITY.md](SECURITY.md) | como relatar vulnerabilidade e os cuidados de operação |
| [ROADMAP.md](ROADMAP.md) | o que fica de fora de propósito e onde encaixaria |
| [docs/pesquisa-dab.md](docs/pesquisa-dab.md) | o modelo em que o forge se inspirou (Databricks Asset Bundles) |

O contrato também sai da própria CLI, sem ler código: `forge schema` (JSON Schema do
`forge.yaml` e de cada tipo de recurso), `forge recipes -o json` e `forge validate`.

## Licença

MIT. Copyright (c) 2026 VOTUM. Veja [LICENSE](LICENSE).
