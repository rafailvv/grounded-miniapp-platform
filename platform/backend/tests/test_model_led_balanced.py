from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.ai import model_registry
from app.ai.openrouter_client import OpenRouterClient
from app.ai.model_registry import task_model_overrides
from app.models.common import GenerationMode, PreviewProfile, TargetPlatform
from app.models.domain import DraftFileOperation, JobEvent, JobRecord, RunRecord
from app.models.grounded_spec import (
    APIRequirement,
    Actor,
    Assumption,
    Contradiction,
    DocRef,
    DomainEntity,
    EntityAttribute,
    GroundedSpecModel,
    Metadata,
    PlatformConstraint,
    SecurityRequirement,
    UIRequirement,
    Unknown,
)
from app.modules.miniapp_generation_runtime.grounded_spec_hygiene import GroundedSpecHygieneRuntime
from app.modules.miniapp_generation_runtime.generation_plan_runtime import MiniappGenerationPlanRuntime
from app.modules.miniapp_generation_runtime.generation_entity_contract import MiniappGenerationEntityContract
from app.modules.miniapp_generation_runtime.generation_progress_reporting import GenerationProgressReportingRuntime
from app.modules.miniapp_generation_runtime.generation_page_graph_runtime import MiniappGenerationPageGraphRuntime
from app.modules.miniapp_generation_runtime.generation_codegen import MiniappGenerationCodegen
from app.modules.miniapp_generation_runtime.generation_contract_frontend import MiniappGenerationContractFrontend
from app.modules.miniapp_generation_runtime.generation_contract_schema import MiniappGenerationContractSchema
from app.modules.miniapp_generation_runtime.generation_normal_loop import MiniappGenerationNormalLoop
from app.modules.miniapp_generation_runtime.generation_repair import MiniappGenerationRepair
from app.modules.miniapp_generation_runtime.generation_reporting import MiniappGenerationReporting
from app.modules.miniapp_generation_runtime.grounded_spec_stabilization import GroundedSpecStabilizationRuntime
from app.modules.miniapp_validation.generation_preflight_validation import GenerationPreflightValidation
from app.validators.build_validator import BuildValidator
from app.validators.connectivity_validator import ConnectivityValidator
from app.repositories.state_store import StateStore
from app.services.miniapp_generation.artifact_manifests import ArtifactManifestsMixin
from app.services.miniapp_generation.service_llm_grounded_misc_mixins import ServiceLlmGroundedMiscMixins
from app.services.miniapp_generation.service_strategy_mixins import ServiceStrategyMixins


def _removed_symbol_name(*parts: str) -> str:
    return "".join(parts)


def _historical_runtime_value(*parts: str) -> str:
    return "".join(parts)


def _minimal_spec(entity_name: str) -> GroundedSpecModel:
    return GroundedSpecModel(
        metadata=Metadata(
            workspace_id="ws_test",
            conversation_id="conv_test",
            prompt_turn_id="turn_test",
            template_revision_id="tpl_test",
        ),
        target_platform=TargetPlatform.TELEGRAM,
        preview_profile=PreviewProfile.TELEGRAM_MOCK,
        product_goal=f"Internal app for {entity_name.lower()} operations.",
        actors=[
            Actor(
                actor_id="actor_client",
                name="Client",
                role="client",
                description="Creates shared items.",
                permissions_hint=[],
                evidence=[],
            )
        ],
        domain_entities=[
            DomainEntity(
                entity_id="entity_primary",
                name=entity_name,
                description=f"{entity_name} item",
                attributes=[
                    EntityAttribute(name="title", type="string", required=True, description="Title"),
                    EntityAttribute(name="details", type="text", required=False, description="Details"),
                ],
                evidence=[],
            )
        ],
        user_flows=[],
        ui_requirements=[
            UIRequirement(
                req_id="ui_root",
                category="screen",
                description="Show the main screen.",
                priority="must",
                evidence=[],
            )
        ],
        api_requirements=[],
        persistence_requirements=[],
        integration_requirements=[],
        security_requirements=[
            SecurityRequirement(
                security_req_id="sec_input",
                category="input_validation",
                rule="Validate input.",
                severity="medium",
                evidence=[],
            )
        ],
        platform_constraints=[
            PlatformConstraint(
                constraint_id="plt_nav",
                category="navigation",
                rule="Use the runtime shell.",
                severity="medium",
                evidence=[],
            )
        ],
        non_functional_requirements=[],
        assumptions=[
            Assumption(
                assumption_id="assume_default",
                text="Shared state is required.",
                status="active",
                rationale="Base invariant",
            )
        ],
        unknowns=[],
        contradictions=[],
        doc_refs=[],
    )


class _DummyGroundedService(ServiceLlmGroundedMiscMixins):
    def _append_event(self, job, event_type, message, details=None):  # noqa: ANN001
        del event_type, message, details
        return None

    def _append_trace(self, workspace_id, event_type, message, payload):  # noqa: ANN001
        del workspace_id, event_type, message, payload
        return None


