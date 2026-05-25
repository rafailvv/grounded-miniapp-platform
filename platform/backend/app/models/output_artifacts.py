from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.models.common import StrictModel


OutputStream = Literal["stdout", "stderr", "combined", "tool"]


class OutputArtifactRef(StrictModel):
    ref: str
    artifact_id: str
    kind: str = "exec_output"
    stream: OutputStream = "stdout"
    sha256: str
    chars: int = 0
    omitted_chars: int = 0
    truncated_full: bool = False


class HeadTailOutput(StrictModel):
    head: str = ""
    tail: str = ""
    excerpt: str = ""
    total_chars: int = 0
    omitted_chars: int = 0
    chunk_count: int = 0
    sha256: str | None = None
    artifact_ref: str | None = None
    truncated_full: bool = False


class CommandOutputArtifact(StrictModel):
    schema_: str = Field(default="grounded.command_output_artifact.v1", alias="schema")
    ref: str
    artifact_id: str
    workspace_id: str
    run_id: str
    process_id: str
    stream: OutputStream
    command: str = ""
    exit_code: int | None = None
    semantic_status: str | None = None
    sha256: str
    chars: int = 0
    truncated_full: bool = False
    head_tail: HeadTailOutput
    content: str = ""
    created_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolOutputArtifact(CommandOutputArtifact):
    schema_: str = Field(default="grounded.tool_output_artifact.v1", alias="schema")
    tool_call_id: str | None = None
    tool: str | None = None


class OutputArtifactIndex(StrictModel):
    schema_: str = Field(default="grounded.output_artifact_index.v1", alias="schema")
    workspace_id: str
    run_id: str
    items: list[OutputArtifactRef] = Field(default_factory=list)
    updated_at: str | None = None
