from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any


_READ_TOOLS = {"read_files", "list_files", "file.read", "file.list"}
_MUTATING_TOOLS = {"write_file", "apply_patch_to_draft", "edit_file_exact", "file.write", "patch.apply"}


def summarize_tool_batch(
    *,
    batch_id: str,
    requests: list[dict[str, Any]],
    results: list[dict[str, Any]],
    duration_ms: int,
    status: str,
    worker: str | None = None,
    owner_scope: str | None = None,
) -> dict[str, object]:
    tools = [str(item.get("tool") or "").strip() for item in requests if str(item.get("tool") or "").strip()]
    failed = [item for item in results if str(item.get("status") or "").lower() in {"error", "failed"}]
    resolved_status = "failed" if failed and status == "completed" else status
    label = _tool_batch_label(requests=requests, results=results, tools=tools, status=resolved_status)
    return {
        "batch_id": batch_id,
        "label": label,
        "summary": label,
        "tools": tools,
        "tool_count": len(tools),
        "result_count": len(results),
        "failed_count": len(failed),
        "status": resolved_status,
        "duration_ms": duration_ms,
        "worker": worker,
        "owner_scope": owner_scope,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _tool_batch_label(*, requests: list[dict[str, Any]], results: list[dict[str, Any]], tools: list[str], status: str) -> str:
    tool_set = set(tools)
    semantic_statuses = {str(item.get("semantic_status") or "").lower() for item in results if item.get("semantic_status")}
    failed_checks = sum(len(item.get("failed_checks") or []) for item in results if isinstance(item.get("failed_checks"), list))
    failed = (
        status in {"failed", "error", "blocked"}
        or failed_checks > 0
        or bool(semantic_statuses & {"failed", "error", "blocked", "blocked_by_policy", "blocked_by_sandbox"})
        or any(str(item.get("status") or "").lower() in {"error", "failed", "blocked"} for item in results)
    )
    if failed:
        if "browser_verify" in tool_set:
            return "Failed browser smoke"
        if "run_checks" in tool_set or failed_checks:
            return "Checks need repair"
        if tool_set & _MUTATING_TOOLS:
            return "Blocked file update"
        return "Tool action needs repair"

    if tool_set and tool_set <= _READ_TOOLS:
        return "Read app context"
    if "search_files" in tool_set:
        return "Searched routes" if _targets_match(requests, "route") else "Searched workspace"
    if "semantic_scan" in tool_set:
        return "Scanned source semantics"
    if "run_checks" in tool_set:
        return "Ran build" if _targets_match(requests, "build") else "Ran tests"
    if "browser_verify" in tool_set:
        return "Checked mobile layout" if _targets_match(requests, "mobile") else "Ran browser smoke"
    if "run_command" in tool_set:
        return _command_label(requests)
    if tool_set & _MUTATING_TOOLS:
        return f"{_mutation_operation(requests=requests, results=results)} {_target_object(requests=requests, results=results)}"
    return "Ran tool batch"


def _command_label(requests: list[dict[str, Any]]) -> str:
    command = " ".join(str(item.get("command") or "") for item in requests).lower()
    if any(token in command for token in ("pytest", "vitest", "npm test", "pnpm test", "yarn test")):
        return "Ran tests"
    if any(token in command for token in ("npm run build", "pnpm build", "yarn build", "tsc", "vite build")):
        return "Ran build"
    if any(token in command for token in ("playwright", "browser", "smoke")):
        return "Ran browser smoke"
    return "Ran diagnostic command"


def _mutation_operation(*, requests: list[dict[str, Any]], results: list[dict[str, Any]]) -> str:
    operations = {str(item.get("operation") or "").lower() for item in _changed_items(requests=requests, results=results)}
    tools = {str(item.get("tool") or "").strip() for item in requests}
    if "create" in operations:
        return "Created"
    if "patch" in operations or "apply_patch_to_draft" in tools:
        return "Patched"
    if "delete" in operations:
        return "Removed"
    return "Updated"


def _target_object(*, requests: list[dict[str, Any]], results: list[dict[str, Any]]) -> str:
    paths = _target_paths(requests=requests, results=results)
    lowered = " ".join(paths).lower()
    name = _domain_name(paths)
    if "mobile" in lowered or "overflow" in lowered:
        return "mobile overflow"
    if "api" in lowered or "/routes/" in lowered or "/routers/" in lowered or "endpoint" in lowered:
        if name.endswith(" api"):
            name = name[: -len(" api")]
        return f"{name} API" if name else "API"
    if "auth" in lowered or "login" in lowered or "sign" in lowered:
        return "auth screen"
    if "client" in lowered or "app.js" in lowered or ".tsx" in lowered or ".jsx" in lowered:
        return f"{name} view" if name else "client view"
    if name:
        return name
    return "file update"


def _domain_name(paths: list[str]) -> str:
    ignored = {
        "api",
        "app",
        "client",
        "component",
        "components",
        "index",
        "main",
        "page",
        "pages",
        "route",
        "routes",
        "server",
        "static",
        "style",
        "styles",
        "test",
        "tests",
        "view",
    }
    for raw_path in paths:
        path = PurePosixPath(raw_path)
        candidates = [path.stem, *reversed(path.parts[:-1])]
        for candidate in candidates:
            normalized = candidate.replace("_", "-").replace(".", "-").strip("-").lower()
            if not normalized or normalized in ignored or normalized.startswith("["):
                continue
            return " ".join(part for part in normalized.split("-") if part)
    return ""


def _targets_match(requests: list[dict[str, Any]], needle: str) -> bool:
    return needle.lower() in " ".join(_target_paths(requests=requests, results=[])).lower()


def _target_paths(*, requests: list[dict[str, Any]], results: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    for item in [*requests, *results, *_changed_items(requests=requests, results=results)]:
        if not isinstance(item, dict):
            continue
        for key in ("file_path", "path", "target"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                paths.append(value.strip())
        for key in ("targets", "files", "changed_files"):
            value = item.get(key)
            if isinstance(value, list):
                paths.extend(str(entry).strip() for entry in value if str(entry).strip())
    return paths


def _changed_items(*, requests: list[dict[str, Any]], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for result in results:
        for key in ("deferred_changes", "file_changes", "changed_files"):
            value = result.get(key)
            if isinstance(value, list):
                for entry in value:
                    if isinstance(entry, dict):
                        items.append(entry)
                    elif isinstance(entry, str):
                        items.append({"file_path": entry})
    for request in requests:
        if request.get("file_path"):
            items.append({"file_path": request.get("file_path"), "operation": request.get("operation")})
    return items
