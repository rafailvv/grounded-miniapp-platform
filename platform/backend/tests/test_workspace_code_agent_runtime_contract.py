from __future__ import annotations

from app.models.common import GenerationMode
from app.modules.workspace_code_agent_runtime.runtime import WorkspaceCodeAgentRuntime
from app.services.workspace.service import WorkspaceService
from app.services.workspace.run_service import RunService


def test_agent_prompt_declares_run_checks_read_only() -> None:
    prompt = WorkspaceCodeAgentRuntime._agent_system_prompt()
    schema = WorkspaceCodeAgentRuntime._agent_turn_schema()

    assert "run_checks is a read-only platform validation snapshot" in prompt
    assert "Tools are diagnostic only" in prompt
    assert "All code changes must be returned in the operations array" in prompt
    assert schema["properties"]["operations"]["maxItems"] == 8


def test_self_blocked_tool_contract_response_is_retryable() -> None:
    payload = {
        "outcome": "fatal_invalid_response",
        "assistant_message": "I could not update the workspace because run_checks never returned.",
        "diagnosis": "The tool could not run a Python script to rewrite files, so no file changes were applied.",
        "operations": [],
    }

    assert WorkspaceCodeAgentRuntime._is_self_blocked_tool_contract_response(payload)

    correction = WorkspaceCodeAgentRuntime._tool_contract_correction_result(payload)
    assert correction["tool"] == "tool_contract_correction"
    assert "cannot write files" in str(correction["contract"])
    assert "operations" in str(correction["required_next_action"])


def test_no_more_tool_rounds_fatal_response_is_retryable() -> None:
    payload = {
        "outcome": "fatal_invalid_response",
        "assistant_message": "Request error: No more tool rounds allowed.",
        "diagnosis": "",
        "operations": [],
    }

    assert WorkspaceCodeAgentRuntime._is_self_blocked_tool_contract_response(payload)


def test_unrecognized_tool_call_fatal_response_is_retryable() -> None:
    payload = {
        "outcome": "fatal_invalid_response",
        "assistant_message": "Tool call was not provided in recognized format.",
        "diagnosis": "",
        "operations": [],
    }

    assert WorkspaceCodeAgentRuntime._is_self_blocked_tool_contract_response(payload)


def test_empty_fatal_agent_response_is_retryable() -> None:
    payload = {
        "outcome": "fatal_invalid_response",
        "assistant_message": "I’m sorry, but I can’t help with that.",
        "diagnosis": "",
        "tool_requests": [],
        "operations": [],
    }

    assert WorkspaceCodeAgentRuntime._is_empty_fatal_agent_response(payload)
    correction = WorkspaceCodeAgentRuntime._empty_fatal_correction_result(payload)
    assert "ordinary workspace code generation" in str(correction["contract"])


def test_empty_fatal_not_able_to_generate_response_is_retryable() -> None:
    payload = {
        "outcome": "fatal_invalid_response",
        "assistant_message": "I’m sorry, but I’m not able to help with that.",
        "diagnosis": "I cannot generate the required response in this context.",
        "tool_requests": [],
        "operations": [],
    }

    assert WorkspaceCodeAgentRuntime._is_empty_fatal_agent_response(payload)


def test_output_cap_correction_for_create_requires_compact_patch() -> None:
    from app.models.domain import CreateRunRequest

    request = CreateRunRequest(prompt="Создай приложение", mode="generate", intent="create")

    correction = WorkspaceCodeAgentRuntime._output_cap_correction_result(
        {"error": "max_output_tokens reached"},
        request=request,
    )

    assert correction["tool"] == "output_cap_correction"
    assert "no more than 6 file operations" in str(correction["required_next_action"])
    assert "do not request more context" in str(correction["required_next_action"])


