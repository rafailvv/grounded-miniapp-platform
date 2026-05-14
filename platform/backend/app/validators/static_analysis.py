from __future__ import annotations

from pathlib import Path
import re

ROLE_ORDER = ("client", "specialist", "manager")


def extract_html_ids(content: str) -> set[str]:
    return set(re.findall(r'id=["\']([A-Za-z0-9_-]+)["\']', str(content or "")))


def extract_js_dom_ids(content: str) -> set[str]:
    ids = set(re.findall(r'querySelector(?:All)?\(\s*["\']#([A-Za-z0-9_-]+)', str(content or "")))
    ids.update(re.findall(r'getElementById\(\s*["\']([A-Za-z0-9_-]+)', str(content or "")))
    return ids


def role_from_static_path(relative_path: str) -> str | None:
    parts = str(relative_path or "").replace("\\", "/").split("/")
    try:
        static_index = parts.index("static")
    except ValueError:
        return None
    if len(parts) <= static_index + 1:
        return None
    role = parts[static_index + 1]
    return role if role in ROLE_ORDER else None


def role_static_root(source_dir: Path, relative_path: str) -> Path | None:
    role = role_from_static_path(relative_path)
    if not role:
        return None
    return source_dir / "miniapp/app/static" / role


def role_html_ids(source_dir: Path, relative_path: str) -> set[str]:
    root = role_static_root(source_dir, relative_path)
    if root is None or not root.exists():
        return set()
    ids: set[str] = set()
    for html_path in root.rglob("index.html"):
        try:
            ids.update(extract_html_ids(html_path.read_text(encoding="utf-8")))
        except OSError:
            continue
    return ids


def role_surface_dom_ids(source_dir: Path, relative_path: str) -> set[str]:
    root = role_static_root(source_dir, relative_path)
    if root is None or not root.exists():
        return set()
    ids = role_html_ids(source_dir, relative_path)
    for js_path in root.rglob("*.js"):
        try:
            ids.update(extract_html_ids(js_path.read_text(encoding="utf-8")))
        except OSError:
            continue
    return ids


def extract_script_refs(html_content: str) -> list[str]:
    refs = re.findall(r"""<script\b[^>]*\bsrc=["']([^"']+\.js(?:[?#][^"']*)?)["']""", str(html_content or ""), flags=re.IGNORECASE)
    return list(dict.fromkeys(ref.split("?", 1)[0].split("#", 1)[0] for ref in refs))


def normalize_api_path(value: str) -> str:
    normalized = str(value or "").strip().strip("'\"`").split("?", 1)[0].split("#", 1)[0]
    normalized = re.sub(r"\$\{[^}]+\}", "{param}", normalized)
    normalized = normalized.rstrip(".,;")
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return re.sub(r"/+", "/", normalized).rstrip("/") or "/"


