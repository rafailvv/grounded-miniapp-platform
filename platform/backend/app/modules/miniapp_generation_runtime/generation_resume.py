from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.models.domain import utc_now
from app.models.grounded_spec import GroundedSpecModel

if TYPE_CHECKING:
    from app.services.miniapp_generation.service import GenerationService


class MiniappGenerationResume:
    def __init__(self, service: "GenerationService") -> None:
        self.service = service

    def store_resume_checkpoint(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        request,
        role_scope: list[str],
        role_contract: dict[str, Any],
        plan_result: dict[str, Any],
    ) -> None:
        payload = {
            "workspace_id": workspace_id,
            "source_run_id": request.linked_run_id,
            "draft_run_id": draft_run_id,
            "status": "pending",
            "prompt": request.prompt,
            "intent": request.intent,
            "mode": request.mode,
            "generation_mode": request.generation_mode.value if hasattr(request.generation_mode, "value") else str(request.generation_mode),
            "target_platform": request.target_platform.value if hasattr(request.target_platform, "value") else str(request.target_platform),
            "preview_profile": request.preview_profile.value if hasattr(request.preview_profile, "value") else str(request.preview_profile),
            "target_role_scope": role_scope,
            "model_profile": request.model_profile,
            "page_graph": plan_result.get("page_graph"),
            "role_contract": role_contract,
            "scope_mode": plan_result.get("scope_mode"),
            "write_strategy": plan_result.get("write_strategy") or plan_result.get("scope_mode"),
            "strategy_reason": plan_result.get("strategy_reason"),
            "flow_mode": plan_result.get("flow_mode"),
            "files_to_read": list(plan_result.get("files_to_read") or []),
            "target_files": list(plan_result.get("target_files") or []),
            "shared_files": list(plan_result.get("shared_files") or []),
            "backend_targets": list(plan_result.get("backend_targets") or []),
            "execution_plan": plan_result.get("execution_plan") or {},
            "generation_clusters": plan_result.get("generation_clusters") or [],
            "created_at": utc_now().isoformat(),
        }
        self.service._store_report(f"resume_checkpoint:{workspace_id}", payload)

    def load_resume_checkpoint_bundle(self, workspace_id: str, source_run_id: str | None) -> dict[str, Any] | None:
        source_run = str(source_run_id or "").strip()
        if not source_run:
            return None
        checkpoint = self.service.store.get("reports", f"resume_checkpoint:{workspace_id}")
        if not checkpoint or checkpoint.get("status") != "pending":
            return None
        if str(checkpoint.get("source_run_id") or "") != source_run:
            return None
        spec_payload = self.service.current_report(workspace_id, "spec")
        role_contract_payload = self.service.current_report(workspace_id, "role_contract")
        if not spec_payload or not role_contract_payload:
            return None
        try:
            grounded_spec = GroundedSpecModel.model_validate(spec_payload)
        except Exception:
            return None
        role_contract = role_contract_payload.get("role_contract")
        page_graph = checkpoint.get("page_graph")
        if not isinstance(role_contract, dict) or not isinstance(page_graph, dict):
            return None
        workspace_tree = self.service.workspace_service.file_tree(workspace_id, run_id=source_run)
        valid_tree_paths = {
            str(item.get("path"))
            for item in workspace_tree
            if isinstance(item, dict) and item.get("type") == "file" and isinstance(item.get("path"), str)
        }
        target_files = self.service._normalize_path_list(checkpoint.get("target_files"), [])
        files_to_read = self.service._normalize_path_list(checkpoint.get("files_to_read"), [])
        shared_files = self.service._normalize_path_list(checkpoint.get("shared_files"), [])
        backend_targets = self.service._normalize_path_list(checkpoint.get("backend_targets"), [])
        target_files = [path for path in target_files if path in valid_tree_paths or path.startswith("miniapp/")]
        files_to_read = [path for path in files_to_read if path in valid_tree_paths]
        shared_files = [path for path in shared_files if path in valid_tree_paths]
        backend_targets = self.service._sanitize_backend_targets(
            [path for path in backend_targets if path in valid_tree_paths or path.startswith("miniapp/")]
        )
        plan_result = {
            "page_graph": page_graph,
            "scope_mode": checkpoint.get("scope_mode") or "minimal_patch",
            "write_strategy": checkpoint.get("write_strategy") or checkpoint.get("scope_mode") or "minimal_patch",
            "strategy_reason": checkpoint.get("strategy_reason") or "Resumed from a saved planning checkpoint.",
            "flow_mode": checkpoint.get("flow_mode") or "multi_page",
            "files_to_read": files_to_read,
            "target_files": target_files,
            "shared_files": shared_files,
            "backend_targets": backend_targets,
            "execution_plan": checkpoint.get("execution_plan") or {},
            "generation_clusters": list(checkpoint.get("generation_clusters") or []),
            "require_multi_page": True,
        }
        plan_result["target_files"] = self.service._sanitize_planner_target_files(
            target_files=plan_result["target_files"],
            backend_targets=plan_result["backend_targets"],
            page_graph=page_graph,
        )
        if not plan_result["target_files"]:
            return None
        return {
            "checkpoint": checkpoint,
            "grounded_spec": grounded_spec,
            "role_contract": role_contract,
            "plan_result": plan_result,
            "role_scope": list(checkpoint.get("target_role_scope") or []),
        }
