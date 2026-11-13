"""Débito técnico: `docs/debito-tecnico.md`, cada entrada uma regra que o projeto ainda não cumpre."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from forge.errors import read_utf8

DEBT_PATH = Path("docs") / "debito-tecnico.md"
REQUIRED_FIELDS = ("regra", "motivo", "mitigacao")
ENTRY_HEADING = re.compile(
    r"^##\s+(?P<rule>[A-Z]{3}\d{3})(?![0-9A-Z])\s*(?:-\s*(?P<title>.*))?$"
)
FIELD_LINE = re.compile(r"^-\s*(?P<field>[a-zç]+)\s*:\s*(?P<value>.*)$")
ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

DEBT_TEMPLATE = """# Débito técnico

Cada entrada é um atalho consciente: uma regra do `forge validate` (ou da equipe) que o
projeto ainda não cumpre. O débito aqui é roteiro de crescimento, não burocracia.

Como trabalhar com este arquivo (vale para gente e para LLM):

1. **Registre o atalho no ato.** Tomou um atalho? A entrada nasce no mesmo commit.
2. **Leia o débito antes de mexer na área.** Ele diz o que está torto ali e por quê.
3. **Pague ao passar pelo código quando couber.** Mexeu na área e o conserto cabe no
   mesmo trabalho? Pague.
4. **Gatilho vencido passa na frente de feature.** `forge validate` e `forge summary`
   avisam quando a data do gatilho passou.
5. **Pago sai do arquivo.** A história fica no git.

Formato (regra, motivo e mitigacao obrigatórios; dono, gatilho e instancia opcionais):

```
## POL002 - um título curto
- regra: o que a regra exige
- motivo: por que não dá para cumprir hoje
- mitigacao: o que fazemos enquanto não está resolvido
- dono: quem responde por isso
- gatilho: 2026-12-31 (ou o evento que obriga a pagar)
- instancia: onde exatamente (o `onde` do achado); sem ela, a regra inteira fica excusada
```
"""


@dataclass(frozen=True)
class DebtEntry:
    rule: str
    title: str
    fields: dict[str, str]

    @property
    def where(self) -> str:
        return self.fields.get("instancia", "")

    @property
    def trigger(self) -> str:
        return self.fields.get("gatilho", "")

    def missing(self) -> list[str]:
        return [
            name for name in REQUIRED_FIELDS if not self.fields.get(name, "").strip()
        ]

    def overdue(self, today: date) -> bool:
        found = ISO_DATE.search(self.trigger)
        return bool(found) and date.fromisoformat(found.group(1)) < today

    def excuses(self, rule: str, where: str) -> bool:
        return (
            self.rule == rule
            and not self.missing()
            and (not self.where or self.where == where)
        )


def parse_debt(text: str) -> list[DebtEntry]:
    entries: list[DebtEntry] = []
    field: str | None = None
    for line in _without_fences(text):
        heading = ENTRY_HEADING.match(line)
        if heading:
            entries.append(
                DebtEntry(heading["rule"], (heading["title"] or "").strip(), {})
            )
            field = None
            continue
        if not entries:
            continue
        field = _absorb(entries[-1].fields, line, field)
    return entries


def _absorb(fields: dict[str, str], line: str, field: str | None) -> str | None:
    found = FIELD_LINE.match(line)
    if found:
        fields[found["field"]] = found["value"].strip()
        return found["field"]
    if field and line.startswith((" ", "\t")) and line.strip():
        fields[field] = f"{fields[field]} {line.strip()}".strip()
        return field
    return None if not line.strip() or line.startswith("#") else field


def load_debt(root: Path) -> list[DebtEntry]:
    path = root / DEBT_PATH
    if not path.is_file():
        return []
    return parse_debt(read_utf8(path))


def _without_fences(text: str) -> list[str]:
    kept: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if not inside:
            kept.append(line)
    return kept
