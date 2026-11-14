"""Orquestrador: lock -> checks -> código montado (só no rollback) -> jobs before_deploy ->
artifacts -> segredos -> storage ensure -> jobs before_up -> up -> health -> routes -> jobs do
Prefect -> jobs after_up -> record -> limpeza das imagens construídas que o rollback não alcança.
Falha nunca grava ok; o lock sai sempre (ou vira aviso). `deploy_and_recover` é o verbo deploy:
em production, a falha que já mexeu no ar volta sozinha à release anterior pelo mesmo caminho do
rollback, salvo com o state ilegível ou que não gravou a tentativa."""

from __future__ import annotations

import dataclasses
import fnmatch
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from forge.adapters.compose import ComposeAdapter
from forge.adapters.prefect import PrefectAdapter
from forge.adapters.proxy import RouteAdapter
from forge.adapters.storage import StorageAdapter
from forge.artifacts import artifact_env, remove_built_images, resolve_artifacts
from forge.contracts import Adapter, DeployContext, Executor
from forge.envelope import envelope_for, forge_values, secret_refs, write_envelope
from forge.errors import ForgeError
from forge.gitinfo import read_git
from forge.load import Loaded
from forge.model import DeployMoment, Job, Service
from forge.mounts import restore_code
from forge.policy import compose_service_names, example_registry, paths_env
from forge.state import (
    Release,
    State,
    StateError,
    Tree,
    deploy_lock,
    now_iso,
    read_state,
    read_trees,
    record,
    record_tree,
    state_file,
)
from forge.tasks import run_deploy_jobs

Report = Callable[[str], None]


class ProductionCheckError(ForgeError):
    pass


class DeployFailedError(ForgeError):
    pass


@dataclass(frozen=True)
class Back:
    """A release de volta e como carregar o bundle dela depois que o checkout muda a árvore."""

    release: Release
    reload: Callable[[str], Loaded]


@dataclass
class Attempt:
    """`back` dado = rollback. `branch` é a branch do checkout antes da tentativa: depois de um
    rollback que destaca o HEAD, é para ela que se volta. `live` vira True quando o primeiro
    serviço começa a subir: daí em diante a falha deixou o ar diferente do que estava.
    `unrecorded` guarda o desfecho que o state recusou gravar: o histórico dele deixa de dizer
    onde o ar está."""

    back: Back | None = None
    branch: str | None = None
    back_dirty: bool | None = None
    given: Mapping[str, str] | None = None
    pinned: dict[str, str] = field(default_factory=dict)
    live: bool = False
    unrecorded: Literal["ok", "failed"] | None = None

    def __post_init__(self) -> None:
        if self.back is not None:
            self.given = self.back.release.digests
        self.pinned.update(self.given or {})


@dataclass(frozen=True)
class Adapters:
    service: Adapter[Service]
    job: Adapter[Job]
    storage: StorageAdapter = field(default_factory=StorageAdapter)
    route: RouteAdapter = field(default_factory=RouteAdapter)


def default_adapters(loaded: Loaded) -> Adapters:
    return Adapters(
        service=ComposeAdapter(siblings=compose_siblings(loaded)),
        job=PrefectAdapter(loaded.name, loaded.flow_jobs),
    )


def compose_siblings(loaded: Loaded) -> dict[str, frozenset[str]]:
    """project -> serviços compose declarados por todos os resources service do bundle."""
    found: dict[str, frozenset[str]] = {}
    for service in loaded.services.values():
        project = service.project or loaded.project
        names = compose_service_names(loaded.root, service.compose, paths_env(loaded))[
            0
        ]
        found[project] = found.get(project, frozenset()) | names
    return found


