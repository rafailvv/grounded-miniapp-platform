from __future__ import annotations

from app.models.grounded_spec import GroundedSpecModel
from app.services.miniapp_generation.constants import SHARED_GENERATED_FILES

from app.modules.miniapp_generation_runtime.generation_scaffold import (
    MINIMAL_BOOTSTRAP_TARGETS,
    thin_role_pages_for_role,
)
from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationCodePlanDefaults(MiniappGenerationRuntimeOwner):
    def _deterministic_code_plan_payload(
        self,
        *,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        role_scope: list[str],
        scope_mode: str,
        require_multi_page: bool,
    ) -> dict[str, object]:
        roles_payload: list[dict[str, object]] = []
        for role in role_scope:
            pages = self._deterministic_role_pages(role, prompt=prompt, grounded_spec=grounded_spec, require_multi_page=require_multi_page)
            roles_payload.append(
                {
                    "role": role,
                    "entry_path": "/",
                    "landing_page_id": pages[0]["page_id"],
                    "routes_file": self._default_routes_file(role),
                    "pages": pages,
                }
            )
        backend_targets = list(MINIMAL_BOOTSTRAP_TARGETS)
        backend_targets.extend(f"miniapp/app/routes/{role}.py" for role in role_scope)
        backend_targets = list(dict.fromkeys(backend_targets))
        flow_mode = "multi_page" if any(len((role_payload.get("pages") or [])) > 2 for role_payload in roles_payload) else "single_page"
        payload = {
            "summary": grounded_spec.product_goal or prompt[:160],
            "flow_mode": flow_mode,
            "files_to_read": [],
            "target_files": [],
            "shared_files": list(SHARED_GENERATED_FILES),
            "backend_targets": backend_targets,
            "page_graph": {
                "app_title": (grounded_spec.product_goal or "Generated mini-app")[:80],
                "summary": grounded_spec.product_goal or prompt[:160],
                "flow_mode": flow_mode,
                "shared_files": list(SHARED_GENERATED_FILES),
                "backend_targets": backend_targets,
                "roles": roles_payload,
            },
        }
        return {"model": "deterministic-planner", "payload": payload}

    def _deterministic_role_pages(
        self,
        role: str,
        *,
        prompt: str | None = None,
        grounded_spec: GroundedSpecModel | None = None,
        require_multi_page: bool,
    ) -> list[dict[str, object]]:
        if grounded_spec is None:
            return []
        return thin_role_pages_for_role(
            role=role,
            prompt=prompt or grounded_spec.product_goal,
            grounded_spec=grounded_spec,
            default_page_file=self.service._default_page_file,
            default_page_asset_path=self.service._default_page_asset_path,
            default_handoff_paths_for_page_kind=self.service._default_handoff_paths_for_page_kind,
        )
