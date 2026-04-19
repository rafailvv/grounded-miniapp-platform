from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_GENERATED_DIR = Path(__file__).resolve().parent


def _load_json(name: str) -> dict[str, Any]:
    path = _GENERATED_DIR / name
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


route_manifest = _load_json("route_manifest.json")
runtime_manifest = _load_json("runtime_manifest.json")
static_runtime_manifest = _load_json("static_runtime_manifest.json")
role_seed = _load_json("role_seed.json")
role_experience = _load_json("role_experience.json")