def production_checks(loaded: Loaded, approved: bool) -> None:
    rule, git, name = loaded.target.git, loaded.git, loaded.target_name
    if not approved:
        raise ProductionCheckError(f"alvo {name} é production: confirme com --yes")
    if git is None or rule is None:
        raise ProductionCheckError(
            f"alvo {name} é production e exige git (targets.{name}.git)"
        )
    if git.dirty:
        raise ProductionCheckError(
            f"alvo {name}: árvore suja; production só sai de commit limpo"
        )
    if rule.branch is not None and not fnmatch.fnmatchcase(
        git.branch or "", rule.branch
    ):
        raise ProductionCheckError(
            f"alvo {name} sai da branch {rule.branch}; HEAD está em "
            f"{git.branch or 'detached'}{_return_hint(loaded)}"
        )
    if rule.tag is not None and not fnmatch.fnmatchcase(git.tag or "", rule.tag):
        found = f"tem a tag {git.tag}" if git.tag else "não tem tag exata"
        raise ProductionCheckError(f"alvo {name} sai de tag {rule.tag}; HEAD {found}")
    pull_less = [n for n, a in loaded.manifest.artifacts.items() if a.pull is None]
    if pull_less:
        raise ProductionCheckError(
            f"alvo {name}: artifact(s) sem pull: {', '.join(pull_less)}"
        )
    for example in example_registry(loaded):
        raise ProductionCheckError(f"{example.where}: {example.message}")


def _return_hint(loaded: Loaded) -> str:
    """HEAD destacado depois de um rollback: a branch de onde o checkout saiu está no state."""
    branch = _left_branch(loaded)
    if loaded.git is None or loaded.git.branch is not None or branch is None:
        return ""
    return (
        f". O rollback deixa o HEAD destacado no commit da release: rode git checkout "
        f"{branch} e o deploy de novo"
    )


def _left_branch(loaded: Loaded) -> str | None:
    """A branch do deploy ok mais recente que a registrou."""
    try:
        trees = read_trees(loaded.state_path, loaded.target_name)
    except StateError:
        return None
    return next((t.branch for t in reversed(trees.values()) if t.branch), None)


def context(
    loaded: Loaded, executor: Executor, env: Mapping[str, str]
) -> DeployContext:
    return DeployContext(
        root=loaded.root,
        target=loaded.target_name,
        mode=loaded.target.mode,
        release=loaded.release,
        project=loaded.project,
        env=dict(env),
        env_file=loaded.env_file,
        build_dir=loaded.build_dir,
        executor=executor,
    )


def prepared_context(
    loaded: Loaded, executor: Executor, images: Mapping[str, str]
) -> DeployContext:
    """Envelope escrito e segredos escondidos; `images` é o env dos artifacts em uso."""
    envelope = envelope_for(loaded, images)
    executor.hide(envelope.secret_values())
    write_envelope(envelope, loaded.env_file)
    return context(loaded, executor, envelope.values)


def public_env(loaded: Loaded) -> dict[str, str]:
    """Env sem segredo: é o que o before_deploy enxerga, antes de o envelope existir."""
    refs = secret_refs(loaded.env, f"env do alvo {loaded.target_name}")
    literals = {k: v for k, v in loaded.env.items() if k not in refs}
    return {**literals, **forge_values(loaded)}


def deploy(
    loaded: Loaded,
    executor: Executor,
    *,
    approved: bool = False,
    force_unlock: bool = False,
    digests: Mapping[str, str] | None = None,
    adapters: Adapters | None = None,
    report: Report = print,
) -> Release:
    """`digests` dado: re-aplica essas imagens sem construir nem puxar, e não traz o código
    montado de volta; reaplicar uma release inteira é o `rollback`."""
    return _attempt(
        loaded,
        executor,
        Attempt(given=digests),
        approved=approved,
        force_unlock=force_unlock,
        adapters=adapters,
        report=report,
    )


def _attempt(
    loaded: Loaded,
    executor: Executor,
    attempt: Attempt,
    *,
    approved: bool,
    force_unlock: bool,
    adapters: Adapters | None,
    report: Report,
) -> Release:
    with deploy_lock(loaded.state_path, force=force_unlock):
        report(f"lock em {loaded.state_path.as_posix()}")
        if loaded.target.mode == "production":
            production_checks(loaded, approved)
            report("checks de production ok")
        attempt.branch = loaded.git.branch if loaded.git else None
        loaded = _code_back(loaded, attempt, report)
        try:
            _steps(
                loaded,
                executor,
                adapters or default_adapters(loaded),
                attempt,
                report,
            )
        except BaseException as error:
            _record_failure(loaded, attempt, error)
            report(f"release {loaded.release} registrada como failed")
            raise
        release = _record(loaded, "ok", attempt)
        report(f"release {release.id} ok")
        for line in [
            *_record_tree(loaded, release, attempt),
            *_clean_images(loaded, executor),
        ]:
            report(line)
        return release


