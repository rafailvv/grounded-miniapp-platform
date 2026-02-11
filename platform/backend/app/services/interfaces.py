from __future__ import annotations

from typing import Protocol

from app.models.app_ir import AppIRModel
from app.models.domain import DocumentRecord
from app.models.grounded_spec import GroundedSpecModel


class DocumentIndexer(Protocol):
    def index(self, document: DocumentRecord) -> DocumentRecord:
        ...


class Retriever(Protocol):
    def retrieve(
        self,
        *,
        workspace_id: str,
        prompt: str,
        target_platform: str,
        limit: int = 8,
    ) -> list[dict]:
        ...


class SpecBuilder(Protocol):
    def build_grounded_spec(self, *, workspace_id: str, prompt: str, target_platform: str, preview_profile: str) -> GroundedSpecModel:
        ...


class ScenarioGraphBuilder(Protocol):
    def build_scenario_graph(self, spec: GroundedSpecModel) -> dict:
        ...


class IRBuilder(Protocol):
    def build_app_ir(self, spec: GroundedSpecModel, scenario_graph: dict) -> AppIRModel:
        ...


class ValidatorSuite(Protocol):
    def validate_grounded_spec(self, spec: GroundedSpecModel):
        ...

    def validate_app_ir(self, ir: AppIRModel):
        ...


class ArtifactPlanner(Protocol):
    def build_artifact_plan(self, spec: GroundedSpecModel, ir: AppIRModel):
        ...


class PatchApplier(Protocol):
    def apply(self, *, workspace_id: str, operations: list) -> str:
        ...


class PreviewManager(Protocol):
    def start(self, workspace_id: str) -> dict:
        ...


class PlatformAdapter(Protocol):
    platform_name: str

    def build_platform_constraints(self) -> list:
        ...

    def build_security_requirements(self) -> list:
        ...

