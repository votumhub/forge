"""`forge run --seguir` e `forge seguir`: a rodada do Prefect lida pela API, sem rede.

O dublê responde como o servidor do Prefect (filtra por estado, por início, por nível e por
página) e guarda cada corpo enviado, então o teste cobra o contrato de verdade. Onde importa o
caminho HTTP (autenticação, erro sem credencial), o mesmo dublê é servido por HTTP local.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from forge.adapters.prefect import created_run_id
from forge.cli import VERBS, build_parser, main
from forge.commands import run as run_cmd
from forge.commands import seguir as seguir_cmd
from forge.commands.run import (
    Screen,
    api_for,
    follow_current,
    follow_job,
    follow_stream,
    follow_run,
    terminal,
)
from forge.errors import ForgeError
from forge.load import Loaded, load
from forge.model import Manifest, ManifestError, parse
from forge.prefect_logs import (
    IN_COURSE_TYPES,
    LOG_PAGE,
    LOG_PAGES,
    PrefectLogs,
    PrefectLogsError,
    auth_headers,
)
from forge.testing import FakeExecutor
from tests.support import Writer, manifest

API = "http://prefect.internal:4200/api"
SECRET_API = "http://user:token-xyz@prefect.internal:4200/api"
JOB = "daily_load"
DEPLOYMENT = "daily_load-dev"
FLOW = "daily-load"
RUN_ID = "0f7c2c6e-1a2b-4c3d-8e9f-0123456789ab"
RUNS_PATH = "flow_runs/filter"
LOGS_PATH = "logs/filter"
CLI_OUTPUT = f"""Creating flow run for deployment '{FLOW}/{DEPLOYMENT}'...
Created flow run 'crimson-dingo'.
└── UUID: {RUN_ID}
└── Parameters: {{}}
└── Scheduled start time: 2026-09-25 12:00:00 UTC (now)
└── URL: <no dashboard available>
"""


def state(kind: str, run_id: str = RUN_ID) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": "crimson-dingo",
        "state": {"type": kind, "name": kind.title()},
    }


COMPLETED, FAILED, RUNNING = state("COMPLETED"), state("FAILED"), state("RUNNING")
PENDING, CANCELLED, SCHEDULED = state("PENDING"), state("CANCELLED"), state("SCHEDULED")


def log(
    message: str,
    *,
    logger: str = "toy.painel",
    at: str = "12:00:01",
    level: int = 20,
    ident: str | None = None,
) -> dict[str, Any]:
    return {
        "id": ident or f"log-{logger}-{at}-{message}",
        "name": logger,
        "level": level,
        "message": message,
        "timestamp": f"2026-09-25T{at}+00:00",
        "flow_run_id": RUN_ID,
    }


class Fake:
    """A API do Prefect: responde pelo caminho e pelo filtro, como o servidor."""

    def __init__(
        self,
        *,
        state: dict[str, Any] | None = None,
        script: list[dict[str, Any]] | None = None,
        runs: list[dict[str, Any]] | None = None,
        records: list[dict[str, Any]] | None = None,
        arriving: dict[int, list[dict[str, Any]]] | None = None,
        present: bool = True,
    ) -> None:
        self.state = COMPLETED if state is None else state
        self.script = list(script or [])
        """Estados das próximas leituras da rodada; depois dele, fica o último."""
        self.runs = [COMPLETED] if runs is None else runs
        """As rodadas do deployment, da mais recente para a mais antiga."""
        self.records = list(records or [])
        self.arriving = dict(arriving or {})
        """Leitura de logs (1, 2, ...) -> registros que chegam nela, como o lote atrasado."""
        self.present = present
        self.sent: list[tuple[str, Mapping[str, Any]]] = []
        self.state_calls = 0
        self.log_reads = 0

    def __call__(self, url: str, payload: Mapping[str, Any]) -> object:
        self.sent.append((url, payload))
        if url.endswith(LOGS_PATH):
            return self._logs(payload)
        where = payload["flow_runs"]
        if "id" in where:
            return self._state()
        if "state" in where:
            wanted = where["state"]["type"]["any_"]
            return [run for run in self.runs if run["state"]["type"] in wanted][:1]
        assert where == {"start_time": {"is_null_": False}}
        started = [
            r for r in self.runs if r["state"]["type"] not in ("SCHEDULED", "PENDING")
        ]
        return started[:1]

    def _state(self) -> list[dict[str, Any]]:
        self.state_calls += 1
        if self.script:
            self.state = self.script.pop(0)
        return [self.state] if self.present else []

    def _logs(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        offset, limit = payload["offset"], payload["limit"]
        if offset == 0:
            self.log_reads += 1
            self.records += self.arriving.pop(self.log_reads, [])
        floor = payload["logs"]["level"]["ge_"]
        visible = sorted(
            (record for record in self.records if record["level"] >= floor),
            key=lambda record: str(record["timestamp"]),
        )
        return visible[offset : offset + limit]

    def queries(self) -> list[Mapping[str, Any]]:
        return [payload for url, payload in self.sent if url.endswith(RUNS_PATH)]


def reader(fake: object, **kwargs: Any) -> PrefectLogs:
    return PrefectLogs(
        API, transport=fake, poll_seconds=0.0, sleep=lambda _s: None, **kwargs
    )  # type: ignore[arg-type]


def bundle(
    write_bundle: Writer,
    *,
    api: str | None = API,
    logger: str | None = "toy.painel",
    timezone: str | None = None,
    cabecalho: str | None = None,
) -> Path:
    extra: dict[str, object] = {
        "include": ["resources/*.yaml"],
        "env": {} if api is None else {"PREFECT_API_URL": api},
    }
    if logger is not None:
        extra["seguir"] = {"logger": logger} | (
            {"cabecalho": cabecalho} if cabecalho else {}
        )
    trigger: dict[str, object] = (
        {"manual": True}
        if timezone is None
        else {"schedule": {"cron": "*/5 * * * *", "timezone": timezone}}
    )
    job = {"trigger": trigger, "flow": f"src/x/flows.py:{JOB}", "work_pool": "p"}
    return write_bundle(manifest(**extra), {f"resources/{JOB}.job.yaml": job})


def loaded(write_bundle: Writer, **kwargs: Any) -> Loaded:
    return load(bundle(write_bundle, **kwargs), "dev", environ={}, release="r1")


def executor() -> FakeExecutor:
    made = FakeExecutor()
    made.respond(["prefect", "deployment", "run"], output=CLI_OUTPUT)
    return made


def screen() -> Screen:
    return Screen(io.StringIO(), io.StringIO())


def shown(where: Screen) -> tuple[str, str]:
    out, err = where.out, where.err
    assert isinstance(out, io.StringIO) and isinstance(err, io.StringIO)
    return out.getvalue(), err.getvalue()


# --- o leitor: qual rodada -------------------------------------------------------------


ONGOING_QUERY = {
    "limit": 1,
    "sort": "EXPECTED_START_TIME_DESC",
    "flows": {"name": {"any_": [FLOW]}},
    "deployments": {"name": {"any_": [DEPLOYMENT]}},
    "flow_runs": {"state": {"type": {"any_": list(IN_COURSE_TYPES)}}},
}
STARTED_QUERY = {
    **ONGOING_QUERY,
    "sort": "START_TIME_DESC",
    "flow_runs": {"start_time": {"is_null_": False}},
}


def test_current_run_asks_first_for_one_in_course_then_for_the_last_started() -> None:
    fake = Fake(runs=[COMPLETED])
    assert reader(fake).current_flow_run(DEPLOYMENT, FLOW) == PrefectLogs._run(
        COMPLETED
    )
    assert [url for url, _ in fake.sent] == [f"{API}/{RUNS_PATH}"] * 2
    assert fake.queries() == [ONGOING_QUERY, STARTED_QUERY]


def test_current_run_prefers_one_in_course_over_the_last() -> None:
    fake = Fake(runs=[COMPLETED, RUNNING])
    assert reader(fake).current_flow_run(DEPLOYMENT, FLOW).state_type == "RUNNING"  # type: ignore[union-attr]
    assert fake.queries() == [ONGOING_QUERY]


def test_a_pending_run_counts_as_in_course() -> None:
    fake = Fake(runs=[COMPLETED, PENDING])
    assert reader(fake).current_flow_run(DEPLOYMENT, FLOW).state_type == "PENDING"  # type: ignore[union-attr]


def test_a_scheduled_run_is_never_the_current_one() -> None:
    """A agenda cria rodadas futuras; seguir uma delas seria esperar a hora dela chegar."""
    assert "SCHEDULED" not in IN_COURSE_TYPES
    fake = Fake(runs=[SCHEDULED, COMPLETED])
    assert reader(fake).current_flow_run(DEPLOYMENT, FLOW).state_type == "COMPLETED"  # type: ignore[union-attr]
    assert reader(Fake(runs=[SCHEDULED])).current_flow_run(DEPLOYMENT, FLOW) is None


def test_deployment_without_any_run_is_none_not_an_error() -> None:
    assert reader(Fake(runs=[])).current_flow_run(DEPLOYMENT, FLOW) is None


def test_missing_run_is_named_as_not_found() -> None:
    with pytest.raises(PrefectLogsError, match="nao existe"):
        reader(Fake(present=False)).run(RUN_ID)


def test_a_transport_that_answers_a_mapping_is_refused() -> None:
    with pytest.raises(PrefectLogsError, match="lista"):
        reader(lambda _url, _payload: {"detail": "no"}).current_flow_run(
            DEPLOYMENT, FLOW
        )


# --- o leitor: os registros ------------------------------------------------------------


def test_logs_come_from_the_api_filtered_by_flow_run_and_level() -> None:
    fake = Fake(
        records=[log("primeira"), log("segunda", level=30), log("barulho", level=10)]
    )
    records = reader(fake).records(RUN_ID)
    url, payload = fake.sent[-1]
    assert url == f"{API}/{LOGS_PATH}"
    assert payload["logs"] == {"flow_run_id": {"any_": [RUN_ID]}, "level": {"ge_": 20}}
    assert payload["sort"] == "TIMESTAMP_ASC"
    assert [record.message for record in records] == ["primeira", "segunda"]
    assert records[0].logger == "toy.painel"


def test_logs_are_read_page_by_page_until_the_end_of_the_run() -> None:
    fake = Fake(
        records=[
            log(f"linha {i}", at=f"12:{i // 60:02d}:{i % 60:02d}")
            for i in range(LOG_PAGE + 15)
        ]
    )
    assert len(reader(fake).records(RUN_ID)) == LOG_PAGE + 15
    offsets = [
        payload["offset"] for url, payload in fake.sent if url.endswith(LOGS_PATH)
    ]
    assert offsets == [0, LOG_PAGE]


def test_a_short_page_ends_the_reading() -> None:
    fake = Fake(records=[log("so uma")])
    reader(fake).records(RUN_ID)
    assert len([url for url, _ in fake.sent if url.endswith(LOGS_PATH)]) == 1


def test_a_server_that_ignores_the_offset_stops_at_the_page_cap() -> None:
    page = [log(f"linha {i}", ident=f"log-{i}") for i in range(LOG_PAGE)]
    calls: list[object] = []

    def stuck(_url: str, payload: Mapping[str, object]) -> object:
        calls.append(payload)
        return page

    assert len(reader(stuck).records(RUN_ID)) == LOG_PAGE * LOG_PAGES
    assert len(calls) == LOG_PAGES


def test_a_record_with_an_unreadable_hour_is_refused() -> None:
    fake = Fake(records=[{**log("sem hora"), "timestamp": "ontem"}])
    with pytest.raises(PrefectLogsError, match="hora ilegivel"):
        reader(fake).records(RUN_ID)


# --- o leitor: seguir até o fim --------------------------------------------------------


def test_follow_shows_every_record_and_returns_the_final_state() -> None:
    fake = Fake(records=[log("linha um")], state=COMPLETED)
    seen: list[str] = []
    run = reader(fake).follow(RUN_ID, show=lambda record: seen.append(record.message))
    assert run.state_type == "COMPLETED" and run.finished and run.ok
    assert seen == ["linha um"]


def test_follow_of_a_failed_run_says_failed() -> None:
    run = reader(Fake(state=FAILED)).follow(RUN_ID, show=lambda _record: None)
    assert run.state_type == "FAILED" and run.finished and not run.ok


def test_follow_keeps_polling_while_running_then_reads_once_more_after_the_end() -> (
    None
):
    """Depois do estado final há uma leitura a mais: o processo da rodada esvazia a fila de log
    ao sair, e a última linha do projeto chega nesse instante."""
    fake = Fake(state=RUNNING)
    engine = reader(fake)
    polls: list[float] = []

    def go_to_sleep(seconds: float) -> None:
        polls.append(seconds)
        fake.state = COMPLETED

    engine.sleep = go_to_sleep
    assert engine.follow(RUN_ID, show=lambda _record: None).state_type == "COMPLETED"
    assert polls == [0.0, 0.0]
    assert fake.state_calls == 2


def test_follow_gives_up_when_the_run_never_finishes() -> None:
    """Acompanhar sem teto penduraria a linha de comando; o teto e a recusa."""
    engine = reader(Fake(state=RUNNING))
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    engine.clock = lambda: next(ticks)
    engine.timeout = 3.0
    with pytest.raises(PrefectLogsError, match="nao terminou"):
        engine.follow(RUN_ID, show=lambda _record: None)


def test_follow_reports_equal_messages_once_per_record() -> None:
    """Duas linhas iguais sao dois registros: a identidade e o id, nao o texto."""
    fake = Fake(records=[log("igual", ident="log-1"), log("igual", ident="log-2")])
    seen: list[str] = []
    reader(fake).follow(RUN_ID, show=lambda record: seen.append(record.message))
    assert seen == ["igual", "igual"]


def test_follow_does_not_repeat_a_record_across_polls() -> None:
    fake = Fake(
        records=[log("unica", ident="log-1")], script=[RUNNING, RUNNING, COMPLETED]
    )
    seen: list[str] = []
    reader(fake).follow(RUN_ID, show=lambda record: seen.append(record.message))
    assert seen == ["unica"]


def test_late_record_and_the_tail_after_the_final_state_show_once() -> None:
    fake = Fake(
        script=[RUNNING, COMPLETED],
        records=[log("primeiro a chegar", at="12:00:10")],
        arriving={
            2: [log("chegou atrasado", at="12:00:05")],
            3: [log("depois do estado final", at="12:00:12")],
        },
    )
    seen: list[str] = []
    reader(fake).follow(RUN_ID, show=lambda record: seen.append(record.message))
    assert seen == ["primeiro a chegar", "chegou atrasado", "depois do estado final"]


# --- o transporte HTTP -----------------------------------------------------------------


@pytest.fixture
def served() -> Iterator[tuple[Fake, str, list[str | None]]]:
    """O mesmo dublê atrás de um HTTP local; guarda o Authorization de cada chamada."""
    fake = Fake(runs=[RUNNING])
    authorizations: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            authorizations.append(self.headers.get("Authorization"))
            size = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(size))
            known = self.path.startswith("/api/") and self.path.endswith(
                (RUNS_PATH, LOGS_PATH)
            )
            status, reply = (
                (200, fake(self.path, body)) if known else (404, {"detail": "no"})
            )
            data = json.dumps(reply).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield fake, f"127.0.0.1:{server.server_address[1]}", authorizations
    server.shutdown()
    server.server_close()


def test_credentials_in_the_url_go_as_basic_auth_not_in_the_address(
    write_bundle: Writer, served: tuple[Fake, str, list[str | None]]
) -> None:
    fake, host, authorizations = served
    engine = api_for(loaded(write_bundle, api=f"http://user:token-xyz@{host}/api"))
    assert engine.current_flow_run(DEPLOYMENT, FLOW) is not None
    assert authorizations == ["Basic " + base64.b64encode(b"user:token-xyz").decode()]
    assert [url for url, _ in fake.sent] == [f"/api/{RUNS_PATH}"]


def test_auth_follows_the_prefect_client() -> None:
    """PREFECT_API_AUTH_STRING vence usuario:senha na URL; sem os dois, PREFECT_API_KEY."""
    basic = "Basic " + base64.b64encode(b"a:b").decode()
    assert auth_headers("x:y", {"PREFECT_API_AUTH_STRING": "a:b"}) == {
        "Authorization": basic
    }
    assert auth_headers("", {"PREFECT_API_KEY": "k"}) == {"Authorization": "Bearer k"}
    assert auth_headers("", {}) == {}


def test_http_errors_never_show_the_credential(
    served: tuple[Fake, str, list[str | None]],
) -> None:
    _, host, _ = served
    answering = PrefectLogs(f"http://user:token-xyz@{host}/nada")
    with pytest.raises(PrefectLogsError, match="respondeu 404") as refused:
        answering.run(RUN_ID)
    silent = PrefectLogs("http://user:token-xyz@127.0.0.1:0/api")
    with pytest.raises(PrefectLogsError, match="nao respondeu") as unreachable:
        silent.run(RUN_ID)
    assert "token-xyz" not in f"{refused.value} {unreachable.value}"
    assert f"http://{host}/nada" in str(refused.value)


def test_the_failure_message_does_not_carry_the_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credencial nao sai nem no caminho de falha: a URL vai sem usuario e sem consulta."""

    def refuse(_request: object, timeout: float = 0.0) -> object:
        raise urllib.error.URLError("sem rota")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(PrefectLogsError) as caught:
        PrefectLogs(SECRET_API).current_flow_run(DEPLOYMENT, FLOW)
    assert "token-xyz" not in str(caught.value)
    assert "prefect.internal:4200" in str(caught.value)