def _code_back(loaded: Loaded, attempt: Attempt, report: Report) -> Loaded:
    """No rollback, o código montado volta ao commit da release; com o checkout mudado, o
    bundle (serviços, compose, jobs) se carrega de novo da árvore dela."""
    back = attempt.back
    if back is None:
        return loaded
    restored = restore_code(loaded, back.release, report)
    attempt.back_dirty = restored.dirty
    if not restored.moved:
        return loaded
    report(f"bundle carregado de novo do commit da release {back.release.id}")
    return _same_state(back.reload(back.release.id), loaded, report)


def _same_state(again: Loaded, before: Loaded, report: Report) -> Loaded:
    """O state e a trava ficam onde a tentativa os abriu, mesmo que a release declare outro."""
    if again.state_path == before.state_path:
        return again
    report(
        f"state continua em {before.state_path.as_posix()} (a release declara "
        f"{again.state_path.as_posix()})"
    )
    target = again.target.model_copy(update={"state_path": before.target.state_path})
    return dataclasses.replace(again, target=target)


def _record_failure(loaded: Loaded, attempt: Attempt, error: BaseException) -> None:
    """Se nem a falha se grava, as duas causas sobem juntas, a do deploy primeiro."""
    try:
        _record(loaded, "failed", attempt)
    except StateError as unrecorded:
        raise DeployFailedError(
            f"{_cause(error)}\na falha também não foi registrada no state: {unrecorded}"
        ) from error


def _cause(error: BaseException) -> str:
    """Ctrl-C (KeyboardInterrupt) não tem texto e o SystemExit só traz o código."""
    if isinstance(error, Exception) and str(error):
        return str(error)
    return f"{type(error).__name__} {error}".strip()


def _clean_images(loaded: Loaded, executor: Executor) -> list[str]:
    """A release já está ok: a limpeza que falha vira linha do relatório, nunca erro."""
    try:
        return remove_built_images(loaded, executor, unreachable(loaded))
    except ForgeError as error:
        return [
            f"imagens construídas: limpeza não rodou ({error}); a release continua ok e "
            f"as imagens velhas ficam até o próximo deploy (docker image ls --filter "
            f"reference='{loaded.name}-*')"
        ]


def unreachable(loaded: Loaded) -> list[str]:
    """Releases deste alvo que o rollback de nenhum alvo de development alcança mais: as
    imagens construídas para elas podem sair."""
    own = read_state(loaded.state_path, loaded.target_name).releases
    return sorted({r.id for r in own} - reachable(loaded))


def reachable(loaded: Loaded, *, skip_own: bool = False) -> set[str]:
    """A etiqueta da imagem construída não leva o alvo: dois alvos de development com o mesmo
    id de release dividem a imagem, e o rollback de qualquer um deles a protege."""
    kept: set[str] = set()
    for name, target in loaded.manifest.targets.items():
        if target.mode != "development" or (skip_own and name == loaded.target_name):
            continue
        other = dataclasses.replace(loaded, target_name=name, target=target)
        kept |= read_state(other.state_path, name).rollback_reach()
    return kept


def _steps(
    loaded: Loaded,
    executor: Executor,
    adapters: Adapters,
    attempt: Attempt,
    report: Report,
) -> None:
    _deploy_jobs(
        context(loaded, executor, public_env(loaded)), loaded, "before_deploy", report
    )
    pinned = attempt.pinned
    if attempt.given is None:
        pinned.update(resolve_artifacts(loaded, executor))
    report(f"artifacts: {', '.join(f'{k}={v}' for k, v in pinned.items()) or 'nenhum'}")
    ctx = prepared_context(
        loaded, executor, artifact_env(loaded.manifest.artifacts, pinned)
    )
    report(f"envelope em {loaded.env_file.as_posix()}")
    for name, storage in loaded.storages.items():
        report(adapters.storage.ensure(ctx, name, storage))
    _deploy_jobs(ctx, loaded, "before_up", report)
    attempt.live = True
    _up(ctx, loaded, adapters, report)
    _deploy_jobs(ctx, loaded, "after_up", report)


