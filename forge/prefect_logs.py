"""A rodada do Prefect lida pela API: achar a rodada, ler os logs, seguir ate o fim.

O transporte e injetado, entao a suite roda offline e o teste ve o corpo enviado. Os
caminhos e as formas sao do servidor do Prefect: flow_runs/filter para a rodada e
logs/filter para os registros, filtrados por flow_run_id e por nivel.
"""

from __future__ import annotations

import base64
import functools
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from forge.errors import ForgeError

INFO_LEVEL = 20
API_TIMEOUT = 15.0
API_POLL_SECONDS = 2.0
LOG_PAGE = 200
LOG_PAGES = 50
"""Teto de paginas: um servidor que ignore o offset nao pode prender o comando."""
LOG_SORT = "TIMESTAMP_ASC"
FINISHED_TYPES = frozenset({"COMPLETED", "FAILED", "CRASHED", "CANCELLED"})
IN_COURSE_TYPES = ("PENDING", "RUNNING", "CANCELLING", "PAUSED")
"""Em curso; SCHEDULED fica de fora: e agenda, ainda nao e rodada."""


class PrefectLogsError(ForgeError):
    pass


class FollowDeadline(PrefectLogsError):
    """O teto de acompanhar venceu com a rodada ainda em curso: quem desistiu foi o leitor."""


class Transport(Protocol):
    def __call__(self, url: str, payload: Mapping[str, object], /) -> object: ...


@dataclass(frozen=True)
class FlowRun:
    id: str
    name: str
    state_type: str
    state_name: str

    @property
    def finished(self) -> bool:
        return self.state_type.upper() in FINISHED_TYPES

    @property
    def ok(self) -> bool:
        return self.state_type.upper() == "COMPLETED"


@dataclass(frozen=True)
class LogRecord:
    id: str
    logger: str
    message: str
    timestamp: datetime

    @property
    def key(self) -> str:
        """A identidade do registro e o id do Prefect; sem ele, a propria linha."""
        return self.id or f"{self.timestamp.isoformat()}|{self.logger}|{self.message}"


def _address(api_url: str, path: str) -> str:
    return api_url.rstrip("/") + "/" + path


def _shown(url: str) -> str:
    """O que de uma URL pode ir para a mensagem: sem usuario, senha, consulta nem fragmento."""
    parts = urllib.parse.urlsplit(url)
    if not parts.hostname:
        return parts.path
    port = ":" + str(parts.port) if parts.port else ""
    return parts.scheme + "://" + parts.hostname + port + parts.path


def auth_headers(userinfo: str, env: Mapping[str, str]) -> dict[str, str]:
    """Como o cliente do Prefect: PREFECT_API_AUTH_STRING (ou usuario:senha na URL) vira
    Basic; sem ele, PREFECT_API_KEY vira Bearer."""
    basic = env.get("PREFECT_API_AUTH_STRING") or userinfo
    if basic:
        return {
            "Authorization": "Basic " + base64.b64encode(basic.encode("utf-8")).decode()
        }
    key = env.get("PREFECT_API_KEY")
    return {"Authorization": f"Bearer {key}"} if key else {}


