from __future__ import annotations

import re
from typing import Any

from app.modules.miniapp_contract.runtime_contract_sync import MiniappRuntimeContractSync
from app.models.domain import DraftFileOperation

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner

CANONICAL_ENDPOINT_ALIASES: dict[str, str] = {}


class MiniappGenerationContractFrontend(MiniappGenerationRuntimeOwner):
    @classmethod
    def _needs_frontend_api_contract_repair(cls, file_path: str, content: str) -> bool:
        normalized = str(content or "")
        if not normalized:
            return False
        if cls._strip_mock_profile_names(normalized) != normalized:
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
        updated = cls._strip_mock_profile_names(html)
        if updated != html:
            return True
        if "/static/shared/base.css" not in updated:
            return True
        if expected_style_href and expected_style_href not in updated:
            return True
        if expected_script_src and expected_script_src not in updated:
            return True
        if "/static/preview_bridge.js" not in updated:
            return True
        if "page-shell" not in updated:
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
        return cls._strip_mock_profile_names(content)

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

    def _synchronize_frontend_api_contract(
        self,
        workspace_id: str,
        draft_run_id: str,
        operations: list[DraftFileOperation],
        contract_sync_mode: str = "repair_invariants",
    ) -> list[DraftFileOperation]:
        operation_map = {operation.file_path: operation for operation in operations}
        for file_path in list(operation_map):
            if not (file_path.startswith("miniapp/app/static/") and (file_path.endswith(".js") or file_path.endswith(".html"))):
                continue
            content = self._operation_or_workspace_content(workspace_id, draft_run_id, operation_map, file_path)
            if not content:
                continue
            if not self._needs_frontend_api_contract_repair(file_path, content):
                continue
            updated = self._normalize_api_aliases_in_text(content)
            if file_path.endswith(".html"):
                if updated == content:
                    continue
                operation_map[file_path] = DraftFileOperation(
                    file_path=file_path,
                    operation="replace",
                    content=updated,
                    reason="Pre-apply contract sync: canonicalize frontend API aliases before runtime checks.",
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
            updated = self._strip_mock_profile_names(html)
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
            updated = updated.replace("/static/shell.css", "/static/shared/base.css")
            if "/static/shared/base.css" not in updated:
                updated = self._inject_head_asset_link(updated, '<link rel="stylesheet" href="/static/shared/base.css" />')
            updated = self._ensure_preview_bridge_ref(updated)
            updated = self._ensure_head_asset_link(updated, expected_style_href)
            updated = self._ensure_body_script_ref(updated, expected_script_src)
            updated = self._ensure_page_shell_contract(updated)
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
        src = "/static/preview_bridge.js"
        if src in html:
            return html
        tag = f'<script src="{src}" defer></script>'
        body_match = re.search(r"</body>", html, flags=re.IGNORECASE)
        if not body_match:
            return f"{html}\n{tag}"
        script_match = re.search(r"<script[^>]+src=[\"'][^\"']+[\"'][^>]*></script>", html, flags=re.IGNORECASE)
        if script_match:
            return html[: script_match.start()] + f"    {tag}\n" + html[script_match.start() :]
        return html.replace("</body>", f"    {tag}\n</body>", 1)

    @staticmethod
    def _ensure_page_shell_contract(html: str) -> str:
        main_match = re.search(r"<main\b([^>]*)>", html, flags=re.IGNORECASE)
        if not main_match:
            return html
        attributes = main_match.group(1)
        updated_attrs = attributes
        class_match = re.search(r"""class=(["'])([^"']*)\1""", attributes, flags=re.IGNORECASE)
        if class_match:
            classes = [item for item in re.split(r"\s+", class_match.group(2).strip()) if item]
            if "page-shell" not in classes:
                classes.append("page-shell")
            replacement = f'class={class_match.group(1)}{" ".join(classes)}{class_match.group(1)}'
            updated_attrs = updated_attrs[: class_match.start()] + replacement + updated_attrs[class_match.end() :]
        else:
            updated_attrs += ' class="page-shell"'
        style_match = re.search(r"""style=(["'])([^"']*)\1""", updated_attrs, flags=re.IGNORECASE)
        shell_style = "padding-top: max(76px, calc(var(--telegram-top-safe-offset) + 12px));"
        if style_match:
            style_content = style_match.group(2)
            if "padding-top" not in style_content:
                suffix = "" if style_content.rstrip().endswith(";") or not style_content.strip() else ";"
                style_content = f"{style_content}{suffix} {shell_style}".strip()
                replacement = f'style={style_match.group(1)}{style_content}{style_match.group(1)}'
                updated_attrs = updated_attrs[: style_match.start()] + replacement + updated_attrs[style_match.end() :]
        else:
            updated_attrs += f' style="{shell_style}"'
        return html[: main_match.start()] + f"<main{updated_attrs}>" + html[main_match.end() :]

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
        for candidate in declared_routes:
            if not candidate.startswith(f"/{role}"):
                continue
            short = candidate[len(role) + 1 :]
            short = short or "/"
            replacements[short] = candidate
        replacements["/"] = f"/{role}"

        def _replace(match: re.Match[str]) -> str:
            quote = match.group(1)
            route = match.group(2)
            normalized = cls._normalize_local_route_ref(route)
            replacement = replacements.get(normalized)
            if replacement is None and not normalized.startswith(f"/{role}"):
                replacement = replacements.get(normalized.rstrip("/"))
            if replacement is None:
                return match.group(0)
            return f'href={quote}{replacement}{quote}'

        return re.sub(r"""href=(["'])(/(?!api/|static/)[^"']*)\1""", _replace, html)