def _deploy_jobs(
    ctx: DeployContext, loaded: Loaded, when: DeployMoment, report: Report
) -> None:
    for line in run_deploy_jobs(ctx, loaded.jobs, loaded.services, when):
        report(line)


def _up(ctx: DeployContext, loaded: Loaded, adapters: Adapters, report: Report) -> None:
    """Serviços sobem e ficam saudáveis antes dos jobs: o orquestrador que recebe os
    deployments pode ser um serviço do próprio bundle."""
    for name, service in loaded.services.items():
        adapters.service.up(ctx, name, service)
        report(f"serviço {name}: up")
    for name, service in loaded.services.items():
        adapters.service.health(ctx, name, service)
        report(f"serviço {name}: saudável")
    for line in adapters.route.apply(
        ctx, loaded.name, loaded.routes, loaded.target.proxy
    ):
        report(line)
    for name, job in loaded.flow_jobs.items():
        adapters.job.up(ctx, name, job)
        adapters.job.health(ctx, name, job)
        report(f"job {name}: deployment publicado")


def _record(
    loaded: Loaded, status: Literal["ok", "failed"], attempt: Attempt
) -> Release:
    git, back = loaded.git, attempt.back
    release = Release(
        id=loaded.release,
        git_sha=back.release.git_sha if back else (git.sha if git else None),
        at=now_iso(),
        status=status,
        digests=dict(attempt.pinned),
    )
    try:
        record(loaded.state_path, loaded.target_name, release)
    except StateError:
        attempt.unrecorded = status
        raise
    return release


def _record_tree(loaded: Loaded, release: Release, attempt: Attempt) -> list[str]:
    """Depois do state, e nunca derruba a release que já subiu: sem a entrada, o rollback para
    ela trata a árvore como desconhecida."""
    git = loaded.git
    dirty = attempt.back_dirty if attempt.back else bool(git and git.dirty)
    tree = Tree(dirty=dirty, branch=attempt.branch)
    try:
        return record_tree(loaded.state_path, loaded.target_name, release, tree)
    except StateError as error:
        return [f"árvore da release {release.id} não registrada: {error}"]


def rollback(
    loaded: Loaded,
    executor: Executor,
    *,
    reload: Callable[[str], Loaded],
    approved: bool = False,
    force_unlock: bool = False,
    adapters: Adapters | None = None,
    report: Report = print,
) -> Release:
    """`reload(id)` carrega o bundle de novo com a release de volta: `${bundle.release}` no env
    e nos resources passa a valer o id dela, não o da árvore atual. O código montado do disco
    volta ao commit dela antes de qualquer passo, e o bundle se carrega de novo da árvore dela."""
    candidate = read_state(loaded.state_path, loaded.target_name).rollback_candidate()
    report(f"voltando para {candidate.id} ({candidate.at})")
    return _attempt(
        reload(candidate.id),
        executor,
        Attempt(Back(candidate, reload)),
        approved=approved,
        force_unlock=force_unlock,
        adapters=adapters,
        report=report,
    )


def deploy_and_recover(
    loaded: Loaded,
    executor: Executor,
    *,
    reload: Callable[[str], Loaded],
    approved: bool = False,
    force_unlock: bool = False,
    adapters: Adapters | None = None,
    report: Report = print,
) -> Release:
    """Deploy novo. Se falha depois de mexer no ar, diz onde o serviço ficou; em production
    volta sozinho à release que o rollback escolheria, e termina em erro mesmo assim."""
    attempt = Attempt(None)
    try:
        return _attempt(
            loaded,
            executor,
            attempt,
            approved=approved,
            force_unlock=force_unlock,
            adapters=adapters,
            report=report,
        )
    except Exception as error:
        if not attempt.live:
            raise
        recovery = Recovery(loaded, executor, reload, approved, adapters, report)
        raise recovery.after(error, attempt.unrecorded) from error