def test_output_cap_correction_for_edit_prefers_focused_replace() -> None:
    from app.models.domain import CreateRunRequest

    request = CreateRunRequest(prompt="Добавь блок Сегодня", mode="generate", intent="edit")

    correction = WorkspaceCodeAgentRuntime._output_cap_correction_result(
        {"error": "max_output_tokens reached"},
        request=request,
    )

    assert "1-2 focused operations" in str(correction["required_next_action"])
    assert "full-file replace" in str(correction["required_next_action"])


def test_agent_turn_tuning_caps_all_generation_modes() -> None:
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.FAST, intent="edit") == {
        "reasoning": {"effort": "low"},
        "max_output_tokens": 16000,
    }
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.FAST) == {
        "reasoning": {"effort": "low"},
        "max_output_tokens": 32000,
    }
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.BALANCED) == {
        "reasoning": {"effort": "medium"},
        "max_output_tokens": 45000,
    }
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.QUALITY) == {
        "reasoning": {"effort": "high"},
        "max_output_tokens": 60000,
    }


def test_prompt_alignment_domain_detection_does_not_treat_booking_catalog_as_commerce() -> None:
    prompt = "создай приложение для бронирования тренировок: каталог направлений, расписание, тренеры и запись на слот"

    assert not WorkspaceCodeAgentRuntime._is_commerce_prompt(prompt)
    assert WorkspaceCodeAgentRuntime._is_booking_prompt(prompt)


def test_workspace_name_ignores_leading_timestamp() -> None:
    prompt = "1:49 AM\n\n\nСоздай интернет-магазин с каталогом товаров и корзиной"

    assert RunService._derive_workspace_name_from_prompt(prompt) == "Интернет Магазин Каталогом Товаров Корзиной"


def test_patch_hunk_without_file_headers_is_normalized_to_target_path() -> None:
    diff = "@@ -1,2 +1,2 @@\n-old\n+new\n"

    normalized = WorkspaceService._ensure_unified_diff_paths(diff, "miniapp/app/db.py")

    assert normalized.startswith("--- a/miniapp/app/db.py\n+++ b/miniapp/app/db.py\n@@")
    assert WorkspaceService._paths_from_unified_diff(normalized) == ["miniapp/app/db.py"]


def test_line_free_hunk_patch_is_preserved_and_applied_without_git_ranges() -> None:
    existing = 'const role = "manager";\nwindow.setupPreviewBridge?.(role);\n'
    diff = (
        "@@\n"
        '-const role = "manager";\n'
        "-window.setupPreviewBridge?.(role);\n"
        '+const role = "manager";\n'
        "+window.setupPreviewBridge?.(role);\n"
        '+const API_ROOT = "/api/manager";\n'
    )

    assert WorkspaceService._ensure_unified_diff_paths(diff, "miniapp/app/static/manager/app.js") == diff

    updated = WorkspaceService._apply_line_free_hunks(existing, diff)

    assert updated == (
        'const role = "manager";\n'
        "window.setupPreviewBridge?.(role);\n"
        'const API_ROOT = "/api/manager";\n'
    )


def test_line_free_addition_hunk_can_create_new_file_content() -> None:
    diff = "@@\n+from app.routes.studio import router as studio_router\n"

    assert WorkspaceService._apply_line_free_hunks("", diff) == "from app.routes.studio import router as studio_router\n"


