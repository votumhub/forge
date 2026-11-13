"""forge summary: resumo estático do bundle, sem resolver nem mostrar segredo."""

from __future__ import annotations

import argparse
import json
from datetime import date
from typing import cast

from forge.adapters.prefect import deployment_name
from forge.commands._common import add_target_args, load_from
from forge.debt import load_debt
from forge.load import Loaded
from forge.model import Job, Route, task_order
from forge.state import read_state


def register(verbs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = verbs.add_parser("summary", help="resumo estático do bundle, sem segredo")
    add_target_args(parser)
    parser.add_argument("-o", "--output", choices=("text", "json"), default="text")
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    data = summarize(load_from(args), date.today())
    print(
        json.dumps(data, indent=2, ensure_ascii=False)
        if args.output == "json"
        else text(data)
    )
    return 0


def summarize(loaded: Loaded, today: date) -> dict[str, object]:
    current = read_state(loaded.state_path, loaded.target_name).current_release()
    return {
        "bundle": loaded.name,
        "target": loaded.target_name,
        "mode": loaded.mode,
        "release": loaded.release,
        "project": loaded.project,
        "state_path": loaded.state_path.as_posix(),
        "current_release": current.id if current else None,
        "env": dict(sorted(loaded.env.items())),
        "variables": dict(sorted(loaded.variables.items())),
        "artifacts": {
            name: {"env": a.env, "build": a.build is not None, "pull": a.pull}
            for name, a in loaded.manifest.artifacts.items()
        },
        "services": {
            name: {"compose": s.compose, "release_services": s.release_services}
            for name, s in loaded.services.items()
        },
        "jobs": {name: _job(loaded, name, j) for name, j in loaded.jobs.items()},
        "storages": {
            name: s.model_dump(exclude_none=True) for name, s in loaded.storages.items()
        },
        "routes": {name: _route(r) for name, r in loaded.routes.items()},
        "proxy": loaded.target.proxy.kind if loaded.target.proxy else None,
        "debt": [
            {
                "rule": e.rule,
                "title": e.title,
                "trigger": e.trigger or None,
                "overdue": e.overdue(today),
            }
            for e in load_debt(loaded.root)
        ],
    }


def _job(loaded: Loaded, name: str, job: Job) -> dict[str, object]:
    trigger = job.trigger.model_dump(exclude_none=True)
    if job.on_deploy is not None:
        return {"trigger": trigger, "tasks": [t.key for t in task_order(job.tasks)]}
    return {
        "trigger": trigger,
        "deployment": deployment_name(name, loaded.target_name),
        "flow": job.flow,
        "work_pool": job.work_pool,
    }


def _route(route: Route) -> dict[str, object]:
    """Sem tls: o caminho do certificado é do host do proxy, não interessa ao resumo."""
    return route.model_dump(exclude={"tls"}) | {"tls": route.tls is not None}


def text(data: dict[str, object]) -> str:
    lines = [
        f"{data['bundle']} -> {data['target']} ({data['mode']})",
        f"  release {data['release']} (atual: {data['current_release'] or 'nenhuma'})",
        f"  project {data['project']}, state em {data['state_path']}, "
        f"proxy {data['proxy'] or 'nenhum'}",
    ]
    for section in (
        "env",
        "variables",
        "artifacts",
        "services",
        "jobs",
        "storages",
        "routes",
    ):
        entries = cast(dict[str, object], data[section])
        lines.append(f"{section}: {len(entries)}")
        lines += [f"  {name}: {_brief(value)}" for name, value in entries.items()]
    debt = cast(list[dict[str, object]], data["debt"])
    lines.append(f"débito: {len(debt)}")
    lines += [_debt_line(item) for item in debt]
    return "\n".join(lines)


def _brief(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _debt_line(item: dict[str, object]) -> str:
    trigger = (
        f" [gatilho {item['trigger']}{' VENCIDO' if item['overdue'] else ''}]"
        if item["trigger"]
        else ""
    )
    return f"  {item['rule']} {item['title']}{trigger}"