@dataclass(frozen=True)
class Recovery:
    loaded: Loaded
    executor: Executor
    reload: Callable[[str], Loaded]
    approved: bool
    adapters: Adapters | None
    report: Report

    def after(
        self, error: Exception, unrecorded: Literal["ok", "failed"] | None
    ) -> DeployFailedError:
        """Sem a tentativa no state, a candidata do rollback sai de um histórico que não
        reflete o ar: não se escolhe para onde voltar."""
        loaded, target = self.loaded, self.loaded.target_name
        failed = f"a release {loaded.release} falhou no alvo {target}: {error}"
        if unrecorded == "ok":
            failed = f"a release {loaded.release} subiu saudável no alvo {target}, mas {error}"
        try:
            state = read_state(loaded.state_path, target)
        except StateError as unreadable:
            return DeployFailedError(f"{failed}\n{_unreadable(loaded, unreadable)}")
        if unrecorded is not None:
            return DeployFailedError(f"{failed}\n{_unrecorded(loaded, state)}")
        try:
            candidate = state.rollback_candidate()
        except StateError:
            return DeployFailedError(f"{failed}\n{_nowhere(loaded)}")
        if loaded.mode != "production":
            return DeployFailedError(
                f"{failed}\no serviço ficou na release {loaded.release}, que falhou "
                f"(development não volta sozinho). Para voltar à {candidate.id}: "
                f"forge rollback -t {target}"
            )
        return self._back(failed, candidate.id)

    def _back(self, failed: str, candidate: str) -> DeployFailedError:
        target = self.loaded.target_name
        self.report(f"production: voltando sozinho para a release {candidate}")
        try:
            back = rollback(
                self.loaded,
                self.executor,
                reload=self.reload,
                approved=self.approved,
                adapters=self.adapters,
                report=self.report,
            )
        except Exception as second:
            return DeployFailedError(
                f"{failed}\na volta automática para a release {candidate} também falhou: "
                f"{second}\no serviço pode estar fora do ar. Confira com forge plan -t "
                f"{target} e tente de novo com forge rollback -t {target} --yes"
            )
        return DeployFailedError(
            f"{failed}\nvoltei sozinho para a release {back.id}: o serviço está de pé "
            f"nela. {_detached(self.loaded)}Corrija e rode forge deploy -t "
            f"{target} --yes de novo"
        )


def _detached(loaded: Loaded) -> str:
    git, branch = read_git(loaded.root), _left_branch(loaded)
    if git is None or git.branch is not None or branch is None:
        return ""
    return (
        f"O checkout ficou destacado no commit {git.short} da release: rode git checkout "
        f"{branch} antes. "
    )


def _unreadable(loaded: Loaded, error: StateError) -> str:
    return (
        f"o state do alvo não pôde ser lido ({error}): o forge não sabe em que release o "
        f"serviço ficou nem para qual voltar, e não volta sozinho. Conserte ou restaure "
        f"esse arquivo e confira com forge plan -t {loaded.target_name}"
    )


def _unrecorded(loaded: Loaded, state: State) -> str:
    target, release = loaded.target_name, loaded.release
    confirm = " --yes" if loaded.mode == "production" else ""
    marks = (
        f"ainda marca a release {state.current}"
        if state.current is not None
        else "não marca release nenhuma"
    )
    return (
        f"o state não registrou esta tentativa e {marks}, mas o que está no ar é a "
        f"{release}. {_stale_rollback(state, target, confirm)}O forge não volta sozinho: "
        f"conserte a gravação de {state_file(loaded.state_path, target).as_posix()} "
        f"(permissão, disco cheio) e rode forge deploy -t {target}{confirm} de novo"
    )


def _stale_rollback(state: State, target: str, confirm: str) -> str:
    try:
        candidate = state.rollback_candidate()
    except StateError:
        return ""
    return (
        f"Cuidado: forge rollback -t {target}{confirm} agora iria para a {candidate.id}, "
        "escolhida por esse histórico que não tem esta tentativa. "
    )


def _nowhere(loaded: Loaded) -> str:
    target = loaded.target_name
    if loaded.mode == "production":
        return (
            "não há release anterior boa para voltar: esta é a primeira deste alvo, e "
            f"destroy não existe em production. Corrija e rode forge deploy -t {target} "
            "--yes de novo"
        )
    return (
        "não há release anterior boa para voltar: esta é a primeira deste alvo. Para "
        f"derrubar o que subiu: forge destroy -t {target}"
    )