def test_an_address_the_url_opener_refuses_does_not_escape_as_a_traceback() -> None:
    """Endereco sem esquema vira ValueError dentro do urllib: a saida tem que ser erro do forge."""
    with pytest.raises(PrefectLogsError, match="endereco invalido"):
        PrefectLogs("").current_flow_run(DEPLOYMENT, FLOW)


# --- o endereço: do envelope resolvido --------------------------------------------------


def secured_bundle(write_bundle: Writer) -> Path:
    """O `PREFECT_API_URL` do alvo como ele nasce no arquetipo de automacao."""
    data = manifest(
        include=["resources/*.yaml"],
        env={"PREFECT_API_URL": "secret://PREFECT_API_URL"},
    )
    data["targets"]["dev"]["secrets"] = {"provider": "dotenv", "path": "s.env"}  # type: ignore[index]
    job = {
        "trigger": {"manual": True},
        "flow": f"src/x/flows.py:{JOB}",
        "work_pool": "p",
    }
    return write_bundle(
        data,
        {"s.env": f"PREFECT_API_URL={SECRET_API}\n", f"resources/{JOB}.job.yaml": job},
    )


def test_the_reader_points_at_the_url_resolved_from_the_secret(
    write_bundle: Writer,
) -> None:
    """O `secret://` do alvo vira o endereco de verdade, a mesma fonte do envio do job."""
    engine = api_for(
        load(secured_bundle(write_bundle), "dev", environ={}, release="r1")
    )
    assert engine.api_url == SECRET_API


