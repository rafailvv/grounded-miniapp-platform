from __future__ import annotations

from typing import Any, Callable

from app.models.domain import DraftFileOperation, utc_now
from app.services.miniapp_generation.artifact_js_tests import ArtifactJsTestsMixin
from app.services.miniapp_generation.artifact_manifests import ArtifactManifestsMixin
from app.services.miniapp_generation.artifact_python_tests import ArtifactPythonTestsMixin
from app.services.miniapp_generation.artifact_reports import ArtifactReportsMixin


class MiniappArtifactBuilder(
    ArtifactManifestsMixin,
    ArtifactPythonTestsMixin,
    ArtifactJsTestsMixin,
    ArtifactReportsMixin,
):
    def __init__(
        self,
        *,
        normalize_role_route_path: Callable[[str, str], str],
        absolute_role_route_path: Callable[[str, str], str],
        default_page_asset_path: Callable[[str, str], str],
        normalize_runtime_python_path: Callable[[str], str],
    ) -> None:
        self._normalize_role_route_path = normalize_role_route_path
        self._absolute_role_route_path = absolute_role_route_path
        self._default_page_asset_path = default_page_asset_path
        self._normalize_runtime_python_path = normalize_runtime_python_path

    def ensure_app_level_test_operations(
        self,
        *,
        page_graph: dict[str, Any],
        role_scope: list[str],
        entity_contract: dict[str, Any] | None,
        operations: list[DraftFileOperation],
    ) -> list[DraftFileOperation]:
        required_tests = {
            "miniapp/tests/test_generated_app.py": self.python_app_level_test_content(page_graph=page_graph, role_scope=role_scope, entity_contract=entity_contract),
            "miniapp/tests/generated_app.test.mjs": self.js_app_level_test_content(page_graph=page_graph, role_scope=role_scope),
        }
        ensured_operations = [operation for operation in operations if operation.file_path not in required_tests]
        for file_path, content in required_tests.items():
            ensured_operations.append(
                DraftFileOperation(
                    file_path=file_path,
                    operation="replace",
                    content=content,
                    reason="Provide deterministic generated app-level tests for the synthesized miniapp workspace.",
                )
            )
        return ensured_operations

    @staticmethod
    def build_traceability_entry(*, workspace_id: str, role_scope: list[str], assistant_message: str) -> dict[str, Any]:
        return {
            "entry_id": f"trace_{utc_now().strftime('%Y%m%d%H%M%S')}",
            "workspace_id": workspace_id,
            "role_scope": list(role_scope),
            "assistant_message": assistant_message,
        }
