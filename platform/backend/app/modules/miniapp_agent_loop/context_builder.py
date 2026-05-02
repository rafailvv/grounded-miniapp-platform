from __future__ import annotations

from app.models.domain import CheckExecutionRecord, RunIterationRecord
from app.repositories.state_store import StateStore
from app.services.workspace.service import WorkspaceService


class AgentContextBuilder:
    def __init__(self, *, store: StateStore, workspace_service: WorkspaceService) -> None:
        self.store = store
        self.workspace_service = workspace_service

    def store_agent_reports(
        self,
        *,
        callbacks,
        workspace_id: str,
        run_id: str,
        iterations: list[RunIterationRecord],
        latest_execution: CheckExecutionRecord,
    ) -> None:
        callbacks.store_report(
            f"iterations:{workspace_id}",
            {"run_id": run_id, "items": [item.model_dump(mode="json") for item in iterations]},
        )
        callbacks.store_report(
            f"check_results:{workspace_id}",
            {
                "run_id": run_id,
                "items": [item.model_dump(mode="json") for item in latest_execution.results],
                "execution": latest_execution.model_dump(mode="json"),
            },
        )
        callbacks.store_report(
            f"candidate_diff:{workspace_id}",
            {
                "run_id": run_id,
                "diff": self.workspace_service.diff(workspace_id, run_id=run_id),
            },
        )

    def current_diff_summary(self, workspace_id: str, run_id: str) -> str | None:
        diff_text = self.workspace_service.diff(workspace_id, run_id=run_id)
        if not diff_text.strip():
            return None
        paths: list[str] = []
        for line in diff_text.splitlines():
            if not line.startswith("diff --git "):
                continue
            if " b/" not in line:
                continue
            paths.append(line.split(" b/", 1)[1].strip())
        if not paths:
            return "Draft diff exists."
        unique_paths = list(dict.fromkeys(paths))
        return f"Changed files: {', '.join(unique_paths[:6])}"

