# Changelog

Mudanças que importam para quem usa o forge. O formato do manifesto continua `forge: 2`.

## 3.0.0

Primeira versão pública, sob licença MIT. Quem vem da 2.2.4 deve ler primeiro a quebra de
compatibilidade.

### Quebra de compatibilidade

| O que muda | Quem é afetado | O que fazer |
|---|---|---|
| O CI gerado (`.github/workflows/forge.yml`) só faz deploy com a variável de repositório `FORGE_RUNNER`. Sem ela, nenhum deploy roda (nem dev, nem prd) e o run escreve "Deploy pulado" no resumo. A validação de PR roda em `FORGE_CI_RUNNER` ou, sem ela, em `ubuntu-latest` | repositório criado pelo `init` da 3.0.0; o `init` não sobrescreve o `forge.yml` de quem já tem | defina `FORGE_RUNNER` com o rótulo do runner self-hosted que alcança o host do deploy |
| A receita `github-actions` troca a variável `FORGE_RUNNER` por `FORGE_CI_RUNNER`, e PR de fork roda sempre em `ubuntu-latest` | quem já gerou a receita | apague o `github-actions-ci.yml` antigo e rode `forge init --recipe github-actions` de novo (o `init` nunca sobrescreve); depois renomeie a variável de repositório |
| A expiração padrão de 14 dias saiu das receitas `aws-s3`, `minio` e `cloudflare-r2`. A regra só é gravada com `AWS_S3_LIFECYCLE_DAYS`, `MINIO_LIFECYCLE_DAYS` ou `R2_LIFECYCLE_DAYS` no `env` do alvo; sem a variável, a regra que o bucket já tem não é removida; com ela, o bootstrap substitui as regras de ciclo de vida que o bucket já tem | quem conta com a expiração automática | declare a variável com o número de dias |
| Deploy em alvo `production` que falha depois de começar a subir volta sozinho à release anterior, pelo mesmo caminho do `rollback`, e termina em erro. Com o state ilegível, ou se ele não consegue gravar o desfecho, não volta: a mensagem diz a release que o state marca e a que ficou no ar | quem tratava a falha de produção à mão | leia a mensagem: ela diz para qual release voltou |
| O deploy bom remove as imagens construídas que o rollback de nenhum alvo de development alcança; o `destroy` remove as do alvo. Depois de um `destroy`, o `rollback` em development não acha mais a imagem antiga | quem fazia rollback em dev depois de `destroy` | rode um deploy novo |
| Regras novas no `validate`: POL017 (erro) reprova job que aponta para arquivo que não existe; POL015, POL016, POL018 e POL019 (avisos) reprovam com `--strict`, que é como o CI gerado roda | bundle que já existe | rode `forge validate --strict` e siga o "como resolver" de cada regra na referência |
| O deploy de production recusa artifact que puxa do registro de exemplo do molde (`ghcr.io/example`), mesmo sem `--strict` | bundle do molde `app` que nunca trocou `variables.registry` | troque pelo registro real |
| `init --vendor` não grava mais o grupo `forge` no `pyproject.toml`, e o CI gerado roda o forge vendorizado (`uv run --no-sync python -m forge`) | quem usa `--vendor` | nada, se o forge mora em `./forge` |
| A receita `github-actions-cron` instala o forge como o `forge.yml` (uv e o grupo `forge` do `pyproject.toml`) e não lê mais a variável `FORGE_SOURCE`; o comando agendado leva `--yes`, então quem versiona o workflow assina a rodada agendada em produção | quem já gerou a receita | apague o `github-actions-cron.yml` antigo e rode `forge init --recipe github-actions-cron` de novo (o `init` nunca sobrescreve); a variável de repositório `FORGE_SOURCE` pode ser apagada |
| O `init` tira o nome do bundle do `[project].name` do `pyproject.toml` que já existe (antes, da pasta) e o registro do remoto `origin` no GitHub (`ghcr.io/<dono>`) | quem roda `init --no-input` num projeto que já existe | confira `bundle.name` e `variables.registry` depois do `init` |

### Novo

- `forge schema [service|job|storage|route]`: imprime o JSON Schema do `forge.yaml` ou de um
  tipo de recurso, derivado do mesmo modelo que o forge usa para carregar. Roda fora de um
  bundle.
- Regras POL015 a POL019 no `validate`: registro de exemplo em production, receita sem as
  variáveis que pede, job que aponta para arquivo inexistente, módulo em `src/` num projeto que
  não se instala, receita sem os pacotes que importa.
- `requires` e `runs_in_image` no `recipe.yaml`: a receita diz o que instalar; o
  `init --recipe` imprime o `uv add` pronto.
- O CI gerado declara `permissions: contents: read`, fixa as actions por SHA e diz no resumo
  quando pulou o deploy.
- O molde `app` publica a porta só em `127.0.0.1` (`APP_BIND`), declara `[build-system]` e
  responde 404 a rota desconhecida.
- O `init` acrescenta `.venv/` e `.pytest_cache/` ao `.gitignore`.
- Mensagens de erro que dizem o próximo passo: repositório sem `forge.yaml`, Docker ausente ou
  parado, pull negado pelo registro, `rollback` e `destroy` sem release registrada.
- Erro inesperado sai com o endereço para relatar o defeito.
- README, guia para agentes (`docs/para-agentes.md`), `llms.txt`, `SECURITY.md` e licença MIT.

### Consertado

- O repositório criado pelo molde `app` nascia com o teste vermelho.
- Cada deploy em development deixava uma imagem construída que nem o `destroy` tirava.
- No Windows, a trava de deploy nunca reconhecia que o processo dono tinha morrido.
- No Windows, um comando sem extensão ao lado do `.exe` ou `.cmd` quebrava com `WinError 193`.
- `forge env -- <cmd>` com um comando que o sistema encontra mas não consegue rodar terminava
  em traceback; agora termina em uma linha de erro.
- O bootstrap de bucket das receitas `aws-s3`, `minio` e `cloudflare-r2` confere o próprio
  bucket com `head_bucket` em vez de listar a conta, o que funciona com chave restrita ao
  bucket.
- Arquivo de segredos do provider `dotenv` que não existe agora termina em erro que manda
  criá-lo, com o formato da linha.
- O `FORGE.md` gerado dizia que o teto do `--timeout` saía com código 2; o código é 3.
- Arquivo do repositório fora de UTF-8 (`forge.yaml`, recurso, compose, `pyproject.toml`,
  `.gitignore`, arquivo de segredos, débito técnico, `--config-file`, state em `.forge/state/`,
  arquivo de route no `plan`) termina em uma linha de erro com o nome do arquivo,
  não em traceback.
- Com a saída capturada (CI, agente), a linha `erro:` aparecia antes do progresso; agora é a
  última.
- Com a pasta do state sem permissão de escrita, o erro do lock (`Permission denied` no
  `.lock`) escondia a causa do deploy; agora a causa sobe, a gravação vira erro do state e o
  lock que não sai vira aviso.
- `forge env` e `forge summary` mostravam as imagens de uma tentativa falha quando ela tinha o
  mesmo id da release atual; agora mostram as da última entrada boa.
