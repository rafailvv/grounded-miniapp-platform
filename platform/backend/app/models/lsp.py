from __future__ import annotations

from typing import Any

from pydantic import Field

from app.models.common import StrictModel


class LspServerState(StrictModel):
    schema_: str = Field(default="grounded.lsp_server_state.v1", alias="schema")
    workspace_id: str | None = None
    run_id: str | None = None
    language: str
    status: str = "unavailable"
    command: list[str] = Field(default_factory=list)
    root_uri: str | None = None
    pid: int | None = None
    initialized: bool = False
    fallback_used: bool = True
    message: str = ""
    started_at: str | None = None
    updated_at: str | None = None


class LspDiagnosticReportV2(StrictModel):
    schema_: str = Field(default="grounded.lsp_diagnostics.v2", alias="schema")
    workspace_id: str
    run_id: str | None = None
    status: str = "passed"
    engine: str = "static"
    server_status: dict[str, Any] = Field(default_factory=dict)
    fallback_used: bool = True
    diagnostics_ref: str | None = None
    route_graph_ref: str | None = None
    symbol_index_ref: str | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    jumps: list[dict[str, Any]] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    changed_only: bool = False
    changed_files: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)
    next_sequence: int = 0


class LspSymbolIndex(StrictModel):
    schema_: str = Field(default="grounded.lsp_symbol_index.v1", alias="schema")
    workspace_id: str
    run_id: str | None = None
    status: str = "ready"
    engine: str = "static"
    server_status: dict[str, Any] = Field(default_factory=dict)
    fallback_used: bool = True
    symbol_index_ref: str | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    jumps: list[dict[str, Any]] = Field(default_factory=list)
    next_sequence: int = 0


class LspReferenceReport(StrictModel):
    schema_: str = Field(default="grounded.lsp_references.v1", alias="schema")
    workspace_id: str
    run_id: str | None = None
    symbol: str = ""
    status: str = "ready"
    engine: str = "static"
    server_status: dict[str, Any] = Field(default_factory=dict)
    fallback_used: bool = True
    references_ref: str | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    jumps: list[dict[str, Any]] = Field(default_factory=list)
    next_sequence: int = 0


class LspRouteGraphReport(StrictModel):
    schema_: str = Field(default="grounded.lsp_route_graph.v1", alias="schema")
    workspace_id: str
    run_id: str | None = None
    status: str = "ready"
    engine: str = "static"
    server_status: dict[str, Any] = Field(default_factory=dict)
    fallback_used: bool = True
    route_graph_ref: str | None = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    missing_edges: list[dict[str, Any]] = Field(default_factory=list)
    api_mismatches: list[dict[str, Any]] = Field(default_factory=list)
    jumps: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    next_sequence: int = 0


class LspContextReport(StrictModel):
    schema_: str = Field(default="grounded.lsp_context.v1", alias="schema")
    workspace_id: str
    run_id: str | None = None
    status: str = "ready"
    engine: str = "static"
    server_status: dict[str, Any] = Field(default_factory=dict)
    fallback_used: bool = True
    lsp_context_ref: str
    diagnostics_ref: str | None = None
    symbol_index_ref: str | None = None
    route_graph_ref: str | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    symbols: dict[str, Any] = Field(default_factory=dict)
    route_graph: dict[str, Any] = Field(default_factory=dict)
    items: list[dict[str, Any]] = Field(default_factory=list)
    jumps: list[dict[str, Any]] = Field(default_factory=list)
    next_sequence: int = 0