def test_line_free_addition_hunk_uses_nearby_anchor_when_context_drifted() -> None:
    existing = (
        '@router.get("/api/client/bookings", response_model=list[BookingOut])\n'
        "def client_bookings(db: Session = Depends(get_db)) -> list[BookingOut]:\n"
        "    return []\n"
        "\n"
        "\n"
        '@router.post("/api/client/book", response_model=BookingOut)\n'
        "def create_booking(request: BookingRequest, db: Session = Depends(get_db)) -> BookingOut:\n"
        "    return BookingOut()\n"
    )
    diff = (
        "@@\n"
        ' @router.get("/api/client/bookings", response_model=list[BookingOut])\n'
        "def list_client_bookings(db: Session = Depends(get_db)) -> list[BookingOut]:\n"
        "     return []\n"
        " \n"
        " \n"
        '+@router.delete("/api/client/bookings/{booking_id}", status_code=204)\n'
        "+def delete_client_booking(booking_id: int, db: Session = Depends(get_db)) -> None:\n"
        "+    booking = db.get(Booking, booking_id)\n"
        "+    if booking is None:\n"
        '+        raise HTTPException(status_code=404, detail="Бронирование не найдено")\n'
        "+    db.delete(booking)\n"
        "+    db.commit()\n"
        "+\n"
        ' @router.post("/api/client/book", response_model=BookingOut)\n'
        "def create_booking(request: BookingRequest, db: Session = Depends(get_db)) -> BookingOut:\n"
        "     return BookingOut()\n"
    )

    updated = WorkspaceService._apply_line_free_hunks(existing, diff)

    assert updated is not None
    assert '@router.delete("/api/client/bookings/{booking_id}", status_code=204)' in updated
    assert updated.index('@router.delete("/api/client/bookings/{booking_id}"') < updated.index('@router.post("/api/client/book"')


def test_line_free_route_addition_does_not_insert_before_class_field_anchor() -> None:
    existing = (
        "class BookingOut(StrictModel):\n"
        "    id: int\n"
        "    client_name: str\n"
        "    slot_id: int\n"
        "    slot_label: str\n"
    )
    diff = (
        "@@\n"
        "     id: int\n"
        "     client_name: str\n"
        "     slot_id: int\n"
        '+@router.delete("/api/client/bookings/{booking_id}")\n'
        "+def cancel_booking(booking_id: int) -> None:\n"
        "+    return None\n"
        "     slot_label: str\n"
    )

    assert WorkspaceService._apply_line_free_hunks(existing, diff) is None


def test_unified_diff_detection_rejects_plain_file_content() -> None:
    assert WorkspaceCodeAgentRuntime._looks_like_unified_diff("@@ -1 +1 @@\n-old\n+new\n")
    assert not WorkspaceCodeAgentRuntime._looks_like_unified_diff("from __future__ import annotations\n\nprint('full file')\n")


def test_patch_with_separate_full_content_is_coerced_to_replace() -> None:
    runtime = object.__new__(WorkspaceCodeAgentRuntime)
    operations = runtime._coerce_operations(
        [
            {
                "file_path": "miniapp/app/static/client/app.js",
                "operation": "patch",
                "content": 'const role = "client";\nwindow.setupPreviewBridge?.(role);\n',
                "diff": "*** Begin Patch\n*** Update File: miniapp/app/static/client/app.js\n@@\n-old\n+new\n*** End Patch\n",
                "reason": "Use full content fallback when a separate patch is also provided.",
            }
        ]
    )

    assert operations[0].operation == "replace"
    assert operations[0].content == 'const role = "client";\nwindow.setupPreviewBridge?.(role);\n'


def test_codex_update_patch_can_be_applied_to_expected_file() -> None:
    existing = "from sqlalchemy import create_engine\nfrom sqlalchemy.orm import DeclarativeBase, sessionmaker\n"
    patch = (
        "*** Begin Patch\n"
        "*** Update File: miniapp/app/db.py\n"
        "@@\n"
        "-from sqlalchemy import create_engine\n"
        "-from sqlalchemy.orm import DeclarativeBase, sessionmaker\n"
        "+from sqlalchemy import Column, create_engine\n"
        "+from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker\n"
        "*** End Patch\n"
    )

    updated = WorkspaceService._apply_codex_update_patch(existing, patch, expected_path="miniapp/app/db.py")

    assert updated == "from sqlalchemy import Column, create_engine\nfrom sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker\n"
    assert WorkspaceService._ensure_unified_diff_paths(patch, "miniapp/app/db.py") == patch
