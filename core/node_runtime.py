from __future__ import annotations

import json
from pathlib import Path


def missing_node_dependencies(app_dir: Path) -> list[str]:
    manifest_path = app_dir / "package.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        dependencies = manifest.get("dependencies", {})
    except (OSError, json.JSONDecodeError, AttributeError):
        return ["app/package.json"]

    modules_dir = app_dir / "node_modules"
    return sorted(
        package
        for package in dependencies
        if not (modules_dir / package / "package.json").is_file()
    )
