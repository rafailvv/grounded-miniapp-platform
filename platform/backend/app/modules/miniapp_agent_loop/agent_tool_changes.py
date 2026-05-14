from __future__ import annotations

from typing import Any, Callable

from app.models.domain import DraftAction


MUTATING_AGENT_TOOLS = {"apply_patch_to_draft", "write_file", "edit_file_exact"}


def is_mutating_agent_tool_call(request_item: dict[str, Any]) -> bool:
    tool = str(request_item.get("tool") or "").strip().lower()
    canonical = str(request_item.get("canonical_tool") or "").strip().lower()
    return tool in MUTATING_AGENT_TOOLS or canonical in {"patch.apply", "file.write", "file.edit"}


def file_changes_from_mutating_tool_calls(
    tool_calls: list[dict[str, Any]],
    *,
    default_worker_id: str | None = None,
    default_owner_scope: str | None = None,
    read_text_file: Callable[[str], str | None] | None = None,
    file_freshness: Callable[[str], dict[str, object]] | None = None,
    find_similar_path: Callable[[str], str | None] | None = None,
) -> tuple[list[DraftAction], list[dict[str, object]]]:
    from app.modules.miniapp_agent_loop.tool_router import ToolRouter

    return ToolRouter.deferred_mutations_from_calls(
        tool_calls,
        default_worker_id=default_worker_id,
        default_owner_scope=default_owner_scope,
        read_text_file=read_text_file,
        file_freshness=file_freshness,
        find_similar_path=find_similar_path,
    )
