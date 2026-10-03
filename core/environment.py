from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, load_dotenv


@dataclass(frozen=True)
class EnvironmentLoadResult:
    path: Path | None
    shadowed_keys: tuple[str, ...] = ()


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def load_project_environment(base_dir: Path | None = None) -> EnvironmentLoadResult:
    env_path = (base_dir or project_root()) / ".env"
    if not env_path.is_file():
        return EnvironmentLoadResult(path=None)

    configured_values = dotenv_values(env_path)
    shadowed_keys = tuple(
        sorted(key for key in configured_values if key in os.environ)
    )
    load_dotenv(dotenv_path=env_path, override=False)
    return EnvironmentLoadResult(path=env_path.resolve(), shadowed_keys=shadowed_keys)
