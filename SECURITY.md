# Segurança

## Como relatar uma vulnerabilidade

Relate em particular pelo GitHub: na aba **Security** do repositório
[github.com/votumhub/forge](https://github.com/votumhub/forge), use **Report a vulnerability**.
Não abra issue pública para vulnerabilidade.

Inclua a versão (`forge --version`), o comando, o `forge.yaml` mínimo que reproduz e o que
acontece. Tire todo valor de segredo do relato.

## Escopo

| Dentro | Fora |
|---|---|
| O pacote `forge`: CLI, carga do `forge.yaml`, resolução de segredo, envelope, deploy, rollback, destroy, restore | O Docker, o compose, o Prefect, o proxy e as ferramentas que as receitas configuram |
| Os arquivos que o `init` gera: moldes, receitas e o workflow `.github/workflows/forge.yml` | O host do deploy, o runner e o registro de imagens, que são seus |
| Vazamento de segredo pelo forge: em mensagem, log, arquivo versionado ou no Prefect | Segredo que o seu código ou o seu comando imprime (`forge env -- <cmd>` não mascara a saída do comando) |

## Dois cuidados de operação

### Runner próprio e PR de fork em repositório público

O workflow gerado manda o PR que vem de fork para `ubuntu-latest`, mas essa guarda mora no
próprio arquivo de workflow, e o PR de fork pode editá-lo. Num repositório público com runner
self-hosted (`FORGE_RUNNER` ou `FORGE_CI_RUNNER`), um PR de fork pode rodar código de terceiro
na sua máquina, que alcança o host do deploy. Escolha um:

- não registre runner próprio num repositório público; ou
- exija aprovação antes de rodar workflow de PR de fork: Settings > Actions > General, aprovação
  para todos os colaboradores externos.

O environment `production` do job de prd só pede aprovação se você configurar a proteção em
Settings > Environments.

### Segredo só por `secret://NOME`

Valor sensível entra no `forge.yaml` só como referência, `secret://NOME`, no `env` comum ou do
alvo. O provider de segredo do alvo resolve o nome na hora do comando:

| Provider | Lê de |
|---|---|
| `env` | o ambiente de quem roda o forge (o caso do CI) |
| `dotenv` | um arquivo `NOME=valor` fora do git; o `validate` reprova se não estiver no `.gitignore` (POL004) |
| `exec` | o stdout de um comando, como JSON `{"NOME": "valor"}`; roda sem shell, com prazo, e o stdout nunca aparece em mensagem |

O que o forge garante:

- segredo que falta é erro, nunca valor vazio; valor com quebra de linha é recusado;
- o `validate` reprova literal com cara de segredo no `env` ou em `variables` (POL002);
- o envelope de cada alvo (`.forge/env/<alvo>.env`) é escrito com permissão 0600 e fica fora do
  git (`.forge/` no `.gitignore`, POL004);
- a saída dos comandos que o deploy roda sai com os valores secretos mascarados;
- nenhum segredo vai para o Prefect: o job leva só `FORGE_TARGET`, `FORGE_RELEASE`,
  `FORGE_ENV_FILE` e `FORGE_ROOT`, e o worker lê o envelope;
- `summary` e `plan` não resolvem segredo;
- em production, escrever o envelope (`forge env`) exige `--yes`.
