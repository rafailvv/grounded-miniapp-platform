from __future__ import annotations

import json
import re
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
GENERATED_DIR = BASE_DIR / "generated"
ROUTE_MANIFEST_PATH = GENERATED_DIR / "route_manifest.json"
ROLES = ("client", "specialist", "manager")


def load_route_manifest() -> dict:
    if not ROUTE_MANIFEST_PATH.exists():
        return {}
    try:
        return json.loads(ROUTE_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def canonicalize_role_path(role: str, actual_path: str) -> str:
    normalized = str(actual_path or "").strip() or f"/{role}"
    if normalized != "/" and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    if normalized == f"/{role}/root":
        return f"/{role}"
    return normalized or f"/{role}"


def normalize_declared_page_path(file_path: str) -> Path:
    normalized_file_path = str(file_path or "").replace("\\", "/")
    if normalized_file_path.startswith("miniapp/app/"):
        return BASE_DIR.parent / normalized_file_path.removeprefix("miniapp/app/")
    if normalized_file_path.startswith("app/"):
        return BASE_DIR.parent / normalized_file_path.removeprefix("app/")
    return BASE_DIR / normalized_file_path


def route_matches(pattern: str, actual: str) -> bool:
    normalized_pattern = re.sub(r"\{[^/]+\}", "[^/]+", pattern)
    normalized_pattern = re.sub(r":[^/]+", "[^/]+", normalized_pattern)
    return re.fullmatch(normalized_pattern, actual) is not None


def resolve_declared_page_file(role: str, actual_path: str) -> Path | None:
    actual_path = canonicalize_role_path(role, actual_path)
    route_manifest = load_route_manifest()
    pages = (((route_manifest.get("roles") or {}).get(role) or {}).get("pages") or [])
    for page in pages:
        if not isinstance(page, dict):
            continue
        route_path = str(page.get("route_path") or "").strip()
        file_path = str(page.get("file_path") or "").strip()
        if not route_path or not file_path:
            continue
        if not route_matches(route_path, actual_path):
            continue
        resolved = normalize_declared_page_path(file_path)
        if resolved.exists():
            return resolved
    return None


def resolve_default_role_page(role: str, actual_path: str) -> Path | None:
    actual_path = canonicalize_role_path(role, actual_path)
    if actual_path == f"/{role}":
        page_file = STATIC_DIR / role / "index.html"
        return page_file if page_file.exists() else None
    if actual_path == f"/{role}/profile":
        page_file = STATIC_DIR / role / "profile" / "index.html"
        return page_file if page_file.exists() else None
    slug_parts = [segment for segment in actual_path.removeprefix(f"/{role}").split("/") if segment]
    if len(slug_parts) == 1:
        page_file = STATIC_DIR / role / slug_parts[0] / "index.html"
        return page_file if page_file.exists() else None
    return None


def resolve_role_page(role: str, actual_path: str) -> Path:
    if role not in ROLES:
        raise KeyError(role)
    actual_path = canonicalize_role_path(role, actual_path)
    declared_page = resolve_declared_page_file(role, actual_path)
    if declared_page is not None:
        return declared_page
    fallback_page = resolve_default_role_page(role, actual_path)
    if fallback_page is not None:
        return fallback_page
    raise KeyError(actual_path)
