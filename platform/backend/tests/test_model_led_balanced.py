from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx

from app.ai.openrouter_client import OpenRouterClient
from app.ai.model_registry import task_model_overrides
from app.models.common import GenerationMode, PreviewProfile, TargetPlatform
from app.models.domain import JobRecord
from app.models.domain import DraftFileOperation
from app.models.grounded_spec import (
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
                description="Creates records.",
                permissions_hint=[],
                evidence=[],
            )
        ],
        domain_entities=[
            DomainEntity(
                entity_id="entity_primary",
                name=entity_name,
                description=f"{entity_name} record",
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
    assert primary == "gpt-5.1-codex-mini"
    assert fallback == "gpt-5.1-codex-mini"


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
    assert primary == "gpt-5.1-codex-mini"
    assert fallback == "gpt-5.1-codex-mini"


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
        model_override="gpt-5.1-codex-mini",
        fallback_model_override="gpt-5.1-codex-mini",
    )

    assert result["model"] == "anthropic/claude-haiku-4.5"
    assert provider_calls == [
        "openai:gpt-5.1-codex-mini",
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


def test_provider_budget_role_ui_fallback_materializes_contract_pages() -> None:
    runtime = object.__new__(MiniappGenerationCodegen)
    runtime.workspace_service = _DummyWorkspaceService(
        {"miniapp/app/static/manager/profile/index.html": "<!doctype html><html></html>"}
    )

    result = runtime._whole_file_error_fallback_result(
        workspace_id="ws_test",
        draft_run_id="run_test",
        cluster_name="role_manager_ui_bundle",
        cluster_targets=[
            "miniapp/app/static/manager/index.html",
            "miniapp/app/static/manager/styles.css",
            "miniapp/app/static/manager/app.js",
            "miniapp/app/static/manager/details/index.html",
            "miniapp/app/static/manager/details/styles.css",
            "miniapp/app/static/manager/details/app.js",
            "miniapp/app/static/manager/profile/index.html",
        ],
        error_message="OpenRouter chat/completions returned 402: can only afford 742 tokens",
        entity_contract={
            "api_path": "/api/cases",
            "singular_label": "case",
            "plural_label": "cases",
            "status_literals": ["open", "resolved"],
            "key_fields": [{"name": "customer_name", "type": "string", "required": True}],
        },
    )

    assert result is not None
    operations = result["operations"]
    paths = {operation.file_path for operation in operations}
    assert "miniapp/app/static/manager/index.html" in paths
    assert "miniapp/app/static/manager/details/app.js" in paths
    assert "miniapp/app/static/manager/profile/index.html" not in paths
    joined = "\n".join(str(operation.content or "") for operation in operations)
    assert "/api/cases" in joined
    assert "customer_name" in joined
    assert "Booking" not in joined


def test_split_role_ui_timeout_fallback_materializes_contract_pages() -> None:
    runtime = object.__new__(MiniappGenerationCodegen)
    runtime.workspace_service = _DummyWorkspaceService()

    result = runtime._whole_file_role_ui_fallback_result(
        workspace_id="ws_test",
        draft_run_id="run_test",
        cluster_name="role_client_ui_details",
        cluster_targets=[
            "miniapp/app/static/client/details/index.html",
            "miniapp/app/static/client/details/styles.css",
            "miniapp/app/static/client/details/app.js",
        ],
        entity_contract={
            "api_path": "/api/cases",
            "singular_label": "case",
            "plural_label": "cases",
            "status_literals": ["open", "resolved"],
            "key_fields": [{"name": "customer_name", "type": "string", "required": True}],
        },
        timeout_seconds=420,
    )

    assert result is not None
    assert result["fallback_used"] is True
    assert {operation.file_path for operation in result["operations"]} == {
        "miniapp/app/static/client/details/index.html",
        "miniapp/app/static/client/details/styles.css",
        "miniapp/app/static/client/details/app.js",
    }
    joined = "\n".join(str(operation.content or "") for operation in result["operations"])
    assert "/api/cases" in joined
    assert "customer_name" in joined


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


def test_route_schema_contract_adds_actual_schema_prefix_candidate() -> None:
    runtime = object.__new__(MiniappGenerationContractSchema)
    runtime.workspace_service = _DummyWorkspaceService(
        {
            "miniapp/app/schemas.py": (
                "from datetime import datetime\n"
                "from pydantic import BaseModel\n\n"
                "class CompanyVehicleMaintenanceCreate(BaseModel):\n"
                "    start_date: datetime\n\n"
                "class CompanyVehicleMaintenanceRead(CompanyVehicleMaintenanceCreate):\n"
                "    id: str\n"
            ),
            "miniapp/app/routes/maintenances.py": (
                'SCHEMA_PREFIX = "Maintenance"\n\n'
                "def _candidate_schema_names() -> list[str]:\n"
                "    return [SCHEMA_PREFIX]\n\n"
                "def _schema_model(suffixes: tuple[str, ...]):\n"
                "    return None\n\n"
                "@router.get(\"/maintenances\")\n"
                "def list_records(): pass\n\n"
                "@router.post(\"/maintenances\")\n"
                "def create_record(): pass\n\n"
                "@router.get(\"/maintenances/{item_id}\")\n"
                "def get_record(item_id: str): pass\n\n"
                "@router.put(\"/maintenances/{item_id}\")\n"
                "@router.patch(\"/maintenances/{item_id}\")\n"
                "def update_record(item_id: str): pass\n"
            ),
        }
    )

    operations = runtime._synchronize_route_schema_contract(
        "ws_test",
        "run_test",
        [
            DraftFileOperation(
                file_path="miniapp/app/routes/maintenances.py",
                operation="replace",
                content=runtime.workspace_service.existing["miniapp/app/routes/maintenances.py"],
                reason="test",
            )
        ],
    )

    route_content = next(operation.content for operation in operations if operation.file_path == "miniapp/app/routes/maintenances.py")
    assert "CompanyVehicleMaintenance" in route_content
    assert "return seen" in route_content


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
        await fetch("/api/records", { method: "POST" });
      });
    </script>
    """

    assert ConnectivityValidator._contains_state(content.lower(), "inline submit spinner", state_kind="loading") is True


def test_generated_python_tests_use_progress_status_when_schema_choices_are_unavailable() -> None:
    source = (Path(__file__).resolve().parents[1] / "app/services/miniapp_generation/artifact_python_tests.py").read_text(
        encoding="utf-8"
    )

    assert 'return choices[0] if choices else "in_progress"' in source


def test_stabilizer_preserves_valid_refresh_ui_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    draft = tmp_path / "draft"
    source_page = source / "miniapp/app/static/client"
    draft_page = draft / "miniapp/app/static/client"
    for path in (source_page, draft_page):
        path.mkdir(parents=True)
    (source / "miniapp/app").mkdir(parents=True, exist_ok=True)
    (draft / "miniapp/app").mkdir(parents=True, exist_ok=True)
    (source / "miniapp/app/schemas.py").write_text("class RecordRead:\n    pass\n", encoding="utf-8")
    (draft / "miniapp/app/schemas.py").write_text("class RecordRead:\n    pass\n", encoding="utf-8")
    (source_page / "index.html").write_text(
        '<main><section id="records"></section></main><script src="/static/client/app.js"></script>',
        encoding="utf-8",
    )
    (source_page / "app.js").write_text('document.getElementById("records");', encoding="utf-8")
    (source_page / "styles.css").write_text(".page{}\n", encoding="utf-8")
    (draft_page / "index.html").write_text(
        '<main><button id="refresh-action">Refresh</button><section id="records"></section></main><script src="/static/client/app.js"></script>',
        encoding="utf-8",
    )
    (draft_page / "app.js").write_text(
        'document.getElementById("records"); document.getElementById("refresh-action");',
        encoding="utf-8",
    )
    (draft_page / "styles.css").write_text(".page{}.refresh{}\n", encoding="utf-8")

    runtime = MiniappGenerationNormalLoop(SimpleNamespace(workspace_service=_PathWorkspaceService(source)))
    changed = runtime.stabilize_draft_contract_from_source(workspace_id="ws_test", draft_source=draft)

    assert "miniapp/app/static/client/index.html" not in changed
    assert "refresh-action" in (draft_page / "index.html").read_text(encoding="utf-8")
    assert "refresh-action" in (draft_page / "app.js").read_text(encoding="utf-8")


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
    assert len(issues) == 3


def test_frontend_contract_sync_maps_generic_record_aliases_to_entity_api() -> None:
    content = 'const response = await window.miniappApiFetch("/api/records?status=pending");'

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
    """

    updated = MiniappGenerationContractFrontend._clean_static_ui_text_artifacts(content)

    assert " 192 " not in updated
    assert "decision85" not in updated
    assert "requests10141515" not in updated
    assert " - " in updated
    assert '"Saving decision"' in updated
    assert ">Loading requests<" in updated