class _DummyWorkspaceService:
    def __init__(self, existing: dict[str, str] | None = None) -> None:
        self.existing = existing or {}

    def try_read_text_file(self, workspace_id: str, file_path: str, *, run_id: str | None = None) -> str | None:
        del workspace_id, run_id
        return self.existing.get(file_path)

    def read_file(self, workspace_id: str, file_path: str, *, run_id: str | None = None) -> str:
        del workspace_id, run_id
        if file_path not in self.existing:
            raise FileNotFoundError(file_path)
        return self.existing[file_path]


class _PathWorkspaceService:
    def __init__(self, source_root: Path) -> None:
        self._source_root = source_root

    def source_dir(self, workspace_id: str) -> Path:
        del workspace_id
        return self._source_root


class _CaptureWholeFileCodegen(MiniappGenerationCodegen):
    def _resolve_whole_file_code_edits(self, **kwargs):  # noqa: ANN003
        return {"captured_entity_contract": kwargs.get("entity_contract")}


def test_task_model_overrides_light_balanced_visual_patch() -> None:
    primary, fallback = task_model_overrides(
        role="code_edit",
        generation_mode=GenerationMode.BALANCED,
        scope_mode="minimal_patch",
        visual_only_patch=True,
        target_file_count=3,
        backend_target_count=0,
    )
    assert primary is None
    assert fallback is None


def test_task_model_overrides_ignores_backend_touch() -> None:
    primary, fallback = task_model_overrides(
        role="code_edit",
        generation_mode=GenerationMode.BALANCED,
        scope_mode="minimal_patch",
        visual_only_patch=True,
        target_file_count=3,
        backend_target_count=1,
    )
    assert primary is None
    assert fallback is None


def test_task_model_overrides_light_balanced_shared_static_whole_file() -> None:
    primary, fallback = task_model_overrides(
        role="code_edit",
        generation_mode=GenerationMode.BALANCED,
        scope_mode="whole_file_build",
        target_file_count=1,
        backend_target_count=0,
        cluster_name="shared_static",
    )
    assert primary is None
    assert fallback is None


def test_role_only_scope_stays_whole_file_for_flow_prompt() -> None:
    prompt = (
        "Please update the client flow so clicking a record from the list opens a separate details page "
        "with the full information while keeping the current logic."
    )
    assert ServiceStrategyMixins._scope_mode("role_only_change", prompt, ["client"]) == "whole_file_build"


def test_grounded_spec_hygiene_extracts_prompt_entity_without_request_bias() -> None:
    entity_name = GroundedSpecHygieneRuntime.infer_entity_name(
        "I need an internal mini-app for vehicle maintenance scheduling with a simple queue."
    )
    assert entity_name == "VehicleMaintenanceScheduling"


def test_grounded_spec_hygiene_uses_neutral_entity_fallback() -> None:
    assert GroundedSpecHygieneRuntime.infer_entity_name("Сделай простое приложение с ролями и формами.") == "Entity"


def test_grounded_spec_hygiene_ignores_mobile_use_copy_when_extracting_entity() -> None:
    entity_name = GroundedSpecHygieneRuntime.infer_entity_name(
        "The app must be optimized for mobile use. Client role: the client should be able to submit a request, "
        "choose a time, and track the status of their request."
    )

    assert entity_name == "Request"


def test_entity_contract_overrides_low_signal_mobile_use_resource_with_prompt_entity() -> None:
    spec = _minimal_spec("MobileUse")
    spec.api_requirements = [
        APIRequirement(
            api_req_id="api_mobile_use_list",
            name="List mobile use",
            method="GET",
            path="/api/uses",
            purpose="Load current user items and role queues.",
            request_fields=[],
            response_fields=[],
            evidence=[],
        )
    ]
    runtime = MiniappGenerationEntityContract.__new__(MiniappGenerationEntityContract)

    contract = runtime.extract_entity_contract(
        prompt=(
            "The app must be optimized for mobile use. Client role: the client submits a request, "
            "manager reviews requests, and specialist processes assigned requests."
        ),
        grounded_spec=spec,
        generation_mode=GenerationMode.BALANCED,
    )

    assert contract["entity_slug"] == "request"
    assert contract["entity_slug_plural"] == "requests"
    assert contract["api_path"] == "/api/requests"
    assert contract["entity_name"] == "Request"


def test_grounded_spec_stabilization_does_not_inject_default_entity_api() -> None:
    spec = _minimal_spec("StoreOrder")
    spec.api_requirements = []
    spec.user_flows = []
    spec.persistence_requirements = []

    stabilized = GroundedSpecStabilizationRuntime().stabilize_grounded_spec(spec)

    assert stabilized.api_requirements == []


def test_grounded_spec_stabilization_prefers_domain_entity_slug() -> None:
    runtime = GroundedSpecStabilizationRuntime()
    spec = _minimal_spec("Shipment")
    assert runtime._default_api_resource_slug(spec) == "shipments"