def test_the_reader_refuses_a_target_without_the_url(write_bundle: Writer) -> None:
    with pytest.raises(ForgeError, match="sem PREFECT_API_URL"):
        api_for(loaded(write_bundle, api=None))


# --- run --seguir -----------------------------------------------------------------------


def test_run_seguir_follows_the_run_it_created_and_shows_only_the_declared_logger(
    write_bundle: Writer,
) -> None:
    fake = Fake(
        script=[RUNNING, RUNNING, COMPLETED],
        records=[
            log("Beginning flow run", logger="prefect.flow_runs", at="12:00:00"),
            log("ciclo  rodada  início", at="12:00:01"),
        ],
        arriving={
            2: [
                log("cliente 9001 ok", logger="toy.detalhe", at="12:00:02"),
                log("ciclo  extração  fim", logger="toy.painel.passo", at="12:00:03"),
                log("vizinho de nome", logger="toy.painelx", at="12:00:03"),
            ],
            3: [log("ciclo  rodada  fim  ok", at="12:00:05")],
        },
    )
    made, where = executor(), screen()
    code = follow_job(
        loaded(write_bundle), JOB, made, reader=reader(fake), screen=where
    )
    out, err = shown(where)
    assert code == 0
    assert out == (
        "12:00:01  ciclo  rodada  início\n"
        "12:00:03  ciclo  extração  fim\n"
        "12:00:05  ciclo  rodada  fim  ok\n"
    )
    assert made.argvs() == [("prefect", "deployment", "run", f"{FLOW}/{DEPLOYMENT}")]
    assert made.calls[0].env["PYTHONIOENCODING"] == "utf-8"
    assert (
        "Created flow run 'crimson-dingo'." in err
    )  # a resposta do disparo, fora da tela
    assert f"rodada crimson-dingo ({RUN_ID}) do job {JOB} em dev" in err
    assert err.endswith("rodada crimson-dingo: Completed\n")
    # a rodada seguida é a criada, pelo id: nunca uma busca pelo deployment
    assert all("id" in query["flow_runs"] for query in fake.queries())
    assert {query["flow_runs"]["id"]["any_"][0] for query in fake.queries()} == {RUN_ID}


