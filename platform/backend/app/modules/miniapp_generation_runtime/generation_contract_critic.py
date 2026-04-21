from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.models.common import GenerationMode
from app.models.domain import DraftFileOperation

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationContractCritic(MiniappGenerationRuntimeOwner):
    _FEATURE_ROUTE_EXCLUDED_STEMS = {
        "__init__",
        "client",
        "specialist",
        "manager",
        "profiles",
        "runtime",
        "users",
        "workload",
        "time_slots",
        "comments",
        "assignments",
        "role_pages",
        "health",
    }

    @staticmethod
    def _looks_like_live_collection_surface(html_content: str) -> bool:
        lowered = str(html_content or "").lower()
        markers = (
            "request-list",
            "queue-list",
            "workload-list",
            "approval-list",
            "availability-list",
            "conflict-list",
            "summary-card",
            "metric-card",
            "empty-state",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _contains_seeded_live_collection(script_content: str) -> bool:
        lowered = str(script_content or "").lower()
        collection_patterns = (
            r"\bconst\s+(?:requests|bookings|orders|tasks|tickets|cases|items)\s*=\s*\[\s*\{",
            r"\blet\s+(?:requests|bookings|orders|tasks|tickets|cases|items)\s*=\s*\[\s*\{",
            r"\bvar\s+(?:requests|bookings|orders|tasks|tickets|cases|items)\s*=\s*\[\s*\{",
        )
        if any(re.search(pattern, lowered) for pattern in collection_patterns):
            return True
        return bool(
            re.search(
                r"\[\s*\{[\s\S]{0,600}(?:reason|status|requester|start_date|end_date|assigned)[\s\S]{0,600}\}\s*\]",
                lowered,
            )
        )

    @classmethod
    def should_run_preapply_critic(
        cls,
        *,
        generation_mode: GenerationMode,
        operations: list[DraftFileOperation],
        entity_contract: dict[str, Any] | None,
    ) -> bool:
        if generation_mode in {GenerationMode.QUALITY, GenerationMode.BALANCED}:
            return True
        route_stem = str((entity_contract or {}).get("entity_slug_plural") or "").strip().lower()
        for operation in operations:
            if operation.content is None:
                continue
            normalized_path = str(operation.file_path or "").replace("\\", "/")
            content = str(operation.content or "").lower()
            if "/api/submissions/{table}" in content:
                return True
            if route_stem and normalized_path.endswith(f"/{route_stem}.py"):
                return True
            if normalized_path.endswith((".html", ".js")) and cls._contains_seeded_live_collection(content):
                return True
        return False

    def build_preapply_report(
        self,
        *,
        operations: list[DraftFileOperation],
        entity_contract: dict[str, Any] | None,
        generation_mode: GenerationMode,
    ) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        feature_route_stems: set[str] = set()
        feature_route_files: dict[str, str] = {}
        expected_route_file = str((entity_contract or {}).get("route_file") or "").replace("\\", "/")
        expected_route_stem = Path(expected_route_file).stem if expected_route_file else ""
        for operation in operations:
            if operation.content is None:
                continue
            normalized_path = str(operation.file_path or "").replace("\\", "/")
            content = str(operation.content or "")
            lowered = content.lower()
            if normalized_path.startswith("miniapp/app/routes/") and normalized_path.endswith(".py"):
                stem = Path(normalized_path).stem
                if stem not in self._FEATURE_ROUTE_EXCLUDED_STEMS:
                    feature_route_stems.add(stem)
                    feature_route_files[stem] = normalized_path
                if "/api/submissions/{table}" in lowered:
                    issues.append(
                        {
                            "code": "critic.generic_submission_shim",
                            "severity": "high",
                            "file_path": normalized_path,
                            "message": "Feature route regressed into a generic /api/submissions/{table} shim.",
                        }
                    )
            if normalized_path.endswith(".html"):
                companion_script_path = str(Path(normalized_path).with_name("app.js")).replace("\\", "/")
                script_operation = next(
                    (
                        item
                        for item in operations
                        if str(item.file_path or "").replace("\\", "/") == companion_script_path and item.content is not None
                    ),
                    None,
                )
                script_content = str(script_operation.content or "") if script_operation is not None else ""
                if self._looks_like_live_collection_surface(content) and self._contains_seeded_live_collection(script_content):
                    issues.append(
                        {
                            "code": "critic.seeded_live_collection",
                            "severity": "high",
                            "file_path": normalized_path,
                            "message": "Page renders a seeded business collection instead of relying on real API-backed state or an honest empty state.",
                        }
                    )
        if len(feature_route_stems) > 1:
            issues.append(
                {
                    "code": "critic.split_entity_routes",
                    "severity": "medium",
                    "message": (
                        "Draft introduces multiple feature route stems for what should usually be one dominant entity lifecycle: "
                        + ", ".join(sorted(feature_route_stems))
                    ),
                    "route_stems": sorted(feature_route_stems),
                    "implicated_files": [feature_route_files[stem] for stem in sorted(feature_route_stems) if stem in feature_route_files],
                }
            )
        if expected_route_stem and feature_route_stems and expected_route_stem not in feature_route_stems:
            issues.append(
                {
                    "code": "critic.entity_route_stem_drift",
                    "severity": "medium",
                    "message": (
                        f"Extracted entity contract expects the dominant feature route stem '{expected_route_stem}', "
                        f"but current draft feature route stems are {', '.join(sorted(feature_route_stems))}."
                    ),
                    "expected_route_stem": expected_route_stem,
                    "route_stems": sorted(feature_route_stems),
                    "implicated_files": list(
                        dict.fromkeys(
                            [
                                expected_route_file,
                                *[feature_route_files[stem] for stem in sorted(feature_route_stems) if stem in feature_route_files],
                            ]
                        )
                    ),
                }
            )
        implicated_files = list(
            dict.fromkeys(
                [
                    str(issue.get("file_path") or "").strip()
                    for issue in issues
                    if str(issue.get("file_path") or "").strip()
                ]
                + [
                    str(path or "").strip()
                    for issue in issues
                    for path in list(issue.get("implicated_files") or [])
                    if str(path or "").strip()
                ]
            )
        )
        return {
            "executed": True,
            "mode": generation_mode.value,
            "entity_contract": dict(entity_contract or {}),
            "issues": issues,
            "implicated_files": implicated_files,
            "issue_count": len(issues),
            "blocking_issue_count": sum(1 for item in issues if str(item.get("severity") or "") == "high"),
        }