def extract_frontend_api_refs(content: str) -> set[tuple[str, str]]:
    text = str(content or "")
    refs: set[tuple[str, str]] = set()
    constants = _api_path_constants(text)

    for match in re.finditer(
        r"\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?\.(?P<method>get|post|put|patch|delete)\s*\(\s*(?P<quote>['\"`])(?P<path>[^'\"`]+)(?P=quote)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        path = _resolve_api_literal(match.group("path"), constants)
        if path:
            refs.add((match.group("method").upper(), path))

    for match in re.finditer(
        r"\b(?P<name>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)\s*\(\s*(?P<var>[A-Za-z_$][\w$]*)\s*(?P<tail>,[^;)]*)?\)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        var_name = match.group("var")
        path = constants.get(var_name)
        if not path:
            continue
        method = _method_from_options(match.group("tail") or "") or _method_from_name(match.group("name"))
        if method:
            refs.add((method, normalize_api_path(path)))

    for match in _api_string_literals(text):
        path = _resolve_api_literal(match.group("value"), constants)
        if not path:
            continue
        method = _method_near_api_reference(text, match.start(), match.end())
        if method:
            refs.add((method, path))
    return refs


def extract_declared_routes(routes_root: Path, *, api_only: bool = True) -> set[tuple[str, str]]:
    paths: set[tuple[str, str]] = set()
    if not routes_root.exists():
        return paths
    for path in routes_root.glob("*.py"):
        if path.name == "__init__.py":
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        router_prefixes = {
            match.group("name"): match.group("prefix")
            for match in re.finditer(
                r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*APIRouter\(\s*prefix\s*=\s*['\"](?P<prefix>[^'\"]*)['\"]",
                content,
            )
        }
        for match in re.finditer(
            r"@(?P<name>[A-Za-z_][A-Za-z0-9_]*)\.(?P<method>get|post|put|patch|delete)\(\s*['\"](?P<path>[^'\"]*)['\"]",
            content,
        ):
            prefix = router_prefixes.get(match.group("name"), "")
            full_path = _join_route(prefix, match.group("path"))
            if api_only and full_path != "/api" and not full_path.startswith("/api/"):
                continue
            paths.add((match.group("method").upper(), normalize_api_path(full_path)))
    return paths


def _api_path_constants(content: str) -> dict[str, str]:
    constants: dict[str, str] = {}
    for match in re.finditer(
        r"(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?P<quote>['\"`])(?P<path>/api(?:/[^'\"`]*)?)(?P=quote)",
        content,
    ):
        constants[match.group("name")] = normalize_api_path(match.group("path"))
    return constants


def _api_string_literals(content: str):
    literal_pattern = re.compile(
        r"(?P<quote>['\"`])(?P<value>(?:/api/|\$\{[A-Za-z_$][\w$]*\})[^'\"`\s)]*)(?P=quote)",
        re.DOTALL,
    )
    yield from literal_pattern.finditer(content)


def _resolve_api_literal(value: str, constants: dict[str, str]) -> str | None:
    raw = str(value or "").strip()
    if raw.startswith("/api/") or raw == "/api":
        return normalize_api_path(raw)
    for name, base in constants.items():
        marker = "${" + name + "}"
        if marker not in raw:
            continue
        resolved = raw.replace(marker, base)
        return normalize_api_path(resolved)
    return None


def _method_near_api_reference(content: str, start: int, end: int) -> str | None:
    prefix = str(content or "")[max(0, start - 120):start]
    function_match = re.search(r"\b(?P<name>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)\s*\(\s*$", prefix, re.IGNORECASE)
    if not function_match:
        return None
    call_name = function_match.group("name")
    axios_method = re.search(r"\.(?P<method>get|post|put|patch|delete)$", call_name, re.IGNORECASE)
    if axios_method:
        return axios_method.group("method").upper()
    tail = str(content or "")[end:end + 900].split(";", 1)[0]
    explicit_method = _method_from_options(tail)
    if explicit_method:
        return explicit_method
    literal = str(content or "")[start:end]
    if "${" in literal and re.search(r"\.\.\.\s*(?:options|opts|init)\b", tail):
        return None
    return _method_from_name(call_name)


def _method_from_options(text: str) -> str | None:
    method_match = re.search(r"method\s*:\s*['\"](?P<method>GET|POST|PUT|PATCH|DELETE)['\"]", str(text or ""), re.IGNORECASE)
    if not method_match:
        return None
    return method_match.group("method").upper()


def _method_from_name(name: str) -> str | None:
    name = str(name or "").lower()
    base_name = name.rsplit(".", 1)[-1]
    if base_name == "get":
        return "GET"
    if base_name == "post":
        return "POST"
    if base_name == "put":
        return "PUT"
    if base_name == "patch":
        return "PATCH"
    if base_name == "delete":
        return "DELETE"
    if base_name.startswith("fetch") or "request" in base_name or "json" in base_name or "api" in base_name:
        return "GET"
    return None


def _join_route(prefix: str, route_path: str) -> str:
    normalized_prefix = str(prefix or "").strip().rstrip("/")
    normalized_path = str(route_path or "").strip()
    if not normalized_prefix:
        full_path = normalized_path or "/"
    elif normalized_path in {"", "/"}:
        full_path = normalized_prefix or "/"
    else:
        full_path = f"{normalized_prefix}/{normalized_path.lstrip('/')}"
    if not full_path.startswith("/"):
        full_path = f"/{full_path}"
    return re.sub(r"/+", "/", full_path)