def post_json(
    url: str, payload: Mapping[str, object], env: Mapping[str, str] | None = None
) -> object:
    """Posta JSON e devolve a resposta decodificada; erro de transporte vira erro do forge."""
    parts = urllib.parse.urlsplit(url)
    userinfo, _, host = parts.netloc.rpartition("@")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers |= auth_headers(urllib.parse.unquote(userinfo), env or {})
    try:
        request = urllib.request.Request(
            urllib.parse.urlunsplit(parts._replace(netloc=host)),
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise PrefectLogsError(
            _shown(url) + ": a API do Prefect respondeu " + str(error.code)
        ) from error
    except (urllib.error.URLError, OSError) as error:
        raise PrefectLogsError(
            _shown(url) + ": a API do Prefect nao respondeu (" + str(error) + ")"
        ) from error
    except json.JSONDecodeError as error:
        raise PrefectLogsError(
            _shown(url) + ": a API do Prefect respondeu algo ilegivel"
        ) from error
    except ValueError as error:
        raise PrefectLogsError(
            _shown(url) + ": endereco invalido (" + str(error) + ")"
        ) from error


def transport_for(env: Mapping[str, str]) -> Transport:
    """O transporte HTTP com a autenticacao que o envelope do alvo declara."""
    return functools.partial(post_json, env=dict(env))


@dataclass
class PrefectLogs:
    api_url: str
    transport: Transport = post_json
    poll_seconds: float = API_POLL_SECONDS
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    timeout: float | None = None

    def current_flow_run(self, deployment: str, flow_name: str) -> FlowRun | None:
        """A rodada em curso do deployment; sem nenhuma em curso, a ultima que comecou."""
        in_course = {"state": {"type": {"any_": list(IN_COURSE_TYPES)}}}
        started = {"start_time": {"is_null_": False}}
        return self._first_run(
            deployment, flow_name, in_course, "EXPECTED_START_TIME_DESC"
        ) or (self._first_run(deployment, flow_name, started, "START_TIME_DESC"))

    def _first_run(
        self, deployment: str, flow_name: str, where: Mapping[str, object], sort: str
    ) -> FlowRun | None:
        payload = {
            "limit": 1,
            "sort": sort,
            "flows": {"name": {"any_": [flow_name]}},
            "deployments": {"name": {"any_": [deployment]}},
            "flow_runs": dict(where),
        }
        found = self._list("flow_runs/filter", payload)
        return self._run(found[0]) if found else None

    def run(self, flow_run_id: str) -> FlowRun:
        found = self._list(
            "flow_runs/filter",
            {"limit": 1, "flow_runs": {"id": {"any_": [flow_run_id]}}},
        )
        if not found:
            raise PrefectLogsError("a rodada " + flow_run_id + " nao existe no Prefect")
        return self._run(found[0])

    def records(self, flow_run_id: str) -> list[LogRecord]:
        """Todas as paginas do filtro: sem o offset, o rabo de uma rodada longa ficaria de fora."""
        found: list[LogRecord] = []
        for _ in range(LOG_PAGES):
            page = self._list("logs/filter", self._filter(flow_run_id, len(found)))
            found += [_record(item) for item in page]
            if len(page) < LOG_PAGE:
                break
        return found

    @staticmethod
    def _filter(flow_run_id: str, offset: int) -> dict[str, object]:
        return {
            "limit": LOG_PAGE,
            "offset": offset,
            "sort": LOG_SORT,
            "logs": {
                "flow_run_id": {"any_": [flow_run_id]},
                "level": {"ge_": INFO_LEVEL},
            },
        }

    def follow(
        self,
        flow_run_id: str,
        *,
        show: Callable[[LogRecord], None],
        timeout: float | None = None,
    ) -> FlowRun:
        """Mostra cada registro novo e so volta quando a rodada termina."""
        limit = self.timeout if timeout is None else timeout
        deadline = None if limit is None else self.clock() + limit
        seen: set[str] = set()
        while True:
            run = self.run(flow_run_id)
            self._show_new(flow_run_id, seen, show)
            if run.finished:
                # o processo da rodada esvazia a fila de log ao sair, logo depois do estado final
                self.sleep(self.poll_seconds)
                self._show_new(flow_run_id, seen, show)
                return run
            if deadline is not None and self.clock() >= deadline:
                raise FollowDeadline(
                    "a rodada " + flow_run_id + " nao terminou em " + str(limit)
                    + "s; parei de acompanhar"
                )  # fmt: skip
            self.sleep(self.poll_seconds)

    def _show_new(
        self, flow_run_id: str, seen: set[str], show: Callable[[LogRecord], None]
    ) -> None:
        for record in self.records(flow_run_id):
            if record.key not in seen:
                seen.add(record.key)
                show(record)

    def _list(self, path: str, payload: Mapping[str, object]) -> list[dict[str, Any]]:
        url = _address(self.api_url, path)
        answer = self.transport(url, payload)
        if not isinstance(answer, list):
            raise PrefectLogsError(
                _shown(url) + ": esperava uma lista de registros do Prefect"
            )
        return [item for item in answer if isinstance(item, dict)]

    @staticmethod
    def _run(item: Mapping[str, Any]) -> FlowRun:
        state = item.get("state")
        inside: Mapping[str, Any] = state if isinstance(state, Mapping) else {}
        return FlowRun(
            id=str(item.get("id") or ""),
            name=str(item.get("name") or ""),
            state_type=str(inside.get("type") or ""),
            state_name=str(inside.get("name") or ""),
        )


def _record(item: Mapping[str, Any]) -> LogRecord:
    stamp = str(item.get("timestamp") or "")
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError as error:
        raise PrefectLogsError(
            f"registro do Prefect com hora ilegivel: {stamp!r}"
        ) from error
    return LogRecord(
        id=str(item.get("id") or ""),
        logger=str(item.get("name") or ""),
        message=str(item.get("message") or ""),
        timestamp=when,
    )
