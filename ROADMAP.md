# Roadmap do forge

O que fica de fora do forge (formato `forge: 2`) de propósito, e onde cada item encaixa quando entrar. Nenhum
item aqui exige mudar o motor: cada um é uma implementação nova de um protocolo que já existe
em `forge/contracts.py`, ou um verbo novo sobre o loader.

| Item | Onde encaixa |
|---|---|
| systemd + timer (serviço e agenda sem container) | `Adapter` novo para `resources/<x>.service.yaml` com `systemd:` e para job de `trigger.schedule` com `scheduler: systemd`; `up` escreve a unit em `.forge/build/<alvo>/` e roda `systemctl` pelo `Executor`. |
| Host remoto por ssh | `Executor` novo que embrulha o argv em `ssh <host> --`; o orquestrador não muda. O alvo ganha `host` no `Target`. |
| sync (copiar o bundle para o host antes do deploy) | Job de gatilho `deploy` (`before_deploy`) com task rsync/scp pelo `Executor` ssh; vira passo próprio só se o job não bastar. |
| Templates do init por git-url | Motor do init (`forge/scaffold.py`): a fonte de arquétipo/receita deixa de ser só o pacote e aceita um clone raso; o contrato `recipe.yaml` é o mesmo. |
| bind / generate (adotar recurso que já existe; gerar recurso a partir do vivo) | Métodos novos no `Adapter` (`bind`, `generate`), opcionais por tipo de recurso. |
| Scheduler por GitHub Actions cron como `Adapter` de job | A receita `github-actions-cron` já agenda um comando no GitHub Actions, mas à mão: o workflow é um arquivo gerado uma vez, fora do `forge.yaml`. Fica de fora o `Adapter` de job alternativo ao prefect: `up` geraria `.github/workflows/<job>.yml` com `trigger.schedule.cron` chamando `forge run`, e o job ganharia `scheduler: prefect\|gha`. |
| Outros proxies de route (traefik, cloud load balancer) | `kind` novo em `targets.<alvo>.proxy` e um render em `forge/adapters/proxy.py`; o resto (arquivo por route, reload, limpeza pelo cabeçalho) não muda. |
| Remoção de bucket/database no `destroy --data` | Campo `drop: [argv]` opcional no storage, só honrado em development; hoje o forge só remove volume. |
| Outros providers de segredo (vault, cloud secret managers) | `SecretsProvider` novo; enquanto não existe, o provider `exec` cobre qualquer cofre com um script que devolve JSON. |
| `plan` em serviço com `env_file` | Observado na compose 2.37 (02/10/2026): `config --hash` não reproduz a label `config-hash` que o `up` grava quando o serviço tem `env_file`, e o `plan` diz "atualizar" sem mudança (num projeto com 13 serviços, os 3 com `env_file` divergiram e os 10 sem bateram). Encaixa em `desired_hashes`, comparando por outro meio só esses serviços; até lá, a divergência neles não prova mudança. |
