"""forge schema: o JSON Schema do forge.yaml ou de um arquivo de recurso, derivado do modelo."""

from __future__ import annotations

import argparse
import json
from typing import Any

from pydantic import BaseModel

from forge.errors import ForgeError
from forge.model import Job, Manifest, Route, Service, Storage

RESOURCE_MODELS: dict[str, type[BaseModel]] = {
    "service": Service,
    "job": Job,
    "storage": Storage,
    "route": Route,
}
PER_TARGET = "RouteTarget"


def register(verbs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = verbs.add_parser(
        "schema",
        help="imprime o JSON Schema do forge.yaml ou de um tipo de recurso",
    )
    parser.add_argument(
        "kind",
        nargs="?",
        metavar="TIPO",
        help=f"tipo de recurso ({', '.join(RESOURCE_MODELS)}); sem ele, o forge.yaml",
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    schema = _model(args.kind).model_json_schema()
    if args.kind == "route":
        schema = _per_target(schema)
    print(json.dumps(schema, indent=2))
    return 0


def _per_target(schema: dict[str, Any]) -> dict[str, Any]:
    """O loader sobrepõe `targets.<alvo>` à raiz, mesclando os mapas aninhados, e só então
    cobra os obrigatórios."""
    partial = {
        key: value
        for key, value in schema.items()
        if key not in ("$defs", "required", "description")
    }
    partial["title"] = PER_TARGET
    targets = {
        "type": ["object", "null"],
        "description": "alvo do forge.yaml -> campos da route que valem só nele",
        "additionalProperties": {
            "anyOf": [{"$ref": f"#/$defs/{PER_TARGET}"}, {"type": "null"}]
        },
    }
    nested = {
        name: {key: value for key, value in part.items() if key != "required"}
        for name, part in schema.get("$defs", {}).items()
    }
    whole = {key: value for key, value in schema.items() if key != "required"}
    whole["$defs"] = {**nested, PER_TARGET: partial}
    whole["properties"] = {**schema["properties"], "targets": targets}
    return whole


def _model(kind: str | None) -> type[BaseModel]:
    if kind is None:
        return Manifest
    model = RESOURCE_MODELS.get(kind)
    if model is None:
        raise ForgeError(
            f"tipo de recurso {kind!r} desconhecido: o forge só lê "
            f"resources/<nome>.<tipo>.yaml com tipo {', '.join(RESOURCE_MODELS)}; "
            f"rode `forge schema <tipo>` com um deles, ou `forge schema` para o forge.yaml"
        )
    return model
