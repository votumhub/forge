"""forge validate: valida o manifesto, a política e o débito técnico."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date

from forge.commands._common import add_target_args, load_from
from forge.debt import DebtEntry, load_debt
from forge.load import Loaded, read_manifest
from forge.policy import Finding, check_debt, check_targets, excused

EXIT_OK = 0
EXIT_FOUND = 1


def register(verbs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = verbs.add_parser(
        "validate", help="valida o manifesto, a política e o débito técnico"
    )
    add_target_args(parser)
    parser.add_argument("--strict", action="store_true", help="aviso também reprova")
    parser.set_defaults(handler=run)


@dataclass(frozen=True)
class Verdict:
    failing: list[Finding]
    passing: list[Finding]
    excused: list[tuple[Finding, DebtEntry]]
    targets: list[str]

    def lines(self) -> list[str]:
        out = [f"alvos: {', '.join(self.targets)}"]
        out += [
            _line("ERRO" if f.level == "error" else "AVISO", f) for f in self.failing
        ]
        out += [_line("aviso", f) for f in self.passing]
        if self.excused:
            out.append("exceções (docs/debito-tecnico.md):")
            out += [
                f"  {f.rule} {f.where}: {entry.fields.get('motivo', '')}"
                for f, entry in self.excused
            ]
        out.append(
            f"{len(self.failing)} reprovando, {len(self.passing)} aviso(s), "
            f"{len(self.excused)} exceção(ões)"
        )
        return out


def _line(mark: str, finding: Finding) -> str:
    return f"  {mark:5} {finding.rule} {finding.where}: {finding.message}"


def run(args: argparse.Namespace) -> int:
    verdict = judge(
        targets_for(args), load_debt(args.root.resolve()), args.strict, date.today()
    )
    print("\n".join(verdict.lines()))
    return EXIT_FOUND if verdict.failing else EXIT_OK


def targets_for(args: argparse.Namespace) -> list[Loaded]:
    if args.target:
        return [load_from(args)]
    return [
        load_from(args, name) for name in read_manifest(args.root.resolve()).targets
    ]


def judge(
    loaded: list[Loaded], debt: list[DebtEntry], strict: bool, today: date
) -> Verdict:
    strict = strict or any(item.manifest.policy.strict for item in loaded)
    failing: list[Finding] = []
    passing: list[Finding] = []
    pardoned: list[tuple[Finding, DebtEntry]] = []
    for finding in [*check_targets(loaded), *check_debt(debt, today)]:
        entry = excused(finding, debt)
        if entry is not None:
            pardoned.append((finding, entry))
        elif finding.level == "error" or strict:
            failing.append(finding)
        else:
            passing.append(finding)
    return Verdict(failing, passing, pardoned, [item.target_name for item in loaded])
