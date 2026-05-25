from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.models.common import StrictModel


PromptSuggestionPriority = Literal["must", "should", "could"]


class PromptSuggestion(StrictModel):
    suggestion_id: str
    title: str
    prompt: str
    category: str
    priority: PromptSuggestionPriority = "should"
    reason: str = ""
    target_role: Literal["client", "specialist", "manager"] | None = None
    target_files: list[str] = Field(default_factory=list)
    source_signals: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)
    created_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class PromptSuggestionsReport(StrictModel):
    schema_: str = Field(default="grounded.prompt_suggestions.v1", alias="schema")
    run_id: str
    workspace_id: str
    status: str = "empty"
    run_status: str = ""
    items: list[PromptSuggestion] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    created_at: str