@pytest.mark.parametrize("final", [FAILED, CANCELLED])
def test_run_seguir_exits_1_when_the_run_does_not_complete(
    write_bundle: Writer, final: dict[str, Any]
) -> None:
    fake = Fake(script=[RUNNING, final], records=[log("ciclo  rodada  fim  falhou")])
    where = screen()
    assert (
        follow_job(
            loaded(write_bundle), JOB, executor(), reader=reader(fake), screen=where
        )
        == 1
    )
    out, err = shown(where)
    assert out == "12:00:01  ciclo  rodada  fim  falhou\n"
    assert err.endswith(f"rodada crimson-dingo: {final['state']['name']}\n")


def test_run_seguir_without_api_url_refuses_before_creating_the_run(
    write_bundle: Writer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    made = FakeExecutor()
    monkeypatch.setattr(run_cmd, "LocalExecutor", lambda: made)
    root = bundle(write_bundle, api=None)
    assert main(["--root", str(root), "run", "-t", "dev", JOB, "--seguir"]) == 2
    assert "sem PREFECT_API_URL" in capsys.readouterr().err
    assert made.calls == []


def test_run_seguir_gives_up_at_the_timeout_without_calling_it_a_failure(
    write_bundle: Writer,
) -> None:
    """O teto venceu para quem acompanha, nao para a rodada: codigo 3, e a frase diz isso."""
    engine = reader(Fake(state=RUNNING))
    ticks = iter([0.0, 5.0, 10.0])
    engine.clock = lambda: next(ticks)
    where = screen()
    code = follow_job(loaded(write_bundle), JOB, executor(), reader=engine, screen=where,
                      timeout=5.0)  # fmt: skip
    assert code == 3
    _out, err = shown(where)
    assert "nao terminou em 5.0s" in err and "não é falha dela" in err


def test_ctrl_c_stops_following_with_130_and_leaves_the_run(
    write_bundle: Writer,
) -> None:
    engine = reader(Fake(state=RUNNING, records=[log("ciclo  rodada  início")]))

    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    engine.sleep = interrupt
    made, where = executor(), screen()
    assert (
        follow_job(loaded(write_bundle), JOB, made, reader=engine, screen=where) == 130
    )
    out, err = shown(where)
    assert out == "12:00:01  ciclo  rodada  início\n"
    assert err.endswith("acompanhamento interrompido; a rodada continua no Prefect\n")
    assert [argv[:3] for argv in made.argvs()] == [("prefect", "deployment", "run")]


def test_run_id_comes_from_the_cli_output() -> None:
    assert created_run_id(CLI_OUTPUT) == RUN_ID
    with pytest.raises(ForgeError, match="UUID"):
        created_run_id("Created flow run 'crimson-dingo'.")


# --- a tela -----------------------------------------------------------------------------


def follow_on_screen(write_bundle: Writer, fake: Fake, **kwargs: Any) -> str:
    where = screen()
    follow_run(
        loaded(write_bundle, **kwargs), JOB, RUN_ID, reader=reader(fake), screen=where
    )
    return shown(where)[0]


def test_the_screen_shows_only_the_message_without_level_or_utc_stamp(
    write_bundle: Writer,
) -> None:
    fake = Fake(records=[log("caiu", level=40), log("aviso", level=30, at="12:00:02")])
    assert follow_on_screen(write_bundle, fake) == "12:00:01  caiu\n12:00:02  aviso\n"


def test_without_seguir_block_everything_but_prefect_shows(
    write_bundle: Writer,
) -> None:
    fake = Fake(
        records=[
            log("raiz do prefect", logger="prefect", at="12:00:00"),
            log("Beginning flow run", logger="prefect.flow_runs", at="12:00:01"),
            log("cliente 9001 ok", logger="toy.detalhe", at="12:00:02"),
            log("nome parecido, outro logger", logger="prefeitura", at="12:00:03"),
        ]
    )
    assert follow_on_screen(write_bundle, fake, logger=None) == (
        "12:00:02  cliente 9001 ok\n12:00:03  nome parecido, outro logger\n"
    )


def test_hour_is_shown_in_the_job_timezone(write_bundle: Writer) -> None:
    fake = Fake(records=[{**log("ciclo  extração  andamento"),
                          "timestamp": "2026-09-25T14:55:20.123456Z"}])  # fmt: skip
    out = follow_on_screen(write_bundle, fake, timezone="America/Sao_Paulo")
    assert out == "11:55:20  ciclo  extração  andamento\n"


def test_header_is_printed_once_aligned_with_the_messages(write_bundle: Writer) -> None:
    fake = Fake(
        script=[RUNNING, COMPLETED],
        records=[log("ciclo  rodada  início")],
        arriving={2: [log("ciclo  rodada  fim", at="12:00:02")]},
    )
    out = follow_on_screen(write_bundle, fake, cabecalho="FLUXO  PASSO   EVENTO")
    header, first, second = out.splitlines()
    assert header == "HORA      FLUXO  PASSO   EVENTO"
    assert header.index("FLUXO") == first.index("ciclo") == second.index("ciclo")


def test_the_terminal_writes_utf8_even_on_a_cp1252_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows fora do console (pipe, arquivo, Git Bash) abre o stdout em cp1252."""
    raw = io.BytesIO()
    monkeypatch.setattr(
        sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252", newline="\n")
    )
    monkeypatch.setattr(
        sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    )
    where = terminal()
    print("312 arquivos → 52", file=where.out, flush=True)
    assert raw.getvalue().decode("utf-8") == "312 arquivos → 52\n"


# --- o verbo seguir ---------------------------------------------------------------------


def test_seguir_verb_follows_the_run_in_course(
    write_bundle: Writer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = Fake(
        runs=[COMPLETED, PENDING],
        script=[PENDING, RUNNING, COMPLETED],
        records=[log("ciclo  rodada  início")],
    )
    monkeypatch.setattr(seguir_cmd, "api_for", lambda _loaded: reader(fake))
    assert main(["--root", str(bundle(write_bundle)), "seguir", "-t", "dev", JOB]) == 0
    assert capsys.readouterr().out == "12:00:01  ciclo  rodada  início\n"
    assert fake.queries()[0] == ONGOING_QUERY


def test_seguir_verb_without_run_in_course_shows_the_last_started(
    write_bundle: Writer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = Fake(
        runs=[FAILED], state=FAILED, records=[log("ciclo  rodada  fim  falhou")]
    )
    monkeypatch.setattr(seguir_cmd, "api_for", lambda _loaded: reader(fake))
    assert main(["--root", str(bundle(write_bundle)), "seguir", "-t", "dev", JOB]) == 1
    assert capsys.readouterr().out == "12:00:01  ciclo  rodada  fim  falhou\n"
    assert fake.queries()[:2] == [ONGOING_QUERY, STARTED_QUERY]


def test_seguir_verb_without_any_run(
    write_bundle: Writer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(seguir_cmd, "api_for", lambda _loaded: reader(Fake(runs=[])))
    assert main(["--root", str(bundle(write_bundle)), "seguir", "-t", "dev", JOB]) == 2
    assert f"{DEPLOYMENT} ainda não tem rodada iniciada" in capsys.readouterr().err


class Rounds:
    """O Prefect com as rodadas do job chegando uma depois da outra: cada consulta da rodada
    atual devolve a próxima da fila; fila vazia, quem acompanha aperta Ctrl-C."""

    poll_seconds = 0.0

    def __init__(self, *ids: str | None) -> None:
        self.ids = list(ids)
        self.followed: list[str] = []

    def current_flow_run(self, deployment: str, flow_name: str) -> Any:
        assert (deployment, flow_name) == (DEPLOYMENT, FLOW)
        if not self.ids:
            raise KeyboardInterrupt
        run_id = self.ids.pop(0)
        return None if run_id is None else self.run(run_id)

    def run(self, run_id: str) -> Any:
        return reader(Fake())._run({**COMPLETED, "id": run_id})

    def follow(self, run_id: str, *, show: Any, timeout: float | None = None) -> Any:
        self.followed.append(run_id)
        return self.run(run_id)

    def sleep(self, _seconds: float) -> None:
        return None


def test_continuous_follows_each_new_run_once_until_ctrl_c(
    write_bundle: Writer,
) -> None:
    rounds = Rounds("a", "a", None, "b", "b", "c")
    where = screen()
    code = follow_stream(loaded(write_bundle), JOB, reader=rounds, screen=where)  # type: ignore[arg-type]
    assert (code, rounds.followed) == (130, ["a", "b", "c"])
    assert shown(where)[1].count("esperando a próxima rodada") == 3


def test_continuous_flag_is_registered() -> None:
    assert build_parser().parse_args(["seguir", JOB, "--continuo"]).continuo is True
    assert build_parser().parse_args(["seguir", JOB]).continuo is False


def test_follow_current_of_an_unknown_job_names_it(write_bundle: Writer) -> None:
    with pytest.raises(ForgeError, match="job nope não existe"):
        follow_current(loaded(write_bundle), "nope", reader=reader(Fake()))


def test_the_verbs_and_their_flags_are_registered() -> None:
    assert "seguir" in VERBS
    parser = build_parser()
    assert parser.parse_args(["run", JOB, "--seguir", "--timeout", "5"]).timeout == 5.0
    assert parser.parse_args(["seguir", JOB, "--timeout", "7"]).timeout == 7.0


# --- o manifesto ------------------------------------------------------------------------


def test_seguir_block_in_the_manifest() -> None:
    base = manifest()
    assert parse(
        Manifest, {**base, "seguir": {"logger": "toy.painel"}}, "forge.yaml"
    ).seguir
    with_header = {"logger": "toy.painel", "cabecalho": "FLUXO  PASSO"}
    parsed = parse(Manifest, {**base, "seguir": with_header}, "forge.yaml").seguir
    assert parsed is not None and parsed.cabecalho == "FLUXO  PASSO"
    with pytest.raises(ManifestError, match="seguir.nivel: campo desconhecido"):
        parse(
            Manifest,
            {**base, "seguir": {"logger": "toy", "nivel": "info"}},
            "forge.yaml",
        )
    with pytest.raises(ManifestError, match="seguir.logger"):
        parse(Manifest, {**base, "seguir": {"logger": "dois nomes"}}, "forge.yaml")
    with pytest.raises(ManifestError, match="seguir.cabecalho"):
        parse(
            Manifest,
            {**base, "seguir": {"logger": "x", "cabecalho": "a\nb"}},
            "forge.yaml",
        )