def test_blocked_jobs_receive_failure_signature() -> None:
    service = _DummyGroundedService()
    job = JobRecord(
        workspace_id="ws_test",
        prompt="Build a record workflow.",
        target_platform=TargetPlatform.TELEGRAM,
        preview_profile=PreviewProfile.TELEGRAM_MOCK,
    )
    blocked = service._block_with_messages(
        job,
        ["Route manifest drift detected."],
        code="generation.route_manifest_drift",
        event_type="job_failed",
        failure_reason="Role page graph drift blocked generation.",
    )
    assert blocked.status == "blocked"
    assert blocked.failure_signature == "generation.route_manifest_drift:route_manifest_drift_detected"


def test_runtime_provider_validator_ignores_non_runtime_route_modules(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    routes_dir = workspace / "miniapp" / "app" / "routes"
    routes_dir.mkdir(parents=True)
    (routes_dir / "profiles.py").write_text(
        """
from fastapi import APIRouter

router = APIRouter(prefix="/api/profiles", tags=["profiles"])
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (routes_dir / "runtime.py").write_text(
        """
from fastapi import APIRouter

router = APIRouter(prefix="/api/runtime", tags=["runtime"])
""".strip()
        + "\n",
        encoding="utf-8",
    )

    issues = BuildValidator()._validate_runtime_provider_contract(workspace)
    assert not any(issue.code == "build.duplicate_runtime_route_provider" for issue in issues)


def test_state_store_delete_many_removes_multiple_keys(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.upsert("code_chunks", "code:ws_test:chunk_a", {"workspace_id": "ws_test"})
    store.upsert("code_chunks", "code:ws_test:chunk_b", {"workspace_id": "ws_test"})
    store.upsert("code_chunks", "code:ws_other:chunk_c", {"workspace_id": "ws_other"})

    store.delete_many("code_chunks", ["code:ws_test:chunk_a", "code:ws_test:chunk_b"])

    remaining = dict(store.items("code_chunks"))
    assert "code:ws_test:chunk_a" not in remaining
    assert "code:ws_test:chunk_b" not in remaining
    assert "code:ws_other:chunk_c" in remaining


def test_state_store_migrates_historical_job_events_fidelity_and_run_stage(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    historical_job = JobRecord(
        workspace_id="ws_test",
        prompt="Generate app",
        target_platform=TargetPlatform.TELEGRAM,
        preview_profile=PreviewProfile.TELEGRAM_MOCK,
    ).model_dump(mode="json")
    historical_job["fidelity"] = _historical_runtime_value("basic", "_scaffold")
    historical_job["events"] = [
        {
            "event_type": _historical_runtime_value("building", "_scaffold"),
            "message": "historical event",
            "details": {},
            "created_at": historical_job["created_at"],
        },
        {
            "event_type": _historical_runtime_value("scaffold", "_ready"),
            "message": "historical event",
            "details": {},
            "created_at": historical_job["created_at"],
        },
    ]
    store.upsert("jobs", "job_historical", historical_job)

    historical_run = RunRecord(
        workspace_id="ws_test",
        prompt="Generate app",
        intent="create",
        model_profile="openai_code_fast",
    ).model_dump(mode="json")
    historical_run["current_stage"] = _historical_runtime_value("building", "_scaffold")
    store.upsert("runs", "run_historical", historical_run)

    counters = store.migrate_persisted_runtime_state()

    migrated_job = store.get("jobs", "job_historical")
    migrated_run = store.get("runs", "run_historical")
    assert migrated_job is not None
    assert migrated_run is not None
    assert migrated_job["fidelity"] == "basic_app"
    assert [event["event_type"] for event in migrated_job["events"]] == ["building_surface", "surface_ready"]
    assert migrated_run["current_stage"] == "building_surface"
    assert counters["job_fidelities_migrated"] == 1
    assert counters["job_events_migrated"] == 2
    assert counters["run_stages_migrated"] == 1
    JobRecord.model_validate(migrated_job)
    RunRecord.model_validate(migrated_run)


def test_domain_models_no_longer_accept_historical_job_alias_values() -> None:
    job_payload = JobRecord(
        workspace_id="ws_test",
        prompt="Generate app",
        target_platform=TargetPlatform.TELEGRAM,
        preview_profile=PreviewProfile.TELEGRAM_MOCK,
    ).model_dump(mode="json")
    job_payload["fidelity"] = _historical_runtime_value("basic", "_scaffold")

    with pytest.raises(ValidationError):
        JobRecord.model_validate(job_payload)

    event_payload = {
        "event_type": _historical_runtime_value("building", "_scaffold"),
        "message": "historical event",
        "details": {},
    }
    with pytest.raises(ValidationError):
        JobEvent.model_validate(event_payload)


def test_filter_form_is_not_treated_as_persisted_mutation_surface() -> None:
    html = """
    <section class="filters">
      <form id="filter-form" class="filter-grid" novalidate>
        <select id="filter-status"></select>
        <input id="filter-equipment" type="text" />
        <input id="filter-start" type="date" />
        <input id="filter-end" type="date" />
        <button type="submit">Apply filters</button>
      </form>
    </section>
    """
    assert BuildValidator._looks_like_persisted_form_surface(html) is False


def test_preflight_route_schema_contract_supports_alias_imports(tmp_path) -> None:
    draft_root = tmp_path / "draft"
    routes_dir = draft_root / "miniapp" / "app" / "routes"
    routes_dir.mkdir(parents=True)
    schemas_path = draft_root / "miniapp" / "app" / "schemas.py"
    schemas_path.parent.mkdir(parents=True, exist_ok=True)
    schemas_path.write_text(
        """
class MaintenanceRecord:
    pass
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (routes_dir / "example.py").write_text(
        """
from app.schemas import MaintenanceRecord as MaintenanceSchema
""".strip()
        + "\n",
        encoding="utf-8",
    )

    issues = GenerationPreflightValidation.preflight_route_schema_issues(draft_root)
    assert not issues


def test_profile_pages_do_not_force_profiles_route_as_backend_target() -> None:
    grounded_spec = _minimal_spec("Shipment")
    page_graph = {
        "roles": {
            "client": {
                "pages": [
                    {
                        "route_path": "/client/profile",
                        "file_path": "miniapp/app/static/client/profile/index.html",
                        "page_kind": "profile",
                    }
                ]
            }
        }
    }
    inferred = MiniappGenerationPlanRuntime.detect_missing_backend_contract_targets_from_spec(
        grounded_spec=grounded_spec,
        page_graph=page_graph,
        current_target_files=[],
        backend_targets=[],
        entity_contract={"route_file": "miniapp/app/routes/shipments.py"},
    )
    assert "miniapp/app/routes/profiles.py" not in inferred


def test_openrouter_rescue_is_used_when_openai_is_missing(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")

    client = OpenRouterClient(
        SimpleNamespace(openrouter_app_name="Grounded Mini-App Platform", openrouter_site_url="http://localhost:5173"),
        workspace_log_service=None,
    )
    captured: dict[str, str] = {}

    def fake_request_json_mode(self, **kwargs):  # noqa: ANN001
        captured["provider"] = kwargs["provider"]
        captured["model"] = kwargs["model"]
        return {"payload": {"ok": True}, "cache_stats": {}}

    monkeypatch.setattr(OpenRouterClient, "_request_json_mode", fake_request_json_mode)

    result = client.generate_structured(
        role="code_edit",
        schema_name="test_schema",
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False},
        system_prompt="system",
        user_prompt="user",
    )

    assert result["model"] == "anthropic/claude-sonnet-4.6"
    assert result["response_mode"] == "json_object"
    assert captured == {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.6"}


def test_openrouter_rescue_is_used_after_openai_insufficient_quota(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")

    client = OpenRouterClient(
        SimpleNamespace(openrouter_app_name="Grounded Mini-App Platform", openrouter_site_url="http://localhost:5173"),
        workspace_log_service=None,
    )
    captured: dict[str, str] = {}

    def fake_request_structured(self, **kwargs):  # noqa: ANN001
        if kwargs["provider"] == "openai":
            raise RuntimeError('OpenAI responses returned 429: {"error":{"code":"insufficient_quota"}}')
        raise AssertionError("structured fallback should not run on the rescue provider")

    def fake_request_json_mode(self, **kwargs):  # noqa: ANN001
        captured["provider"] = kwargs["provider"]
        captured["model"] = kwargs["model"]
        return {"payload": {"ok": True}, "cache_stats": {}}

    monkeypatch.setattr(OpenRouterClient, "_request_structured", fake_request_structured)
    monkeypatch.setattr(OpenRouterClient, "_request_json_mode", fake_request_json_mode)

    result = client.generate_structured(
        role="code_edit",
        schema_name="test_schema",
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False},
        system_prompt="system",
        user_prompt="user",
    )

    assert result["model"] == "anthropic/claude-sonnet-4.6"
    assert result["response_mode"] == "json_object"
    assert captured == {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.6"}


def test_openai_quota_circuit_breaker_skips_direct_provider_on_next_call(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")

    client = OpenRouterClient(
        SimpleNamespace(openrouter_app_name="Grounded Mini-App Platform", openrouter_site_url="http://localhost:5173"),
        workspace_log_service=None,
    )
    provider_calls: list[str] = []

    def fake_request_structured(self, **kwargs):  # noqa: ANN001
        provider_calls.append(kwargs["provider"])
        if kwargs["provider"] == "openai":
            raise RuntimeError('OpenAI responses returned 429: {"error":{"code":"insufficient_quota"}}')
        raise AssertionError("structured generation should not execute on the rescue provider")

    def fake_request_json_mode(self, **kwargs):  # noqa: ANN001
        provider_calls.append(f"{kwargs['provider']}:{kwargs['model']}")
        return {"payload": {"ok": True}, "cache_stats": {}}

    monkeypatch.setattr(OpenRouterClient, "_request_structured", fake_request_structured)
    monkeypatch.setattr(OpenRouterClient, "_request_json_mode", fake_request_json_mode)

    first = client.generate_structured(
        role="code_edit",
        schema_name="first_schema",
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False},
        system_prompt="system",
        user_prompt="first",
    )
    second = client.generate_structured(
        role="code_edit",
        schema_name="second_schema",
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False},
        system_prompt="system",
        user_prompt="second",
    )

    assert first["model"] == "anthropic/claude-sonnet-4.6"
    assert second["model"] == "anthropic/claude-sonnet-4.6"
    assert provider_calls == [
        "openai",
        "openrouter:anthropic/claude-sonnet-4.6",
        "openrouter:anthropic/claude-sonnet-4.6",
    ]


def test_openrouter_rescue_prefers_fast_model_for_mini_overrides(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")

    client = OpenRouterClient(
        SimpleNamespace(openrouter_app_name="Grounded Mini-App Platform", openrouter_site_url="http://localhost:5173"),
        workspace_log_service=None,
    )
    provider_calls: list[str] = []

    def fake_request_structured(self, **kwargs):  # noqa: ANN001
        provider_calls.append(f"{kwargs['provider']}:{kwargs['model']}")
        if kwargs["provider"] == "openai":
            raise RuntimeError('OpenAI responses returned 429: {"error":{"code":"insufficient_quota"}}')
        raise AssertionError("structured generation should not execute on the rescue provider")

    def fake_request_json_mode(self, **kwargs):  # noqa: ANN001
        provider_calls.append(f"{kwargs['provider']}:{kwargs['model']}")
        return {"payload": {"ok": True}, "cache_stats": {}}

    monkeypatch.setattr(OpenRouterClient, "_request_structured", fake_request_structured)
    monkeypatch.setattr(OpenRouterClient, "_request_json_mode", fake_request_json_mode)

    result = client.generate_structured(
        role="code_edit",
        schema_name="shared_static_schema",
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False},
        system_prompt="system",
        user_prompt="user",
        model_override="gpt-5.4-mini",
        fallback_model_override="gpt-5.4-mini",
    )

    assert result["model"] == "anthropic/claude-haiku-4.5"
    assert provider_calls == [
        "openai:gpt-5.4-mini",
        "openrouter:anthropic/claude-haiku-4.5",
    ]


def test_openrouter_chat_payload_uses_bounded_max_tokens(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    client = OpenRouterClient(
        SimpleNamespace(openrouter_app_name="Grounded Mini-App Platform", openrouter_site_url="http://localhost:5173"),
        workspace_log_service=None,
    )
    captured: dict[str, object] = {}

    def fake_post_json_with_retries(self, **kwargs):  # noqa: ANN001
        captured.update(kwargs["payload"])
        return {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {}}

    monkeypatch.setattr(OpenRouterClient, "_post_json_with_retries", fake_post_json_with_retries)

    result = client._chat_json_object(
        role="code_edit",
        schema_name="bounded_schema",
        model="anthropic/claude-sonnet-4.6",
        system_prompt="system",
        user_prompt="user",
        provider="openrouter",
    )

    assert result["payload"] == {"ok": True}
    assert captured["max_tokens"] == 32768


def test_openrouter_budget_error_reduces_max_tokens() -> None:
    response = httpx.Response(
        402,
        content=b'{"error":{"message":"This request requires more credits, or fewer max_tokens. You requested up to 32768 tokens, but can only afford 26397."}}',
    )

    reduced = OpenRouterClient._openrouter_reduced_max_tokens(
        response=response,
        payload={"max_tokens": 32768},
        provider="openrouter",
    )

    assert reduced == 25373


def test_openrouter_budget_error_can_reduce_below_8192() -> None:
    response = httpx.Response(
        402,
        content=b'{"error":{"message":"This request requires more credits, or fewer max_tokens. You requested up to 8192 tokens, but can only afford 2366."}}',
    )

    reduced = OpenRouterClient._openrouter_reduced_max_tokens(
        response=response,
        payload={"max_tokens": 8192},
        provider="openrouter",
    )

    assert reduced == 1342


def test_role_ui_clusters_can_run_in_small_parallel_batches() -> None:
    clusters = [
        {"cluster_name": "role_manager_ui_bookings", "target_files": ["a"]},
        {"cluster_name": "role_manager_ui_inventory", "target_files": ["b"]},
        {"cluster_name": "role_manager_ui_profile", "target_files": ["c"]},
        {"cluster_name": "backend_route_manager", "target_files": ["d"]},
    ]

    grouped = GenerationProgressReportingRuntime._group_generation_clusters_for_execution(clusters)

    assert [entry["cluster_name"] for entry in grouped[0]] == [
        "role_manager_ui_bookings",
        "role_manager_ui_inventory",
    ]
    assert [entry["cluster_name"] for entry in grouped[1]] == ["role_manager_ui_profile"]
    assert [entry["cluster_name"] for entry in grouped[2]] == ["backend_route_manager"]


def test_role_static_targets_split_by_role_page_surface() -> None:
    clusters = MiniappGenerationPageGraphRuntime._build_generation_clusters(
        [
            "miniapp/app/static/client/index.html",
            "miniapp/app/static/client/styles.css",
            "miniapp/app/static/client/app.js",
            "miniapp/app/static/client/items/index.html",
            "miniapp/app/static/client/items/styles.css",
            "miniapp/app/static/client/items/app.js",
            "miniapp/app/static/manager/index.html",
        ]
    )

    assert clusters == [
        {"cluster_name": "role_manager_ui_root", "target_files": ["miniapp/app/static/manager/index.html"]},
        {
            "cluster_name": "role_client_ui_root",
            "target_files": [
                "miniapp/app/static/client/index.html",
                "miniapp/app/static/client/styles.css",
                "miniapp/app/static/client/app.js",
            ],
        },
        {
            "cluster_name": "role_client_ui_items",
            "target_files": [
                "miniapp/app/static/client/items/index.html",
                "miniapp/app/static/client/items/styles.css",
                "miniapp/app/static/client/items/app.js",
            ],
        },
    ]


def test_backend_route_targets_group_into_one_backend_routes_cluster() -> None:
    clusters = MiniappGenerationPageGraphRuntime._build_generation_clusters(
        [
            "miniapp/app/main.py",
            "miniapp/app/db.py",
            "miniapp/app/schemas.py",
            "miniapp/app/routes/bookings.py",
            "miniapp/app/routes/client.py",
            "miniapp/app/routes/manager.py",
        ]
    )

    assert clusters == [
        {
            "cluster_name": "backend_support",
            "target_files": [
                "miniapp/app/main.py",
                "miniapp/app/db.py",
                "miniapp/app/schemas.py",
            ],
        },
        {
            "cluster_name": "backend_routes",
            "target_files": [
                "miniapp/app/routes/bookings.py",
                "miniapp/app/routes/client.py",
                "miniapp/app/routes/manager.py",
            ],
        },
    ]


def test_balanced_role_ui_timeout_uses_service_default_not_short_cap() -> None:
    runtime = object.__new__(MiniappGenerationCodegen)
    runtime.WHOLE_FILE_UI_CLUSTER_TIMEOUT_SECONDS = 420
    runtime.WHOLE_FILE_CLUSTER_TIMEOUT_SECONDS = 240

    timeout = runtime._whole_file_batch_timeout_seconds(
        [{"cluster_name": "role_client_ui_root", "target_files": ["miniapp/app/static/client/index.html"]}],
        generation_mode=GenerationMode.BALANCED,
    )

    assert timeout == 420


def test_whole_file_codegen_receives_entity_contract() -> None:
    runtime = object.__new__(_CaptureWholeFileCodegen)
    entity_contract = {"api_path": "/api/shipments", "plural_label": "shipments"}

    result = runtime._resolve_code_edits(
        workspace_id="ws_test",
        draft_run_id="run_test",
        prompt="Build a shipment app.",
        grounded_spec=_minimal_spec("Shipment"),
        entity_contract=entity_contract,
        role_scope=["client", "specialist", "manager"],
        file_contexts={},
        target_files=["miniapp/app/static/client/index.html"],
        role_contract={},
        page_graph={},
        intent="create",
        scope_mode="whole_file_build",
        generation_mode=GenerationMode.BALANCED,
        creative_direction={},
    )

    assert result["captured_entity_contract"] == entity_contract


def test_success_summary_hides_internal_provider_and_repair_diagnostics() -> None:
    summary = MiniappGenerationReporting._build_agent_summary(
        grounded_spec=_minimal_spec("Shipment"),
        role_scope=["client", "specialist"],
        operations=[],
        generation_mode=GenerationMode.BALANCED,
        assistant_message=(
            "Initial iteration diagnostics: role_manager_ui_bundle could not be generated because "
            "OpenRouter provider budget requires more credits. Provider-budget fallback applied."
        ),
    )

    lowered = summary.lower()
    assert "built a balanced draft" in lowered
    assert "openrouter" not in lowered
    assert "fallback" not in lowered
    assert "repair" not in lowered
    assert "diagnostics" not in lowered


def test_generation_repair_accepts_generation_mode_for_model_routing() -> None:
    import inspect

    signature = inspect.signature(MiniappGenerationRepair.repair_draft_after_failure)

    assert "generation_mode" in signature.parameters


def test_route_schema_contract_sync_entrypoint_removed() -> None:
    assert not hasattr(MiniappGenerationContractSchema, "".join(("_", "synchronize", "_route_schema_contract")))


def test_build_validator_does_not_treat_issue_heading_as_mutation() -> None:
    html = """
    <section class="detail-card">
      <p class="eyebrow">Issue</p>
      <h1>Issue Detail</h1>
      <button type="button">Refresh status</button>
    </section>
    """

    assert BuildValidator._looks_like_persisted_form_surface(html) is False


def test_build_validator_accepts_role_prefixed_profile_route() -> None:
    page = {
        "route_path": "/manager/profile",
        "page_kind": "form",
        "file_path": "miniapp/app/static/manager/profile/index.html",
    }

    assert BuildValidator._looks_like_role_profile_page(page, "manager") is True


def test_connectivity_validator_accepts_submit_spinner_as_loading_state() -> None:
    content = """
    <form id="issue-form"></form>
    <script>
      document.getElementById("issue-form").addEventListener("submit", async () => {
        submitBtn.textContent = "Submitting...";
        await fetch("/api/items", { method: "POST" });
      });
    </script>
    """

    assert ConnectivityValidator._contains_state(content.lower(), "inline submit spinner", state_kind="loading") is True


def test_generated_python_tests_use_progress_status_when_schema_choices_are_unavailable() -> None:
    source = (Path(__file__).resolve().parents[1] / "app/services/miniapp_generation/artifact_python_tests.py").read_text(
        encoding="utf-8"
    )

    assert 'return choices[0] if choices else "in_progress"' in source


def test_generation_normal_loop_no_longer_exposes_source_stabilizer() -> None:
    assert not hasattr(MiniappGenerationNormalLoop, _removed_symbol_name("stabilize_", "draft_contract_", "from_source"))


def test_route_manifest_dedupes_duplicate_file_paths_preferring_detail_page() -> None:
    pages = [
        {
            "page_id": "manager_requests_id",
            "route_path": "/manager/{requests_id}",
            "file_path": "miniapp/app/static/manager/requests_id/index.html",
            "page_kind": "page",
        },
        {
            "page_id": "manager_request_detail",
            "route_path": "/manager/requests/{id}",
            "file_path": "miniapp/app/static/manager/requests_id/index.html",
            "page_kind": "detail",
        },
    ]

    deduped = ArtifactManifestsMixin._dedupe_route_manifest_pages(pages)

    assert len(deduped) == 1
    assert deduped[0]["page_id"] == "manager_request_detail"


def test_route_manifest_dedupes_equivalent_dynamic_routes() -> None:
    pages = [
        {
            "page_id": "manager_request_detail",
            "route_path": "/manager/requests/:id",
            "file_path": "miniapp/app/static/manager/requests_detail/index.html",
            "page_kind": "detail",
        },
        {
            "page_id": "manager_request_detail",
            "route_path": "/manager/requests/{id}",
            "file_path": "miniapp/app/static/manager/requests_id/index.html",
            "page_kind": "detail",
        },
    ]

    deduped = ArtifactManifestsMixin._dedupe_route_manifest_pages(pages)

    assert len(deduped) == 1
    assert deduped[0]["route_path"] == "/manager/requests/{id}"


def test_build_validator_flags_static_ui_numeric_artifacts() -> None:
    content = """
    actionStatusEl.textContent = "Saving decision85";
    datesEl.textContent = `${formatDate(item.start_date)} 192 ${formatDate(item.end_date)}`;
    <div class="loading">Loading requests10141515</div>
    """

    issues = BuildValidator._static_ui_text_artifact_issues(content, "miniapp/app/static/manager/app.js")

    assert {issue.code for issue in issues} == {"build.static_ui_text_artifact"}
    assert len(issues) == 4


def test_build_validator_flags_generic_state_copy_and_broken_entity_fragments() -> None:
    content = """
    <span class="chevron">181;</span>
    <span class="chevron">203a</span>
    <span class="chevron">›</span>
    <p>Block 181</p>
    <span id="loading-state">Loading...</span>
    <span id="loading-state">Loading all client requests…</span>
    <span id="error-state">Couldn't load the request overview. Please refresh.</span>
    stateEl.textContent = "Unable to load data. Try again.";
    """

    issues = BuildValidator._static_ui_text_artifact_issues(content, "miniapp/app/static/manager/index.html")

    assert {issue.code for issue in issues} == {"build.static_ui_text_artifact"}
    assert len(issues) == 5


def test_build_validator_flags_empty_status_indicator_placeholders() -> None:
    content = """
    <div class="filters">
      <button class="chip chip-active">Open</button>
      <button class="chip">In progress</button>
      <button class="chip">Done</button>
    </div>
    <div class="filter-feedback">
      <span class="status-pill status-pill-info"></span>
      <span class="status-pill status-pill-danger"></span>
    </div>
    """

    issues = BuildValidator._static_ui_text_artifact_issues(content, "miniapp/app/static/manager/index.html")

    assert {issue.code for issue in issues} == {"build.static_ui_text_artifact"}
    assert len(issues) == 1


def test_frontend_contract_sync_maps_generic_collection_aliases_to_entity_api() -> None:
    content = 'const response = await window.miniappApiFetch("/api/items?status=pending");'

    updated = MiniappGenerationContractFrontend._normalize_entity_api_paths(
        content,
        {"api_path": "/api/requests", "entity_slug_plural": "requests", "route_file": "miniapp/app/routes/requests.py"},
    )

    assert '"/api/requests?status=pending"' in updated


def test_frontend_contract_sync_cleans_static_ui_text_artifacts() -> None:
    content = """
    datesEl.textContent = `${formatDate(item.start_date)} 192 ${formatDate(item.end_date)}`;
    actionStatusEl.textContent = "Saving decision85";
    <div class="loading">Loading requests10141515</div>
    <span class="chevron">181;</span>
    <span class="chevron">203a</span>
    <span class="chevron">›</span>
    <p>Block 181</p>
    <span id="loading-state">Loading...</span>
    <span id="loading-state">Loading all client requests…</span>
    <span id="error-state">Unable to load data. Try again.</span>
    <span id="error-state">Couldn't load the request overview. Please refresh.</span>
    stateEl.textContent = "Loading data...";
    errorEl.textContent = "Could not load requests. Try again soon.";
    """

    updated = MiniappGenerationContractFrontend._clean_static_ui_text_artifacts(content)

    assert " 192 " not in updated
    assert "decision85" not in updated
    assert "requests10141515" not in updated
    assert " - " in updated
    assert '"Saving decision"' in updated
    assert ">Loading requests<" not in updated
    assert "181;" not in updated
    assert "&rsaquo;" not in updated
    assert ">203a<" not in updated
    assert ">›<" not in updated
    assert "Block 181" not in updated
    assert ">Loading...<" not in updated
    assert "Loading all client requests" not in updated
    assert "Unable to load data. Try again." not in updated
    assert "Couldn't load the request overview" not in updated
    assert "Could not load requests" not in updated
    assert 'textContent = "";' in updated


def test_frontend_contract_sync_removes_empty_status_indicator_placeholders() -> None:
    content = """
    <div class="filters">
      <button class="chip chip-active">Open</button>
      <button class="chip">In progress</button>
      <button class="chip">Done</button>
    </div>
    <div class="filter-feedback">
      <span class="status-pill status-pill-info"></span>
      <span class="status-pill status-pill-danger"></span>
    </div>
    """

    updated = MiniappGenerationContractFrontend._clean_static_ui_text_artifacts(content)

    assert "status-pill-info" not in updated
    assert "status-pill-danger" not in updated
    assert "filter-feedback" not in updated
    assert "chip-active" in updated


def test_frontend_contract_sync_injects_status_alias_bridge_for_noncanonical_schema_literals() -> None:
    content = """
    const response = await window.miniappApiFetch("/api/items");
    const data = await response.json();
    const status = item.status || "open";
    """

    updated = MiniappGenerationContractFrontend._inject_status_alias_bridge(
        "miniapp/app/static/specialist/app.js",
        content,
        {"status_literals": ["scheduled", "in_progress", "completed", "cancelled"]},
    )

    assert "__GROUND_STATUS_READ_ALIASES__" in updated
    assert '"scheduled": "open"' in updated
    assert "__groundNormalizeStatusPayload(await response.json())" in updated


def test_frontend_contract_sync_prefers_schema_status_literals_over_prompt_contract() -> None:
    schema_content = 'RecordStatus = Literal["scheduled", "in_progress", "completed", "cancelled"]'

    assert MiniappGenerationContractFrontend._schema_status_literals_from_text(schema_content) == [
        "scheduled",
        "in_progress",
        "completed",
        "cancelled",
    ]


def test_frontend_contract_sync_does_not_treat_active_css_class_as_status_alias() -> None:
    content = """
    <button class="chip active" data-status="open">Open</button>
    const activeFilter = "all";
    const status = item.status || "open";
    """

    alias_map = MiniappGenerationContractFrontend._status_read_alias_map(
        content,
        {"status_literals": ["scheduled", "in_progress", "completed", "cancelled"]},
    )

    assert alias_map == {"scheduled": "open"}


def test_build_validator_flags_missing_status_alias_bridge_for_noncanonical_schema_literals(tmp_path: Path) -> None:
    workspace = tmp_path
    static_dir = workspace / "miniapp" / "app" / "static" / "specialist"
    static_dir.mkdir(parents=True)
    schemas_path = workspace / "miniapp" / "app" / "schemas.py"
    schemas_path.parent.mkdir(parents=True, exist_ok=True)
    schemas_path.write_text(
        'from typing import Literal\nRecordStatus = Literal["scheduled", "in_progress", "completed", "cancelled"]\n',
        encoding="utf-8",
    )
    (static_dir / "app.js").write_text(
        'const response = await window.miniappApiFetch("/api/items");\n'
        'const data = await response.json();\n'
        'const status = item.status || "open";\n',
        encoding="utf-8",
    )

    issues = BuildValidator()._validate_status_alias_contract(workspace)

    assert any(issue.code == "build.status_alias_bridge_missing" for issue in issues)
