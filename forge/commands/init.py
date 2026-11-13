"""forge init: cria o esqueleto do bundle num repositório vazio ou existente, sem sobrescrever."""

from __future__ import annotations

import argparse
from pathlib import Path

from forge.load import MANIFEST_FILE, read_manifest
from forge.scaffold import (
    answers_for,
    archetype_names,
    load_recipe,
    read_config,
    scaffold,
)

DEFAULT_ARCHETYPE = "app"


def register(verbs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = verbs.add_parser(
        "init", help="cria o esqueleto do bundle num repositório vazio ou existente"
    )
    parser.add_argument(
        "archetype",
        nargs="?",
        help=f"arquétipo ({', '.join(archetype_names())}; padrão app)",
    )
    parser.add_argument(
        "--recipe",
        action="append",
        default=[],
        metavar="NOME",
        help="acrescenta uma receita (repetível; veja `forge recipes`)",
    )
    parser.add_argument(
        "--config-file", type=Path, help="respostas em YAML (chave: valor)"
    )
    parser.add_argument(
        "--no-input", action="store_true", help="não pergunta; usa os padrões"
    )
    parser.add_argument(
        "--vendor", action="store_true", help="copia o forge para ./forge"
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    root: Path = args.root
    exists = (root / MANIFEST_FILE).is_file()
    archetype = args.archetype or (
        None if exists and args.recipe else DEFAULT_ARCHETYPE
    )
    given = read_config(args.config_file) if args.config_file else {}
    if archetype is None:
        given = {"name": read_manifest(root.resolve()).bundle.name, **given}
    answers = answers_for(
        archetype or DEFAULT_ARCHETYPE,
        root,
        given,
        None if args.no_input or archetype is None else input,
    )
    outcome = scaffold(
        root, archetype, answers, vendored=args.vendor, recipes=args.recipe
    )
    print(
        f"bundle {answers['name']} ({archetype or 'só receitas'}) em {root.resolve()}"
    )
    print("\n".join(outcome.lines()))
    for name in args.recipe:
        _recipe_notes(name)
    print("próximo passo: leia FORGE.md e rode `uv run forge validate`")
    return 0


def _recipe_notes(name: str) -> None:
    recipe = load_recipe(name)
    if recipe.env:
        wanted = ", ".join(recipe.env)
        print(
            f"receita {name}: declare no env do forge.yaml: {wanted} (segredo -> secret://NOME)"
        )
    if recipe.requires:
        print(_requires_note(name, recipe.requires, recipe.runs_in_image))
    print(
        f"receita {name}: documentação {recipe.docs.url} (versão {recipe.docs.version})"
    )


def _requires_note(name: str, requires: list[str], runs_in_image: bool) -> str:
    wanted = ", ".join(requires)
    if runs_in_image:
        return (
            f"receita {name}: o código dela roda dentro da imagem do contêiner, não no "
            f".venv do repositório: a imagem precisa de {wanted} (veja o cabeçalho do "
            f"compose da receita)"
        )
    command = " ".join(f'"{requirement}"' for requirement in requires)
    return (
        f"receita {name}: acrescente às dependências do pyproject.toml: {wanted} "
        f"(`uv add {command}`)"
    )
