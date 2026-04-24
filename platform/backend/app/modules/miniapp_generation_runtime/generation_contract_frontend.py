from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.modules.miniapp_contract.runtime_contract_sync import MiniappRuntimeContractSync
from app.models.domain import DraftFileOperation

from app.modules.miniapp_generation_runtime.generation_shell_contract import MiniappGenerationShellContract
from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner

CANONICAL_ENDPOINT_ALIASES: dict[str, str] = {}


class MiniappGenerationContractFrontend(MiniappGenerationRuntimeOwner):
    _STATUS_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("not_started", ("scheduled", "open", "new", "created", "accepted", "pending", "submitted", "queued", "draft")),
        ("in_progress", ("in_progress", "started", "working", "processing", "active", "claimed", "assigned", "in_review", "issued")),
        ("completed", ("completed", "done", "finished", "closed", "resolved", "approved", "returned")),
        ("cancelled", ("cancelled", "canceled", "rejected")),
    )
    _STATUS_UI_ALIAS_PREFERENCE: dict[str, tuple[str, ...]] = {
        "not_started": ("open", "new", "scheduled", "pending", "created", "accepted", "submitted", "queued", "draft"),
        "in_progress": ("in_progress", "processing", "started", "working", "claimed", "assigned", "in_review", "issued"),
        "completed": ("completed", "done", "finished", "closed", "resolved", "approved", "returned"),
        "cancelled": ("cancelled", "canceled", "rejected"),
    }

    @staticmethod
    def _compact_slug(value: str) -> str:
        text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
        text = re.sub(r"[^A-Za-z0-9]+", "", text)
        return text.lower()

    @classmethod
    def _entity_api_aliases(cls, entity_contract: dict[str, Any] | None) -> tuple[str, set[str]]:
        contract = dict(entity_contract or {})
        canonical_api_path = str(contract.get("api_path") or "").strip()
        canonical_match = re.match(r"^/api/([A-Za-z0-9_-]+)", canonical_api_path)
        canonical_slug = str(canonical_match.group(1) if canonical_match else "").strip().lower()
        aliases: set[str] = set()
        for key in (
            "entity_slug",
            "entity_slug_plural",
            "detail_route_slug",
            "entity_name",
            "schema_prefix",
            "model_prefix",
            "singular_label",
            "plural_label",
        ):
            compact = cls._compact_slug(str(contract.get(key) or ""))
            if not compact:
                continue
            aliases.add(compact)
            if not compact.endswith("s"):
                aliases.add(f"{compact}s")
        route_file = str(contract.get("route_file") or "").strip().replace("\\", "/")
        route_stem = Path(route_file).stem if route_file else ""
        compact_route_stem = cls._compact_slug(route_stem)
        if compact_route_stem:
            aliases.add(compact_route_stem)
        aliases.update({"record", "records", "item", "items", "workflow", "workflows"})
        aliases.discard("")
        if canonical_slug:
            aliases.discard(canonical_slug)
            aliases.discard(cls._compact_slug(canonical_slug))
        return canonical_api_path, aliases

    @staticmethod
    def _clean_static_ui_text_artifacts(content: str) -> str:
        updated = str(content or "")
        updated = re.sub(
            r'(<[^>]*class=["\'][^"\']*\bchevron\b[^"\']*["\'][^>]*>)\s*(?:›|&rsaquo;|&#x?203a;?|203a|\d{2,};?)\s*(</[^>]+>)',
            r"\1\2",
            updated,
            flags=re.IGNORECASE,
        )
        updated = re.sub(
            r">\s*\d{2,};\s*<",
            "><",
            updated,
            flags=re.IGNORECASE,
        )
        updated = re.sub(
            r">\s*(?:Block|Section|Card)\s+\d{2,}\s*<",
            "><",
            updated,
            flags=re.IGNORECASE,
        )
        updated = re.sub(
            r"([\"'])\s*(?:Block|Section|Card)\s+\d{2,}\s*\1",
            r"\1\1",
            updated,
            flags=re.IGNORECASE,
        )
        updated = re.sub(
            r">\s*(?:Loading\b[^<]{0,120}|(?:Couldn['’]?t|Could\s+not|Unable\s+to)\s+load\b[^<]{0,160})\s*<",
            "><",
            updated,
            flags=re.IGNORECASE,
        )
        updated = re.sub(
            r"(\b(?:textContent|innerText)\s*=\s*)([\"'])\s*(?:Loading\b[^\"']{0,120}|(?:Couldn['’]?t|Could\s+not|Unable\s+to)\s+load\b[^\"']{0,160})\s*\2",
            r"\1\2\2",
            updated,
            flags=re.IGNORECASE,
        )
        updated = re.sub(
            r"<(?P<tag>div|span|p)(?![^>]*\b(?:aria-label|title)\s*=)[^>]*class=[\"'][^\"']*\b(?:chip|pill|badge|tag|indicator|dot)\b[^\"']*[\"'][^>]*>\s*(?:&nbsp;|&#160;|&#8203;|<!--.*?-->)*\s*</(?P=tag)>",
            "",
            updated,
            flags=re.IGNORECASE | re.DOTALL,
        )
        updated = re.sub(
            r"<(?P<tag>div|section|p)(?![^>]*\b(?:id|aria-live)\s*=)[^>]*class=[\"'][^\"']*\b(?:message|notice|status|indicator|legend|feedback|alert)\b[^\"']*[\"'][^>]*>\s*</(?P=tag)>",
            "",
            updated,
            flags=re.IGNORECASE | re.DOTALL,
        )
        updated = re.sub(
            r"(\$\{\s*formatDate\([^}]+\)\s*\})\s+\d{2,}\s+(\$\{\s*formatDate\([^}]+\)\s*\})",
            r"\1 - \2",
            updated,
            flags=re.IGNORECASE,
        )
        updated = re.sub(
            r"(\b(?:textContent|innerText)\s*=\s*['\"])([^'\"]*[A-Za-z][A-Za-z\s.,:;!?-]*[a-z])\d{2,}([^'\"]*['\"])",
            r"\1\2\3",
            updated,
            flags=re.IGNORECASE,
        )
        updated = re.sub(
            r"(>\s*(?:Loading|Saving|Submitting|Syncing|Refreshing|Updating|Fetching|Sending|Processing|Preparing)\b[^<]*?[A-Za-z])\d{2,}(\s*<)",
            r"\1\2",
            updated,
            flags=re.IGNORECASE,
        )
        return updated

    @classmethod
    def _normalize_entity_api_paths(cls, content: str, entity_contract: dict[str, Any] | None) -> str:
        canonical_api_path, aliases = cls._entity_api_aliases(entity_contract)
        updated = str(content or "")
        if not canonical_api_path or not aliases:
            return updated
        for alias in sorted(aliases, key=len, reverse=True):
            updated = re.sub(
                rf"/api/{re.escape(alias)}(?=(?:[/?\"'`]|$))",
                canonical_api_path,
                updated,
                flags=re.IGNORECASE,
            )
        return updated

    @classmethod
    def _entity_status_literals(cls, entity_contract: dict[str, Any] | None) -> list[str]:
        seen: list[str] = []
        for raw_value in list((entity_contract or {}).get("status_literals") or []):
            value = str(raw_value or "").strip().lower()
            if value and value not in seen:
                seen.append(value)
        return seen

    @staticmethod
    def _schema_status_literals_from_text(content: str) -> list[str]:
        source = str(content or "")
        patterns = (
            re.compile(r"\bRecordStatus\s*=\s*Literal\[(?P<body>[^\]]+)\]", flags=re.IGNORECASE | re.DOTALL),
            re.compile(r"\bstatus\s*:\s*Literal\[(?P<body>[^\]]+)\]", flags=re.IGNORECASE | re.DOTALL),
        )
        for pattern in patterns:
            match = pattern.search(source)
            if not match:
                continue
            body = str(match.group("body") or "")
            values = [value.strip().lower() for value in re.findall(r'["\']([^"\']+)["\']', body)]
            if values:
                return list(dict.fromkeys(values))
        return []

    def _effective_entity_contract(
        self,
        workspace_id: str,
        draft_run_id: str,
        operation_map: dict[str, DraftFileOperation],
        entity_contract: dict[str, Any] | None,
    ) -> dict[str, Any]:
        effective_contract = dict(entity_contract or {})
        schema_content = self._operation_or_workspace_content(
            workspace_id,
            draft_run_id,
            operation_map,
            "miniapp/app/schemas.py",
        )
        schema_status_literals = self._schema_status_literals_from_text(schema_content or "")
        if schema_status_literals:
            effective_contract["status_literals"] = schema_status_literals
        return effective_contract

    @classmethod
    def _status_literals_referenced_in_text(cls, content: str) -> set[str]:
        lowered = str(content or "").lower()
        found: set[str] = set()
        for _family_name, candidates in cls._STATUS_FAMILIES:
            for candidate in candidates:
                patterns = (
                    rf'["\'`]{re.escape(candidate)}["\'`]',
                    rf"\bdata-status=[\"']{re.escape(candidate)}[\"']",
                )
                if any(re.search(pattern, lowered) for pattern in patterns):
                    found.add(candidate)
        return found

    @classmethod
    def _status_read_alias_map(cls, content: str, entity_contract: dict[str, Any] | None) -> dict[str, str]:
        allowed = cls._entity_status_literals(entity_contract)
        if not allowed:
            return {}
        allowed_set = set(allowed)
        referenced = cls._status_literals_referenced_in_text(content)
        mapping: dict[str, str] = {}
        for family_name, candidates in cls._STATUS_FAMILIES:
            allowed_candidates = [candidate for candidate in allowed if candidate in candidates]
            if not allowed_candidates:
                continue
            alias = next(
                (
                    candidate
                    for candidate in cls._STATUS_UI_ALIAS_PREFERENCE.get(family_name, candidates)
                    if candidate in referenced and candidate not in allowed_set
                ),
                None,
            )
            if not alias:
                continue
            canonical = allowed_candidates[0]
            if canonical != alias:
                mapping[canonical] = alias
        return mapping

    @classmethod
    def _status_alias_bridge_source(cls, read_alias_map: dict[str, str]) -> str:
        payload = json.dumps(read_alias_map, ensure_ascii=False, sort_keys=True)
        return (
            "// grounded-status-alias-bridge:start\n"
            f"const __GROUND_STATUS_READ_ALIASES__ = {payload};\n"
            "function __groundNormalizeStatusValue(value) {\n"
            "  if (typeof value !== \"string\") {\n"
            "    return value;\n"
            "  }\n"
            "  const normalized = value.trim().toLowerCase();\n"
            "  return __GROUND_STATUS_READ_ALIASES__[normalized] ?? normalized;\n"
            "}\n"
            "function __groundNormalizeStatusPayload(payload) {\n"
            "  if (Array.isArray(payload)) {\n"
            "    return payload.map((item) => __groundNormalizeStatusPayload(item));\n"
            "  }\n"
            "  if (!payload || typeof payload !== \"object\") {\n"
            "    return payload;\n"
            "  }\n"
            "  const result = {};\n"
            "  Object.entries(payload).forEach(([key, value]) => {\n"
            "    if (key === \"status\") {\n"
            "      result[key] = __groundNormalizeStatusValue(value);\n"
            "      return;\n"
            "    }\n"
            "    result[key] = __groundNormalizeStatusPayload(value);\n"
            "  });\n"
            "  return result;\n"
            "}\n"
            "// grounded-status-alias-bridge:end\n\n"
        )

    @classmethod
    def _inject_status_alias_bridge(cls, file_path: str, content: str, entity_contract: dict[str, Any] | None) -> str:
        if not str(file_path or "").endswith(".js"):
            return str(content or "")
        updated = str(content or "")
        read_alias_map = cls._status_read_alias_map(updated, entity_contract)
        if not read_alias_map:
            return updated
        bridge_block = cls._status_alias_bridge_source(read_alias_map)
        bridge_pattern = re.compile(
            r"// grounded-status-alias-bridge:start\n.*?// grounded-status-alias-bridge:end\n\n?",
            flags=re.DOTALL,
        )
        if bridge_pattern.search(updated):
            updated = re.sub(bridge_pattern, bridge_block, updated, count=1)
        else:
            updated = f"{bridge_block}{updated}"
        updated = re.sub(
            r"(?<!__groundNormalizeStatusPayload\()await\s+([A-Za-z_$][\w$.]*)\.json\(\)",
            lambda match: f"__groundNormalizeStatusPayload(await {match.group(1)}.json())",
            updated,
        )
        return updated

    @staticmethod
    def _route_aliases(route: str) -> set[str]:
        normalized = str(route or "").strip()
        if not normalized:
            return set()
        aliases = {normalized}
        if normalized.endswith("_detail"):
            aliases.add(normalized[: -len("_detail")])
        if normalized.endswith("-detail"):
            aliases.add(normalized[: -len("-detail")])
        if normalized.endswith("detail"):
            aliases.add(normalized[: -len("detail")].rstrip("_-"))
        return {alias for alias in aliases if alias}

    @staticmethod
    def _template_app_root() -> Path:
        return Path(__file__).resolve().parents[5] / "runtime" / "templates" / "base-miniapp" / "miniapp" / "app"

    @classmethod
    def _template_source_for_path(cls, file_path: str) -> str | None:
        normalized = str(file_path or "").strip().replace("\\", "/")
        if not normalized:
            return None
        template_path = cls._template_app_root() / normalized.removeprefix("miniapp/app/")
        if not template_path.exists():
            return None
        try:
            return template_path.read_text(encoding="utf-8")
        except OSError:
            return None

    @classmethod
    def _needs_shared_base_stylesheet_repair(cls, content: str) -> bool:
        normalized = str(content or "")
        if not normalized:
            return False
        if ".page-shell" not in normalized:
            return True
        if "padding-top: 76px" not in normalized and "padding-top: max(76px" not in normalized:
            return True
        if "--telegram-top-safe-offset" not in normalized:
            return True
        return False

    @classmethod
    def _needs_frontend_api_contract_repair(cls, file_path: str, content: str) -> bool:
        normalized = str(content or "")
        if not normalized:
            return False
        if cls._normalize_api_aliases_in_text(normalized) != normalized:
            return True
        if file_path.endswith(".js"):
            if re.search(r"(?<![\w.])fetch\(\s*([\"'`])/api/", normalized):
                return True
            if "window.fetch" in normalized and ("runtime.fetchJson" in normalized or "miniappApiFetch" in normalized):
                return True
        return False

    @classmethod
    def _needs_basic_page_state_contract_repair(
        cls,
        html: str,
        *,
        role: str,
        declared_routes: set[str],
        expected_style_href: str,
        expected_script_src: str,
    ) -> bool:
        updated = cls._normalize_api_aliases_in_text(html)
        if updated != html:
            return True
        if MiniappGenerationShellContract.BASE_STYLESHEET_HREF not in updated:
            return True
        if expected_style_href and expected_style_href not in updated:
            return True
        if expected_script_src and expected_script_src not in updated:
            return True
        if MiniappGenerationShellContract.PREVIEW_BRIDGE_SRC not in updated:
            return True
        if MiniappGenerationShellContract.PAGE_SHELL_CLASS not in updated:
            return True
        if MiniappGenerationShellContract.PAGE_SHELL_INLINE_STYLE not in updated:
            return True
        if role:
            normalized_links = cls._normalize_role_local_links(updated, role=role, declared_routes=declared_routes)
            if normalized_links != updated:
                return True
        return False

    @staticmethod
    def _strip_mock_profile_names(content: str) -> str:
        updated = str(content or "")
        updated = updated.replace("Ivan Ivanov", "Complete profile")
        updated = updated.replace("ivan ivanov", "complete profile")
        return updated

    @classmethod
    def _normalize_api_aliases_in_text(cls, content: str) -> str:
        updated = cls._strip_mock_profile_names(content)
        updated = cls._strip_noncanonical_shared_base_html_refs(updated)
        updated = cls._strip_noncanonical_shared_base_style_imports(updated)
        updated = cls._strip_noncanonical_preview_bridge_html_refs(updated)
        updated = cls._normalize_preview_bridge_script_imports(updated)
        return updated

    @classmethod
    def _strip_noncanonical_shared_base_html_refs(cls, content: str) -> str:
        pattern = re.compile(
            r"""\s*<link\b[^>]*href=["'](?P<href>[^"']*shared/base\.css(?:[?#][^"']*)?)["'][^>]*>\s*""",
            flags=re.IGNORECASE,
        )

        def _replace(match: re.Match[str]) -> str:
            href = str(match.group("href") or "").strip()
            if href == MiniappGenerationShellContract.BASE_STYLESHEET_HREF:
                return match.group(0)
            return ""

        return re.sub(pattern, _replace, content)

    @classmethod
    def _strip_noncanonical_shared_base_style_imports(cls, content: str) -> str:
        updated = str(content or "")
        patterns = (
            re.compile(
                r"""(?m)^\s*import\s+["'](?!/static/shared/base\.css(?:[?#][^"']*)?)[^"']*shared/base\.css(?:[?#][^"']*)?["']\s*;?\s*$"""
            ),
            re.compile(
                r"""(?m)^\s*@import\s+["'](?!/static/shared/base\.css(?:[?#][^"']*)?)[^"']*shared/base\.css(?:[?#][^"']*)?["']\s*;?\s*$"""
            ),
        )
        for pattern in patterns:
            updated = re.sub(pattern, "", updated)
        return updated

    @classmethod
    def _strip_noncanonical_preview_bridge_html_refs(cls, content: str) -> str:
        pattern = re.compile(
            r"""\s*<script\b[^>]*src=["'](?!/static/preview_bridge\.js(?:[?#][^"']*)?)[^"']*preview_bridge\.js(?:[?#][^"']*)?["'][^>]*>\s*</script>""",
            flags=re.IGNORECASE,
        )
        return re.sub(pattern, "", content)

    @classmethod
    def _normalize_preview_bridge_script_imports(cls, content: str) -> str:
        updated = str(content or "")
        named_import_pattern = re.compile(
            r"""(?m)^\s*import\s*\{(?P<bindings>[^}]+)\}\s*from\s*["'][^"']*preview_bridge\.js(?:[?#][^"']*)?["']\s*;?\s*$"""
        )
        bare_import_pattern = re.compile(
            r"""(?m)^\s*import\s*["'][^"']*preview_bridge\.js(?:[?#][^"']*)?["']\s*;?\s*$"""
        )

        def _replace_named_import(match: re.Match[str]) -> str:
            bindings = cls._window_destructuring_bindings(match.group("bindings"))
            if not bindings:
                return ""
            return f"const {{ {bindings} }} = window;"

        updated = re.sub(named_import_pattern, _replace_named_import, updated)
        updated = re.sub(bare_import_pattern, "", updated)
        return updated

    @staticmethod
    def _window_destructuring_bindings(bindings: str) -> str:
        normalized: list[str] = []
        for raw_item in str(bindings or "").split(","):
            item = raw_item.strip()
            if not item:
                continue
            alias_match = re.fullmatch(r"([A-Za-z_$][\w$]*)\s+as\s+([A-Za-z_$][\w$]*)", item)
            if alias_match:
                normalized.append(f"{alias_match.group(1)}: {alias_match.group(2)}")
                continue
            normalized.append(item)
        return ", ".join(normalized)

    @staticmethod
    def _normalize_local_route_ref(route_ref: str) -> str:
        normalized = str(route_ref or "").strip()
        if not normalized:
            return normalized
        normalized = re.sub(r"\$\{[^/]+\}", "sample", normalized)
        normalized = re.sub(r"\{[^/]+\}", "sample", normalized)
        normalized = re.sub(r":[^/]+", "sample", normalized)
        if normalized != "/" and normalized.endswith("/"):
            normalized = normalized.rstrip("/")
        return normalized

    @staticmethod
    def _roleless_route(route_ref: str, *, role: str) -> str:
        normalized = MiniappGenerationContractFrontend._normalize_local_route_ref(route_ref)
        if normalized.startswith(f"/{role}"):
            trimmed = normalized[len(role) + 1 :]
            if not trimmed:
                return "/"
            return trimmed if trimmed.startswith("/") else f"/{trimmed}"
        return normalized

    @classmethod
    def _route_family_key(cls, route_ref: str, *, role: str) -> str:
        normalized = cls._roleless_route(route_ref, role=role).strip("/")
        if not normalized:
            return "/"
        first = normalized.split("/", 1)[0]
        if not first or first == "sample":
            return "/"
        return f"/{first}"

    @staticmethod
    def _route_looks_like_detail(route_ref: str) -> bool:
        normalized = MiniappGenerationContractFrontend._normalize_local_route_ref(route_ref)
        if any(marker in normalized for marker in ("{", "}", ":")):
            return True
        segments = [segment for segment in normalized.strip("/").split("/") if segment]
        return len(segments) >= 2 and segments[-1] not in {"new", "create", "edit"}

    @classmethod
    def _preferred_declared_family_route(
        cls,
        *,
        route_ref: str,
        role: str,
        family_candidates: list[str],
    ) -> str | None:
        if not family_candidates:
            return None
        normalized = cls._roleless_route(route_ref, role=role)
        wants_create = normalized.endswith("/new") or normalized.endswith("/create")
        wants_detail = cls._route_looks_like_detail(normalized) and not wants_create
        list_candidates = [candidate for candidate in family_candidates if not cls._route_looks_like_detail(candidate)]
        detail_candidates = [candidate for candidate in family_candidates if cls._route_looks_like_detail(candidate)]
        if wants_create and list_candidates:
            return min(list_candidates, key=len)
        if wants_detail and detail_candidates:
            return max(detail_candidates, key=len)
        if list_candidates:
            return min(list_candidates, key=len)
        return min(family_candidates, key=len)

    def _synchronize_frontend_api_contract(
        self,
        workspace_id: str,
        draft_run_id: str,
        operations: list[DraftFileOperation],
        entity_contract: dict[str, Any] | None = None,
        role_scope: list[str] | None = None,
        contract_sync_mode: str = "repair_invariants",
    ) -> list[DraftFileOperation]:
        operation_map = {operation.file_path: operation for operation in operations}
        effective_entity_contract = self._effective_entity_contract(workspace_id, draft_run_id, operation_map, entity_contract)
        target_paths = set(operation_map)
        scoped_roles = {str(role).strip() for role in (role_scope or []) if str(role).strip()}
        for entry in self.workspace_service.file_tree(workspace_id, run_id=draft_run_id):
            file_path = str(entry.get("path") or "")
            if entry.get("type") != "file":
                continue
            if not (file_path.startswith("miniapp/app/static/") and (file_path.endswith(".js") or file_path.endswith(".html"))):
                continue
            if not scoped_roles or any(file_path.startswith(f"miniapp/app/static/{role}/") for role in scoped_roles):
                target_paths.add(file_path)
            else:
                content = self._operation_or_workspace_content(workspace_id, draft_run_id, operation_map, file_path)
                if content and self._clean_static_ui_text_artifacts(content) != content:
                    target_paths.add(file_path)
        for file_path in sorted(target_paths):
            if not (file_path.startswith("miniapp/app/static/") and (file_path.endswith(".js") or file_path.endswith(".html"))):
                continue
            content = self._operation_or_workspace_content(workspace_id, draft_run_id, operation_map, file_path)
            if not content:
                continue
            updated = self._normalize_api_aliases_in_text(content)
            updated = self._normalize_entity_api_paths(updated, effective_entity_contract)
            updated = self._inject_status_alias_bridge(file_path, updated, effective_entity_contract)
            updated = self._clean_static_ui_text_artifacts(updated)
            if not self._needs_frontend_api_contract_repair(file_path, content) and updated == content:
                continue
            if file_path.endswith(".html"):
                if updated == content:
                    continue
                operation_map[file_path] = DraftFileOperation(
                    file_path=file_path,
                    operation="replace",
                    content=updated,
                    reason="Pre-apply contract sync: canonicalize frontend API aliases and clean static UI text artifacts before runtime checks.",
                )
                continue
            updated = re.sub(r"(?<![\w.])fetch\(\s*([\"'`])/api/", r"window.miniappApiFetch(\1/api/", updated)
            updated = re.sub(
                r"const\s+fetchJson\s*=\s*runtime\.fetchJson\s*\?\?\s*\(window\.fetch\s*\?\s*\(\(url,\s*options\s*=\s*\{\}\)\s*=>\s*window\.fetch\(url,\s*options\)\)\s*:\s*null\);",
                "const fetchJson = runtime.fetchJson ?? null;",
                updated,
            )
            updated = re.sub(
                r"const\s+apiFetch\s*=\s*window\.miniappApiFetch\s*\?\?\s*fetchJson\s*\?\?\s*\(window\.fetch\s*\?\s*\(\(url,\s*options\s*=\s*\{\}\)\s*=>\s*window\.fetch\(url,\s*options\)\)\s*:\s*null\);",
                "const apiFetch = window.miniappApiFetch ?? fetchJson;",
                updated,
            )
            updated = re.sub(
                r"const\s+runtimeFetch\s*=\s*window\.miniappApiFetch\s*\?\?\s*runtime\.fetchJson\s*\?\?\s*\(window\.fetch\s*\?\s*\(\(url,\s*options\s*=\s*\{\}\)\s*=>\s*window\.fetch\(url,\s*options\)\)\s*:\s*null\);",
                "const runtimeFetch = window.miniappApiFetch ?? runtime.fetchJson ?? null;",
                updated,
            )
            if updated == content:
                continue
            operation_map[file_path] = DraftFileOperation(
                file_path=file_path,
                operation="replace",
                content=updated,
                reason="Pre-apply contract sync: route frontend API calls through the preview-aware shared fetch helper.",
            )
        return list(operation_map.values())

    @staticmethod
    def _canonicalize_local_role_links_in_text(content: str) -> str:
        return MiniappRuntimeContractSync.canonicalize_local_role_links_in_text(content)

    def _synchronize_frontend_navigation_contract(
        self,
        workspace_id: str,
        draft_run_id: str,
        operations: list[DraftFileOperation],
    ) -> list[DraftFileOperation]:
        return self.runtime_contract_sync.synchronize_frontend_navigation_contract(
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            operations=operations,
        )

    def _synchronize_basic_page_state_contract(
        self,
        workspace_id: str,
        draft_run_id: str,
        *,
        page_graph: dict[str, Any],
        operations: list[DraftFileOperation],
        contract_sync_mode: str = "repair_invariants",
    ) -> list[DraftFileOperation]:
        operation_map = {operation.file_path: operation for operation in operations}
        base_stylesheet_path = MiniappGenerationShellContract.BASE_STYLESHEET_PATH
        base_stylesheet_content = self._operation_or_workspace_content(
            workspace_id,
            draft_run_id,
            operation_map,
            base_stylesheet_path,
        )
        if self._needs_shared_base_stylesheet_repair(base_stylesheet_content or ""):
            template_base_stylesheet = self._template_source_for_path(base_stylesheet_path)
            if template_base_stylesheet:
                operation_map[base_stylesheet_path] = DraftFileOperation(
                    file_path=base_stylesheet_path,
                    operation="replace",
                    content=template_base_stylesheet,
                    reason="Pre-apply contract sync: restore the shared shell stylesheet so page-shell spacing and preview safe-area invariants stay intact.",
                )
        page_expectations: dict[str, dict[str, str]] = {}
        page_assets: dict[str, dict[str, str]] = {}
        role_routes: dict[str, set[str]] = {}
        for role_payload in (page_graph.get("roles") or {}).values():
            if not isinstance(role_payload, dict):
                continue
            for page in role_payload.get("pages") or []:
                if not isinstance(page, dict):
                    continue
                file_path = str(page.get("file_path") or "").strip()
                if not file_path:
                    continue
                page_expectations[file_path] = {
                    "loading_state": str(page.get("loading_state") or "").strip(),
                    "error_state": str(page.get("error_state") or "").strip(),
                }
                page_assets[file_path] = {
                    "style_path": str(page.get("style_path") or self._default_page_asset_path(file_path, asset_kind="css")).strip(),
                    "script_path": str(page.get("script_path") or self._default_page_asset_path(file_path, asset_kind="js")).strip(),
                }
                role_match = re.match(r"miniapp/app/static/(client|specialist|manager)/", file_path)
                if role_match:
                    role = role_match.group(1)
                    route_path = str(page.get("route_path") or "").strip()
                    if route_path:
                        normalized_route = self._absolute_role_route_path(
                            role,
                            self._normalize_role_route_path(role, route_path, index=0),
                        )
                        role_routes.setdefault(role, set()).add(self._normalize_local_route_ref(normalized_route))
        for file_path in list(operation_map):
            if not (file_path.startswith("miniapp/app/static/") and file_path.endswith(".html")):
                continue
            script_path = self._default_page_asset_path(file_path, asset_kind="js")
            html = self._operation_or_workspace_content(workspace_id, draft_run_id, operation_map, file_path)
            script = self._operation_or_workspace_content(workspace_id, draft_run_id, operation_map, script_path)
            if not html:
                continue
            updated = self._normalize_api_aliases_in_text(html)
            role_match = re.match(r"miniapp/app/static/(client|specialist|manager)/", file_path)
            role = role_match.group(1) if role_match else ""
            asset_meta = page_assets.get(file_path) or {
                "style_path": self._default_page_asset_path(file_path, asset_kind="css"),
                "script_path": self._default_page_asset_path(file_path, asset_kind="js"),
            }
            expected_style_href = self._static_asset_href(asset_meta["style_path"])
            expected_script_src = self._static_asset_href(asset_meta["script_path"])
            if not self._needs_basic_page_state_contract_repair(
                html,
                role=role,
                declared_routes=role_routes.get(role) or set(),
                expected_style_href=expected_style_href,
                expected_script_src=expected_script_src,
            ):
                continue
            updated = updated.replace("/static/shell.css", MiniappGenerationShellContract.BASE_STYLESHEET_HREF)
            updated = self.generation_shell_contract.ensure_base_stylesheet_ref(updated)
            updated = self.generation_shell_contract.ensure_preview_bridge_ref(updated)
            updated = self._ensure_head_asset_link(updated, expected_style_href)
            updated = self._ensure_body_script_ref(updated, expected_script_src)
            updated = self.generation_shell_contract.ensure_page_shell_contract(updated)
            if role:
                updated = self._normalize_role_local_links(updated, role=role, declared_routes=role_routes.get(role) or set())
            expectation = page_expectations.get(file_path) or {}
            del expectation
            updated = re.sub(r">\s*Refresh\s*<", ">Reload<", updated, flags=re.IGNORECASE)
            if updated == html:
                continue
            operation_map[file_path] = DraftFileOperation(
                file_path=file_path,
                operation="replace",
                content=updated,
                reason="Pre-apply contract sync: normalize the shared shell/runtime contract without injecting synthetic DOM placeholders.",
            )
        return list(operation_map.values())

    @staticmethod
    def _ensure_fastapi_import_symbol(content: str, symbol: str) -> str:
        match = re.search(r"from\s+fastapi\s+import\s+([^\n]+)", content)
        if not match:
            return content
        imported = [item.strip() for item in match.group(1).split(",") if item.strip()]
        if symbol in imported:
            return content
        imported.append(symbol)
        replacement = f"from fastapi import {', '.join(dict.fromkeys(imported))}"
        return content[: match.start()] + replacement + content[match.end() :]

    @staticmethod
    def _inject_head_asset_link(html: str, tag: str) -> str:
        if "</head>" in html:
            return html.replace("</head>", f"    {tag}\n</head>", 1)
        return f"{tag}\n{html}"

    @classmethod
    def _ensure_head_asset_link(cls, html: str, href: str) -> str:
        if not href or href in html:
            return html
        tag = f'<link rel="stylesheet" href="{href}" />'
        return cls._inject_head_asset_link(html, tag)

    @staticmethod
    def _ensure_body_script_ref(html: str, src: str) -> str:
        if not src or src in html:
            return html
        tag = f'<script src="{src}" defer></script>'
        if "</body>" in html:
            return html.replace("</body>", f"    {tag}\n</body>", 1)
        return f"{html}\n{tag}"

    @staticmethod
    def _ensure_preview_bridge_ref(html: str) -> str:
        return MiniappGenerationShellContract.ensure_preview_bridge_ref(html)

    @staticmethod
    def _ensure_page_shell_contract(html: str) -> str:
        return MiniappGenerationShellContract.ensure_page_shell_contract(html)

    @classmethod
    def _ensure_html_dom_ids_for_script(cls, html: str, script: str | None) -> str:
        return html

    @staticmethod
    def _static_asset_href(asset_path_raw: str) -> str:
        normalized = str(asset_path_raw or "").strip().replace("\\", "/")
        if not normalized:
            return ""
        prefix = "miniapp/app/"
        if normalized.startswith(prefix):
            return "/" + normalized[len(prefix):]
        if normalized.startswith("app/"):
            return "/" + normalized
        if normalized.startswith("/"):
            return normalized
        return "/" + normalized

    @classmethod
    def _normalize_role_local_links(cls, html: str, *, role: str, declared_routes: set[str]) -> str:
        if not role:
            return html
        replacements: dict[str, str] = {}
        family_replacements: dict[str, list[str]] = {}
        for candidate in declared_routes:
            if not candidate.startswith(f"/{role}"):
                continue
            short = candidate[len(role) + 1 :]
            short = short or "/"
            for alias in cls._route_aliases(short):
                replacements[alias] = candidate
            family_key = cls._route_family_key(short, role=role)
            family_replacements.setdefault(family_key, []).append(candidate)
        replacements["/"] = f"/{role}"

        def _replace(match: re.Match[str]) -> str:
            quote = match.group(1)
            route = match.group(2)
            normalized = cls._normalize_local_route_ref(route)
            replacement = replacements.get(normalized)
            if replacement is None and not normalized.startswith(f"/{role}"):
                replacement = replacements.get(normalized.rstrip("/"))
            if replacement is None:
                family_key = cls._route_family_key(normalized, role=role)
                replacement = cls._preferred_declared_family_route(
                    route_ref=normalized,
                    role=role,
                    family_candidates=list(family_replacements.get(family_key) or []),
                )
            if replacement is None:
                return match.group(0)
            return f'href={quote}{replacement}{quote}'

        return re.sub(r"""href=(["'])(/(?!api/|static/)[^"']*)\1""", _replace, html)
