from __future__ import annotations

from app.models.grounded_spec import GroundedSpecModel
from app.services.miniapp_generation.constants import SHARED_GENERATED_FILES

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
            pages = self._deterministic_role_pages(role, require_multi_page=require_multi_page)
            roles_payload.append(
                {
                    "role": role,
                    "entry_path": "/",
                    "landing_page_id": pages[0]["page_id"],
                    "routes_file": self._default_routes_file(role),
                    "pages": pages,
                }
            )
        backend_targets = [
            "miniapp/app/db.py",
            "miniapp/app/schemas.py",
            "miniapp/app/main.py",
            "miniapp/app/routes/health.py",
            "miniapp/app/routes/profiles.py",
            "miniapp/app/routes/requests.py",
            "miniapp/app/routes/assignments.py",
            "miniapp/app/routes/comments.py",
            "miniapp/app/routes/status.py",
            "miniapp/app/routes/users.py",
            "miniapp/app/routes/workload.py",
            "miniapp/app/routes/time_slots.py",
        ]
        payload = {
            "summary": grounded_spec.product_goal or prompt[:160],
            "flow_mode": "multi_page" if require_multi_page else "single_page",
            "files_to_read": [],
            "target_files": [],
            "shared_files": list(SHARED_GENERATED_FILES),
            "backend_targets": backend_targets,
            "page_graph": {
                "app_title": (grounded_spec.product_goal or "Generated mini-app")[:80],
                "summary": grounded_spec.product_goal or prompt[:160],
                "flow_mode": "multi_page" if require_multi_page else "single_page",
                "shared_files": list(SHARED_GENERATED_FILES),
                "backend_targets": backend_targets,
                "roles": roles_payload,
            },
        }
        return {"model": "deterministic-planner", "payload": payload}

    def _deterministic_role_pages(self, role: str, *, require_multi_page: bool) -> list[dict[str, object]]:
        if not require_multi_page:
            return []
        role_pages: dict[str, list[dict[str, object]]] = {
            "client": [
                {"page_id": "client_home", "route_path": "/", "navigation_label": "Home", "component_name": "ClientHomePage", "file_path": "miniapp/app/static/client/index.html", "title": "Requests", "description": "Track the current status of submitted requests.", "purpose": "Show the client request queue and current statuses.", "page_kind": "dashboard", "primary_actions": ["Open request", "Create request", "Open profile"], "handoff_paths": ["/requests_new", "/requests_detail", "/profile"], "data_dependencies": ["/api/requests", "/api/profiles"], "loading_state": "Render a loading container while client requests are loading.", "empty_state": "Show an empty-state card when the client has no requests yet.", "error_state": "Render an error container when the client request list fails to load."},
                {"page_id": "client_request_create", "route_path": "/requests_new", "navigation_label": "New request", "component_name": "ClientRequestCreatePage", "file_path": "miniapp/app/static/client/requests_new/index.html", "title": "Create request", "description": "Submit a new request with task details and preferred time.", "purpose": "Collect request details, preferred time, and optional notes.", "page_kind": "form", "primary_actions": ["Submit request", "Back to requests", "Open profile"], "handoff_paths": ["/", "/profile"], "data_dependencies": ["/api/requests", "/api/time-slots"], "loading_state": "Render a loading container while available time slots are loading.", "empty_state": "Show an empty-state note when no suggested time slots are available.", "error_state": "Render an error container when time slot data fails to load."},
                {"page_id": "client_request_detail", "route_path": "/requests_detail", "navigation_label": "Request detail", "component_name": "ClientRequestDetailPage", "file_path": "miniapp/app/static/client/requests_detail/index.html", "title": "Request detail", "description": "Review the assigned specialist, comments, and status history.", "purpose": "Show a single request with comments, selected time, and current status.", "page_kind": "workspace", "primary_actions": ["Back to requests", "Open profile"], "handoff_paths": ["/", "/profile"], "data_dependencies": ["/api/requests", "/api/comments"], "loading_state": "Render a loading container while the request detail is loading.", "empty_state": "Show an empty-state note when the request detail cannot be found.", "error_state": "Render an error container when the request detail fails to load."},
                {"page_id": "client_profile_edit", "route_path": "/profile", "navigation_label": "Profile", "component_name": "ClientProfilePage", "file_path": "miniapp/app/static/client/profile/index.html", "title": "Profile", "description": "Manage client profile details and contact info.", "purpose": "Edit client profile details used in requests.", "page_kind": "profile", "primary_actions": ["Save profile", "Back to requests"], "handoff_paths": ["/"], "data_dependencies": ["/api/profiles"], "loading_state": "Render a loading container while the profile is loading.", "empty_state": "Show an empty-state note when profile data is not available yet.", "error_state": "Render an error container when the profile fails to load."},
            ],
            "specialist": [
                {"page_id": "specialist_home", "route_path": "/", "navigation_label": "Assigned work", "component_name": "SpecialistHomePage", "file_path": "miniapp/app/static/specialist/index.html", "title": "Assigned work", "description": "Review current assigned tasks and statuses.", "purpose": "Show the specialist queue with new, active, and completed work.", "page_kind": "dashboard", "primary_actions": ["Open task", "Open profile"], "handoff_paths": ["/requests_detail", "/profile"], "data_dependencies": ["/api/assignments", "/api/requests"], "loading_state": "Render a loading container while assigned work is loading.", "empty_state": "Show an empty-state note when there are no assigned tasks.", "error_state": "Render an error container when assigned work fails to load."},
                {"page_id": "specialist_task_detail", "route_path": "/requests_detail", "navigation_label": "Task detail", "component_name": "SpecialistTaskDetailPage", "file_path": "miniapp/app/static/specialist/requests_detail/index.html", "title": "Task detail", "description": "Update task status, add comments, and review request detail.", "purpose": "Show a single assigned request with status controls and comment history.", "page_kind": "workspace", "primary_actions": ["Update status", "Add comment", "Back to queue", "Open profile"], "handoff_paths": ["/", "/profile"], "data_dependencies": ["/api/requests", "/api/comments", "/api/status"], "loading_state": "Render a loading container while task detail is loading.", "empty_state": "Show an empty-state note when the task detail is unavailable.", "error_state": "Render an error container when task detail fails to load."},
                {"page_id": "specialist_profile_edit", "route_path": "/profile", "navigation_label": "Profile", "component_name": "SpecialistProfilePage", "file_path": "miniapp/app/static/specialist/profile/index.html", "title": "Profile", "description": "Manage specialist profile and availability information.", "purpose": "Edit specialist profile details used for assignments and workload.", "page_kind": "profile", "primary_actions": ["Save profile", "Back to queue"], "handoff_paths": ["/"], "data_dependencies": ["/api/profiles"], "loading_state": "Render a loading container while the profile is loading.", "empty_state": "Show an empty-state note when profile data is unavailable.", "error_state": "Render an error container when the profile fails to load."},
            ],
            "manager": [
                {"page_id": "manager_home", "route_path": "/", "navigation_label": "Overview", "component_name": "ManagerHomePage", "file_path": "miniapp/app/static/manager/index.html", "title": "Overview", "description": "Monitor incoming work, statuses, and workload summary.", "purpose": "Show the manager summary with workload and pending requests.", "page_kind": "dashboard", "primary_actions": ["Open inbox", "Open workload", "Open profile"], "handoff_paths": ["/inbox", "/workload", "/profile"], "data_dependencies": ["/api/requests", "/api/workload"], "loading_state": "Render a loading container while overview metrics are loading.", "empty_state": "Show an empty-state note when no manager data is available.", "error_state": "Render an error container when overview data fails to load."},
                {"page_id": "manager_inbox", "route_path": "/inbox", "navigation_label": "Inbox", "component_name": "ManagerInboxPage", "file_path": "miniapp/app/static/manager/inbox/index.html", "title": "Inbox", "description": "Review incoming requests and decide next actions.", "purpose": "Show the queue of incoming requests awaiting assignment or review.", "page_kind": "list", "primary_actions": ["Open request", "Open workload", "Back to overview"], "handoff_paths": ["/requests_detail", "/workload", "/"], "data_dependencies": ["/api/requests", "/api/status"], "loading_state": "Render a loading container while inbox requests are loading.", "empty_state": "Show an empty-state note when there are no pending requests.", "error_state": "Render an error container when inbox requests fail to load."},
                {"page_id": "manager_request_detail", "route_path": "/requests_detail", "navigation_label": "Request detail", "component_name": "ManagerRequestDetailPage", "file_path": "miniapp/app/static/manager/requests_detail/index.html", "title": "Request detail", "description": "Review the request, comments, status, and assignment state.", "purpose": "Show a single request with its specialist assignment and execution history.", "page_kind": "workspace", "primary_actions": ["Assign specialist", "Update status", "Back to inbox", "Open profile"], "handoff_paths": ["/requests_detail_assign", "/inbox", "/profile"], "data_dependencies": ["/api/requests", "/api/comments", "/api/assignments", "/api/specialists"], "loading_state": "Render a loading container while request detail is loading.", "empty_state": "Show an empty-state note when the request detail is unavailable.", "error_state": "Render an error container when request detail fails to load."},
                {"page_id": "manager_assign_panel", "route_path": "/requests_detail_assign", "navigation_label": "Assign", "component_name": "ManagerAssignPanelPage", "file_path": "miniapp/app/static/manager/requests_detail_assign/index.html", "title": "Assign specialist", "description": "Choose a specialist and confirm the assignment for the current request.", "purpose": "Select a specialist, confirm schedule, and persist the assignment.", "page_kind": "form", "primary_actions": ["Confirm assignment", "Back to request"], "handoff_paths": ["/requests_detail", "/workload"], "data_dependencies": ["/api/assignments", "/api/specialists", "/api/time-slots"], "loading_state": "Render a loading container while assignment options are loading.", "empty_state": "Show an empty-state note when no specialist options are available.", "error_state": "Render an error container when assignment options fail to load."},
                {"page_id": "manager_workload", "route_path": "/workload", "navigation_label": "Workload", "component_name": "ManagerWorkloadPage", "file_path": "miniapp/app/static/manager/workload/index.html", "title": "Workload", "description": "Review team workload and capacity by specialist.", "purpose": "Show workload summaries and capacity for each specialist.", "page_kind": "workspace", "primary_actions": ["Open inbox", "Back to overview", "Open profile"], "handoff_paths": ["/inbox", "/", "/profile"], "data_dependencies": ["/api/workload", "/api/specialists"], "loading_state": "Render a loading container while workload data is loading.", "empty_state": "Show an empty-state note when workload data is unavailable.", "error_state": "Render an error container when workload data fails to load."},
                {"page_id": "manager_profile_edit", "route_path": "/profile", "navigation_label": "Profile", "component_name": "ManagerProfilePage", "file_path": "miniapp/app/static/manager/profile/index.html", "title": "Profile", "description": "Manage manager profile details used in coordination flows.", "purpose": "Edit manager profile settings and contact details.", "page_kind": "profile", "primary_actions": ["Save profile", "Back to overview"], "handoff_paths": ["/"], "data_dependencies": ["/api/profiles"], "loading_state": "Render a loading container while the profile is loading.", "empty_state": "Show an empty-state note when profile data is unavailable.", "error_state": "Render an error container when the profile fails to load."},
            ],
        }
        return role_pages.get(role) or []
