from __future__ import annotations

import json
import re
from typing import Any

from app.models.domain import CheckExecutionRecord


CONTEXT_PACK_SCHEMA = "grounded.agent_context_packs.v1"

NOISY_PATH_PATTERNS = (
    re.compile(r"(^|/)(__pycache__|node_modules|\.pytest_cache|\.git|dist|build|\.venv|venv)(/|$)"),
    re.compile(r"\.(png|jpg|jpeg|gif|webp|ico|pdf|zip|gz|sqlite|db)$", re.IGNORECASE),
    re.compile(r"(^|/)miniapp/app/generated/(route_manifest|contract_validator|miniapp_contract)\.json$"),
)

TEMPLATE_INVARIANTS = (
    "Do not edit generated route metadata directly; change source files so metadata can be regenerated.",
    "Keep role pages routeable for client, specialist, and manager when the product contract requires them.",
    "Do not seed runtime product records; empty states and test payloads are allowed.",
    "Keep browser workflow selectors, API payloads, backend schemas, and generated tests aligned.",
    "Use focused checks for the current failure first; run full acceptance only after focused proof passes.",
)


def _compact(value: Any, *, max_chars: int = 1200, max_items: int = 6, depth: int = 0) -> Any:
    if isinstance(value, str):
        return value if len(value) <= max_chars else f"{value[: max_chars // 2]}\n...[truncated {len(value) - max_chars} chars]...\n{value[-max_chars // 2 :]}"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 3:
        text = json.dumps(value, ensure_ascii=False, default=str)
        return _compact(text, max_chars=max_chars)
    if isinstance(value, list):
        compact = [_compact(item, max_chars=max_chars, max_items=max_items, depth=depth + 1) for item in value[:max_items]]
        if len(value) > max_items:
            compact.append({"truncated_items": len(value) - max_items})
        return compact
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                result["truncated_keys"] = len(value) - max_items
                break
            result[str(key)] = _compact(item, max_chars=max_chars, max_items=max_items, depth=depth + 1)
        return result
    return _compact(str(value), max_chars=max_chars)


def _path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _visible(path: object) -> bool:
    normalized = _path(path)
    return bool(normalized) and not any(pattern.search(normalized) for pattern in NOISY_PATH_PATTERNS)


def _paths_from_diff(diff: str) -> list[str]:
    paths: list[str] = []
    for line in str(diff or "").splitlines():
        if not line.startswith("diff --git ") or " b/" not in line:
            continue
        path = _path(line.split(" b/", 1)[1])
        if _visible(path) and path not in paths:
            paths.append(path)
    return paths[:16]


def _repair_paths(repair_packets: list[dict[str, Any]], active_repair_case: dict[str, Any]) -> list[str]:
    paths: list[str] = []

    def add(value: object) -> None:
        path = _path(value)
        if _visible(path) and path.startswith("miniapp/") and path not in paths:
            paths.append(path)

    for source in [active_repair_case, *repair_packets]:
        if not isinstance(source, dict):
            continue
        plan = source.get("focused_patch_plan") if isinstance(source.get("focused_patch_plan"), dict) else {}
        for key in ("allowed_files", "target_files", "likely_files"):
            for item in source.get(key) or plan.get(key) or []:
                add(item)
        prompt = source.get("repair_prompt") if isinstance(source.get("repair_prompt"), dict) else {}
        sections = prompt.get("sections") if isinstance(prompt.get("sections"), dict) else {}
        for key in ("target_files", "allowed_edit_slice", "likely_files"):
            for item in sections.get(key) or []:
                add(item)
    return paths[:12]


def _failed_checks(execution: CheckExecutionRecord) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for result in execution.results:
        if str(result.status or "") != "failed":
            continue
        checks.append(
            {
                "name": result.name,
                "status": result.status,
                "details": result.details,
                "command": result.command,
                "exit_code": result.exit_code,
                "logs": list(result.logs or [])[-8:],
                "diagnostics": _compact(result.diagnostics or {}, max_chars=1400, max_items=8),
            }
        )
    return checks[:8]


