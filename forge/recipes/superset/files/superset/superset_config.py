# Receita superset (bi) - gerada por `forge init --recipe superset`.
# Como conecta: montado no container de compose/superset.yaml e apontado por
#   SUPERSET_CONFIG_PATH; o Superset importa este arquivo na subida. Nada literal aqui: o
#   arquivo vai para o git e a URI do banco de metadados carrega senha.
# Variáveis (env do alvo no forge.yaml): SUPERSET_SECRET_KEY (segredo:
#   secret://SUPERSET_SECRET_KEY; gere uma vez com `openssl rand -base64 42`),
#   SUPERSET_DB_PASSWORD (segredo: secret://SUPERSET_DB_PASSWORD), SUPERSET_BASE_URL
#   (endereço público, usado em links de relatório); SUPERSET_DB_HOST vem do compose.
# dev x prd: cada alvo tem a própria SECRET_KEY e o próprio banco de metadados; a diferença
#   mora só no env do alvo.
# Regras: SUPERSET_SECRET_KEY nasce uma vez por alvo e nunca muda entre deploys, porque
#   assina os cookies de sessão e cifra as senhas das conexões salvas: trocar desloga todo
#   mundo e perde essas credenciais. A conexão que o Superset usa para ler os dados (criada
#   na UI) é uma role SOMENTE LEITURA, nunca a identidade que publica: SQL ad hoc é o
#   propósito da ferramenta e quem digita nem sempre é dono do dado.
# Documentação: https://superset.apache.org/docs/configuration/configuring-superset (superset 4.1)
from __future__ import annotations

import os

SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]

SQLALCHEMY_DATABASE_URI = (
    "postgresql+psycopg2://superset:"
    f"{os.environ['SUPERSET_DB_PASSWORD']}@"
    f"{os.environ.get('SUPERSET_DB_HOST', 'superset-db')}:5432/superset"
)

SQLALCHEMY_TRACK_MODIFICATIONS = False
WEBDRIVER_BASEURL = os.environ.get("SUPERSET_BASE_URL", "http://localhost:8088")
WEBDRIVER_BASEURL_USER_FRIENDLY = WEBDRIVER_BASEURL
