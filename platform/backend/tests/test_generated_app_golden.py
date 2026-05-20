from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_ROOT = ROOT / "runtime" / "templates" / "base-miniapp"
GOLDEN = Path(__file__).resolve().parent / "golden" / "base_miniapp_manifest.json"
IGNORED_DIRS = {".git", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def _template_manifest() -> dict[str, object]:
    digest = hashlib.sha256()
    file_hashes: dict[str, str] = {}
    for path in sorted(item for item in TEMPLATE_ROOT.rglob("*") if item.is_file()):
        relative = path.relative_to(TEMPLATE_ROOT)
        if any(part in IGNORED_DIRS for part in relative.parts):
            continue
        rel = relative.as_posix()
        content = path.read_bytes()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
        file_hashes[rel] = hashlib.sha256(content).hexdigest()
    return {
        "schema": "grounded.golden.generated_app_template.v1",
        "template": "base-miniapp",
        "directory_sha256": digest.hexdigest(),
        "file_count": len(file_hashes),
        "required_paths": sorted(file_hashes),
        "file_hashes": file_hashes,
    }


def test_base_miniapp_template_matches_golden_fixture() -> None:
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    actual = _template_manifest()

    assert actual == expected


def test_base_miniapp_template_keeps_runtime_contract_files() -> None:
    manifest = _template_manifest()
    paths = set(manifest["required_paths"])

    assert "miniapp/app/main.py" in paths
    assert "miniapp/app/routes/health.py" in paths
    assert "miniapp/app/routes/role_routes.py" in paths
    assert "miniapp/app/static/shared/app_helpers.js" in paths
    assert "miniapp/app/static/preview_bridge.js" in paths
    assert not any("__pycache__" in path or path.endswith(".pyc") for path in paths)