def _relevant_source(file_contexts: dict[str, str], active_paths: list[str], *, retry: bool) -> dict[str, str]:
    if not file_contexts:
        return {}
    selected: dict[str, str] = {}
    active = [_path(path) for path in active_paths if _visible(path)]
    candidates = active if retry and active else [_path(path) for path in file_contexts if _visible(path)]
    for path in candidates:
        if path in file_contexts and path not in selected:
            text = str(file_contexts[path] or "")
            cap = 2200 if retry else 3000
            selected[path] = _compact(text, max_chars=cap)
        if len(selected) >= (6 if retry else 10):
            break
    return selected


class AgentContextPacker:
    @staticmethod
    def pack(
        *,
        user_prompt: str,
        acceptance_contract: dict[str, Any],
        implementation_plan: dict[str, Any],
        latest_execution: CheckExecutionRecord,
        file_contexts: dict[str, str],
        latest_diff_summary: str | None,
        repair_packets: list[dict[str, Any]],
        active_repair_case: dict[str, Any],
        diagnostics_delta: dict[str, Any] | None,
        repeated_no_progress: int = 0,
        context_mode: str = "minimal",
    ) -> dict[str, Any]:
        retry = bool(repair_packets or active_repair_case or repeated_no_progress > 0)
        diff_paths = _paths_from_diff(str(latest_diff_summary or ""))
        repair_paths = _repair_paths(repair_packets, active_repair_case)
        active_paths = list(dict.fromkeys([*repair_paths, *diff_paths]))
        product_pack = {
            "schema": "grounded.product_context_pack.v1",
            "prompt_excerpt": _compact(user_prompt, max_chars=900),
            "roles": list(acceptance_contract.get("roles") or []),
            "flows": _compact(acceptance_contract.get("flows") or [], max_chars=900, max_items=5),
            "features": _compact(acceptance_contract.get("features") or {}, max_chars=900, max_items=6),
            "product_task_ledger": _compact(implementation_plan.get("product_task_ledger") or [], max_chars=1300, max_items=6),
            "routeable_screen_plan": _compact(implementation_plan.get("routeable_screen_plan") or {}, max_chars=900, max_items=6),
        }
        error_pack = {
            "schema": "grounded.error_context_pack.v1",
            "retry": retry,
            "context_mode": context_mode,
            "active_paths": active_paths[:12],
            "failed_checks": _failed_checks(latest_execution),
            "repair_class": active_repair_case.get("repair_class") or "",
            "focused_patch_plan": active_repair_case.get("focused_patch_plan") or {},
            "relevant_checks": active_repair_case.get("relevant_checks") or [],
            "diagnostics_delta": _compact(diagnostics_delta or {}, max_chars=1200, max_items=8),
            "diff_summary": _compact(latest_diff_summary or "", max_chars=1400 if retry else 2600),
        }
        template_pack = {
            "schema": "grounded.template_invariants_pack.v1",
            "invariants": list(TEMPLATE_INVARIANTS),
            "excluded_noise": [pattern.pattern for pattern in NOISY_PATH_PATTERNS],
        }
        retry_pack = {
            "schema": "grounded.retry_context_policy.v1",
            "active": retry,
            "mode": "focused_retry" if retry else "standard",
            "include_only": (
                ["product_contract", "latest_diff", "failure_diagnostics", "relevant_source", "repair_recipe"]
                if retry
                else ["product_contract", "selected_source", "template_invariants"]
            ),
            "exclude": ["broad_file_tree", "old tool stdout", "unrelated source files", "generated metadata noise", "binary assets"],
            "relevant_source": _relevant_source(file_contexts, active_paths, retry=retry),
            "escalation": active_repair_case.get("escalation") or {},
        }
        total_chars = sum(
            len(json.dumps(value, ensure_ascii=False, default=str))
            for value in (product_pack, error_pack, template_pack, retry_pack)
        )
        return {
            "schema": CONTEXT_PACK_SCHEMA,
            "mode": "retry" if retry else "standard",
            "budget": {
                "max_total_chars": 14_000 if retry else 22_000,
                "estimated_chars": total_chars,
                "status": "within_budget" if total_chars <= (14_000 if retry else 22_000) else "over_budget",
            },
            "product": product_pack,
            "current_error": error_pack,
            "template_invariants": template_pack,
            "retry_policy": retry_pack,
        }
