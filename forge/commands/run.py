"""forge run: dispara agora o deployment prefect de um job no alvo; com --seguir, acompanha.

Acompanhar (aqui com --seguir e no verbo `seguir`) é ler os logs da rodada pela API do Prefect
e imprimir só a mensagem, com a hora no fuso da agenda do job, até o estado final. Códigos de
saída: 0 a rodada terminou em Completed, 1 em outro estado final, 2 erro do forge e 130 quando
quem acompanha para com Ctrl-C (a rodada continua no Prefect).
"""

from __future__ import annotations

import argparse
import io
import sys
from dataclasses import dataclass, field
from datetime import UTC, tzinfo
from typing import TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from forge.adapters.prefect import (
    PrefectAdapter,
    created_run_id,
    deployment_name,
    flow_name,
)
from forge.commands._common import add_target_args, load_from
from forge.commands.env import deployed_images
from forge.contracts import DeployContext, Executor, RunResult
from forge.deploy import prepared_context
from forge.envelope import envelope_for
from forge.errors import ForgeError
from forge.executor import LocalExecutor
from forge.load import Loaded
from forge.model import Follow, Job
from forge.prefect_logs import FollowDeadline, LogRecord, PrefectLogs, transport_for

HIDDEN_BY_DEFAULT = "prefect"
HOUR_FORMAT = "%H:%M:%S"
GAP = "  "
HOUR_COLUMN = len("00:00:00" + GAP)
INTERRUPTED = 130
STOPPED_FOLLOWING = 3
"""Código de saída de quem para com Ctrl-C (128 + SIGINT), como no shell."""


def register(verbs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = verbs.add_parser("run", help="dispara um job agora")
    add_target_args(parser)
    parser.add_argument("job", help="nome do job (resources/<job>.job.yaml)")
    parser.add_argument("--yes", action="store_true", help="confirma run em production")
    parser.add_argument(
        "--seguir",
        action="store_true",
        help="acompanha o log da rodada criada até o estado final",
    )
    add_timeout_arg(parser)
    parser.set_defaults(handler=run)


def add_timeout_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="teto em segundos para acompanhar a rodada",
    )


@dataclass(frozen=True)
class Screen:
    """Mensagens da rodada no stdout; o que o próprio forge diz, no stderr."""

    out: TextIO = field(default_factory=lambda: sys.stdout)
    err: TextIO = field(default_factory=lambda: sys.stderr)


