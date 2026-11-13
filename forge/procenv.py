"""O ambiente mínimo que todo processo filho recebe; o resto é sempre explícito."""

from __future__ import annotations

import os
from collections.abc import Mapping

BASE_NAMES = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TZ")
WINDOWS_NAMES = (
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "USERNAME",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "HOMEDRIVE",
    "HOMEPATH",
)
PREFIXES = ("DOCKER_",)


def base_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    wanted = {name.upper() for name in (*BASE_NAMES, *WINDOWS_NAMES)}
    return {
        name: value
        for name, value in source.items()
        if name.upper() in wanted or name.upper().startswith(PREFIXES)
    }
