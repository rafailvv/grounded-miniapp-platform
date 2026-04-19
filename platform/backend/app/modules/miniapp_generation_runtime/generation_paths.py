from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationPaths(MiniappGenerationRuntimeOwner):
    @staticmethod
    def _snake_case_filename(name: str) -> str:
        parts = name.split(".")
        stem = parts[0]
        suffix = f".{'.'.join(parts[1:])}" if len(parts) > 1 else ""
        if stem.startswith("__") and stem.endswith("__"):
            return f"{stem.lower()}{suffix.lower()}"
        normalized = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
        normalized = re.sub(r"_+", "_", normalized)
        return f"{normalized or 'file'}{suffix.lower()}"

    @classmethod
    def _normalize_generated_file_path(cls, path: str) -> str:
        candidate = path.strip().lstrip("/")
        if not candidate:
            return candidate
        landing_alias_match = re.fullmatch(
            r"miniapp/app/static/(?P<role>client|specialist|manager)/(?P<alias>home|client_home|specialist_home|manager_home)/(?P<name>index\.html|styles\.css|app\.js)",
            candidate,
        )
        if landing_alias_match:
            role = landing_alias_match.group("role")
            name = landing_alias_match.group("name")
            return f"miniapp/app/static/{role}/{name}"
        flat_static_match = re.fullmatch(
            r"miniapp/app/static/(?P<role>client|specialist|manager)/(?P<name>[^/]+)\.(?P<ext>html|css|js)",
            candidate,
        )
        if flat_static_match:
            role = flat_static_match.group("role")
            name = cls._snake_case_filename(flat_static_match.group("name"))
            ext = flat_static_match.group("ext")
            if name == "index":
                if ext == "html":
                    return f"miniapp/app/static/{role}/index.html"
                if ext == "css":
                    return f"miniapp/app/static/{role}/styles.css"
                return f"miniapp/app/static/{role}/app.js"
            if name == "styles" and ext == "css":
                return f"miniapp/app/static/{role}/styles.css"
            if name == "app" and ext == "js":
                return f"miniapp/app/static/{role}/app.js"
            if name == "app" and ext == "css":
                return f"miniapp/app/static/{role}/styles.css"
            if name == "styles" and ext == "js":
                return f"miniapp/app/static/{role}/app.js"
            if ext == "html":
                return f"miniapp/app/static/{role}/{name}/index.html"
            if ext == "css":
                return f"miniapp/app/static/{role}/{name}/styles.css"
            return f"miniapp/app/static/{role}/{name}/app.js"
        if candidate.startswith("miniapp/app/routes/") and candidate.endswith(".py"):
            head, tail = candidate.rsplit("/", 1)
            return f"{head}/{cls._snake_case_filename(tail)}"
        if candidate.startswith("miniapp/app/static/") and "." in Path(candidate).name:
            head, tail = candidate.rsplit("/", 1)
            return f"{head}/{cls._snake_case_filename(tail)}"
        if candidate.startswith("miniapp/tests/") and "." in Path(candidate).name:
            head, tail = candidate.rsplit("/", 1)
            return f"{head}/{cls._snake_case_filename(tail)}"
        return candidate

    @classmethod
    def _normalize_path_list(cls, value: Any, fallback: list[str] | None = None) -> list[str]:
        if not isinstance(value, list):
            return list(fallback or [])
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            candidate_raw = item.strip().lstrip("/")
            flat_static_match = re.fullmatch(
                r"miniapp/app/static/(?P<role>client|specialist|manager)/(?P<name>[^/]+)\.(?P<ext>html|css|js)",
                candidate_raw,
            )
            if flat_static_match:
                role = flat_static_match.group("role")
                name = cls._snake_case_filename(flat_static_match.group("name"))
                ext = flat_static_match.group("ext")
                if name == "index" and ext == "html":
                    candidate = f"miniapp/app/static/{role}/index.html"
                elif name in {"index", "styles"} and ext == "css":
                    candidate = f"miniapp/app/static/{role}/styles.css"
                elif name in {"index", "app"} and ext == "js":
                    candidate = f"miniapp/app/static/{role}/app.js"
                else:
                    candidate = f"miniapp/app/static/{role}/{name}.{ext}"
            else:
                candidate = cls._normalize_generated_file_path(item)
            if not candidate or ".." in candidate:
                continue
            if any(char.isspace() for char in candidate):
                continue
            if candidate.startswith(("miniapp/", "artifacts/")) and Path(candidate).suffix == "":
                continue
            normalized.append(candidate)
        return list(dict.fromkeys(normalized))

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        values: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            candidate = item.strip()
            if candidate:
                values.append(candidate)
        return list(dict.fromkeys(values))

    @staticmethod
    def _normalize_role_route_path(role: str, route_path: str, *, index: int) -> str:
        raw_normalized = route_path.strip() or ("/" if index == 0 else f"/page-{index + 1}")
        normalized = raw_normalized
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
        role_prefix = f"/{role}"
        compact_prefix = f"/{role}-"
        underscore_prefix = f"/{role}_"
        if normalized == role_prefix:
            return "/"
        if normalized.startswith(f"{role_prefix}/"):
            suffix = normalized[len(role_prefix):]
            return suffix or "/"
        if normalized.startswith(compact_prefix):
            suffix = normalized[len(compact_prefix):]
            return f"/{suffix}" if suffix else "/"
        if normalized.startswith(underscore_prefix):
            suffix = normalized[len(underscore_prefix):]
            return f"/{suffix}" if suffix else "/"
        if normalized in {"/home", "/dashboard", "/root"}:
            return "/"
        if role == "client" and normalized in {"/new", "/request_new", "/request_create"}:
            return "/create"
        if role == "specialist" and normalized in {"/tasks", "/assigned", "/queue"}:
            return "/requests"
        if role == "manager" and normalized in {"/tasks", "/queue", "/overview"}:
            return "/requests"
        return normalized

    @staticmethod
    def _absolute_role_route_path(role: str, route_path: str) -> str:
        normalized = str(route_path or "").strip() or "/"
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
        if normalized == "/":
            return f"/{role}"
        if normalized == f"/{role}" or normalized.startswith(f"/{role}/"):
            return normalized
        return f"/{role}{normalized}"

    @staticmethod
    def _default_page_file(role: str, component_name: str, *, route_path: str | None = None) -> str:
        normalized_route = (route_path or "").strip().lower()
        if normalized_route in {"", "/"}:
            return f"miniapp/app/static/{role}/index.html"
        elif normalized_route.rstrip("/") == "/profile":
            slug = "profile"
        else:
            route_slug = re.sub(r":[^/]+", "detail", normalized_route)
            route_slug = route_slug.strip("/").replace("/", "_").replace("-", "_")
            route_slug = re.sub(r"[^a-z0-9_]+", "_", route_slug).strip("_")
            route_slug = re.sub(r"_+", "_", route_slug)
            slug = route_slug or "page"
        if not slug or slug == role:
            component_slug = re.sub(r"(?<!^)(?=[A-Z])", "_", component_name).replace("__", "_").strip("_").lower()
            if component_slug.endswith("_page"):
                component_slug = component_slug[:-5]
            slug = component_slug or "page"
        return f"miniapp/app/static/{role}/{slug}/index.html"

    @staticmethod
    def _default_page_asset_path(file_path: str, *, asset_kind: str) -> str:
        file_path = file_path.replace("\\", "/")
        role_root_match = re.fullmatch(r"miniapp/app/static/([^/]+)/index\.html", file_path)
        if role_root_match:
            role = role_root_match.group(1)
            return f"miniapp/app/static/{role}/{'styles.css' if asset_kind == 'css' else 'app.js'}"
        if file_path.endswith("/index.html"):
            base_dir = Path(file_path).parent
            file_name = "styles.css" if asset_kind == "css" else "app.js"
            return str(base_dir / file_name).replace("\\", "/")
        suffix = ".css" if asset_kind == "css" else ".js"
        return str(Path(file_path).with_suffix(suffix)).replace("\\", "/")