def terminal() -> Screen:
    """A tela do CLI em UTF-8: a mensagem é texto do projeto, e no Windows fora do console
    (pipe, arquivo, Git Bash) o Python escreve em cp1252 e quebra em `→`."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    return Screen()


def prefect_job(loaded: Loaded, job: str) -> Job:
    if job not in loaded.flow_jobs:
        known = ", ".join(sorted(loaded.flow_jobs)) or "nenhum"
        why = (
            "roda no deploy, não pelo Prefect"
            if job in loaded.jobs
            else "não existe no bundle"
        )
        raise ForgeError(f"job {job} {why} (jobs do Prefect: {known})")
    return loaded.flow_jobs[job]


def _check(loaded: Loaded, job: str, *, approved: bool) -> None:
    prefect_job(loaded, job)
    if loaded.mode == "production" and not approved:
        raise ForgeError(
            f"alvo {loaded.target_name} é production: confirme o run com --yes"
        )


def _context(
    loaded: Loaded, job: str, executor: Executor, *, approved: bool
) -> DeployContext:
    _check(loaded, job, approved=approved)
    return prepared_context(loaded, executor, deployed_images(loaded))


def run_job(
    loaded: Loaded, job: str, executor: Executor, *, approved: bool
) -> RunResult:
    ctx = _context(loaded, job, executor, approved=approved)
    return PrefectAdapter(loaded.name, loaded.flow_jobs).run(ctx, job, loaded.jobs[job])


def api_for(loaded: Loaded) -> PrefectLogs:
    """O leitor aponta para o `PREFECT_API_URL` do envelope resolvido, como o envio do job.

    Lido do overlay cru, o `secret://` do alvo chegaria ao urllib como endereco literal.
    """
    env = envelope_for(loaded, deployed_images(loaded)).values
    api = env.get("PREFECT_API_URL")
    if not api:
        raise ForgeError(
            f"alvo {loaded.target_name}: sem PREFECT_API_URL no envelope,"
            " não há como ler a rodada do Prefect"
        )
    return PrefectLogs(api, transport=transport_for(env))


def follow_run(
    loaded: Loaded,
    job: str,
    run_id: str,
    *,
    reader: PrefectLogs,
    screen: Screen | None = None,
    timeout: float | None = None,
) -> int:
    """Acompanha a rodada `run_id` até o estado final; 0 se terminou bem, 1 se não, 3 se o
    teto de acompanhar venceu e 130 se quem acompanha parou com Ctrl-C (nada é cancelado no
    Prefect, em nenhum dos dois)."""
    screen = screen or Screen()
    printer = _Printer(
        loaded.manifest.seguir, job_zone(prefect_job(loaded, job)), screen.out
    )
    try:
        started = reader.run(run_id)
        where = f"do job {job} em {loaded.target_name}"
        print(f"rodada {started.name} ({run_id}) {where}", file=screen.err, flush=True)
        printer.header()
        run = reader.follow(run_id, show=printer, timeout=timeout)
    except KeyboardInterrupt:
        print(
            "acompanhamento interrompido; a rodada continua no Prefect", file=screen.err
        )
        return INTERRUPTED
    except FollowDeadline as reason:
        print(
            f"{reason}; a rodada continua no Prefect e isso não é falha dela",
            file=screen.err,
        )
        return STOPPED_FOLLOWING
    print(f"rodada {run.name}: {run.state_name or run.state_type}", file=screen.err)
    return 0 if run.ok else 1


def follow_current(
    loaded: Loaded,
    job: str,
    *,
    reader: PrefectLogs,
    screen: Screen | None = None,
    timeout: float | None = None,
) -> int:
    """`seguir`: a rodada em curso do job (nunca a agendada); sem ela, a última que começou."""
    deployment = deployment_name(job, loaded.target_name)
    current = reader.current_flow_run(deployment, flow_name(prefect_job(loaded, job)))
    if current is None:
        raise ForgeError(
            f"job {job}: o deployment {deployment} ainda não tem rodada iniciada"
        )
    return follow_run(
        loaded, job, current.id, reader=reader, screen=screen, timeout=timeout
    )


def follow_stream(
    loaded: Loaded,
    job: str,
    *,
    reader: PrefectLogs,
    screen: Screen | None = None,
) -> int:
    """`seguir --continuo`: a rodada em curso (ou a última) e, a cada uma que termina, espera a
    próxima do job e a acompanha, até Ctrl-C (130; nada é cancelado no Prefect)."""
    screen = screen or Screen()
    deployment = deployment_name(job, loaded.target_name)
    flow = flow_name(prefect_job(loaded, job))
    last: str | None = None
    try:
        while True:
            current = reader.current_flow_run(deployment, flow)
            if current is None or current.id == last:
                reader.sleep(reader.poll_seconds)
                continue
            if (
                follow_run(loaded, job, current.id, reader=reader, screen=screen)
                == INTERRUPTED
            ):
                return INTERRUPTED
            last = current.id
            print(
                "esperando a próxima rodada (Ctrl-C sai)", file=screen.err, flush=True
            )
    except KeyboardInterrupt:
        print("acompanhamento interrompido", file=screen.err)
        return INTERRUPTED


def follow_job(
    loaded: Loaded,
    job: str,
    executor: Executor,
    *,
    approved: bool = False,
    reader: PrefectLogs,
    screen: Screen | None = None,
    timeout: float | None = None,
) -> int:
    """`run --seguir`: dispara e acompanha a rodada que o disparo criou, pelo id que o CLI
    devolve; procurar pelo deployment poderia pegar outra rodada da agenda."""
    screen = screen or Screen()
    started = run_job(loaded, job, executor, approved=approved)
    print(started.output.rstrip(), file=screen.err, flush=True)
    run_id = created_run_id(started.data)
    return follow_run(
        loaded, job, run_id, reader=reader, screen=screen, timeout=timeout
    )


def job_zone(job: Job) -> tzinfo:
    """O fuso da agenda do job; job sem agenda fica em UTC, o padrão da agenda."""
    name = job.trigger.schedule.timezone if job.trigger.schedule else "UTC"
    if name == "UTC":
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ForgeError(
            f"fuso {name} do job não existe neste sistema (falta tzdata?)"
        ) from error


def accepts(follow: Follow | None, logger: str) -> bool:
    """O logger declarado em `seguir.logger` e os filhos; sem o bloco, tudo que não é prefect.*."""
    if follow is None:
        return not _under(logger, HIDDEN_BY_DEFAULT)
    return _under(logger, follow.logger)


def _under(logger: str, parent: str) -> bool:
    return logger == parent or logger.startswith(parent + ".")


@dataclass(frozen=True)
class _Printer:
    follow: Follow | None
    zone: tzinfo
    out: TextIO

    def header(self) -> None:
        if self.follow is not None and self.follow.cabecalho is not None:
            print(
                f"{'HORA'.ljust(HOUR_COLUMN)}{self.follow.cabecalho}",
                file=self.out,
                flush=True,
            )

    def __call__(self, record: LogRecord) -> None:
        if accepts(self.follow, record.logger):
            hour = record.timestamp.astimezone(self.zone)
            print(
                f"{hour.strftime(HOUR_FORMAT)}{GAP}{record.message}",
                file=self.out,
                flush=True,
            )


def run(args: argparse.Namespace) -> int:
    loaded = load_from(args)
    if not args.seguir:
        print(run_job(loaded, args.job, LocalExecutor(), approved=args.yes).output)
        return 0
    _check(loaded, args.job, approved=args.yes)
    reader = api_for(loaded)  # sem PREFECT_API_URL, recusa antes de criar a rodada
    return follow_job(
        loaded,
        args.job,
        LocalExecutor(),
        approved=args.yes,
        reader=reader,
        screen=terminal(),
        timeout=args.timeout,
    )
