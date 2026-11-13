"""forge recipes: lista as receitas que o init sabe acrescentar."""

from __future__ import annotations

import argparse
import json

from forge.scaffold import load_recipe, recipe_names


def register(verbs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = verbs.add_parser("recipes", help="lista as receitas do init")
    parser.add_argument("-o", "--output", choices=("text", "json"), default="text")
    parser.set_defaults(handler=run)


def catalog() -> list[dict[str, object]]:
    return [{"name": name, **load_recipe(name).model_dump()} for name in recipe_names()]


def run(args: argparse.Namespace) -> int:
    items = catalog()
    if args.output == "json":
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return 0
    for item in items:
        mark = "provada" if item["provada"] else "       "
        print(f"{item['name']:26} {item['family']:18} {mark} {item['description']}")
    print(
        f"{len(items)} receita(s); `forge init --recipe <nome>` acrescenta sem sobrescrever"
    )
    return 0
