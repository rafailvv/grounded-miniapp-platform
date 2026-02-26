from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.app_ir import (
    Action,
    AppIRModel,
    AuthModel,
    Component,
    DataField,
    Entity,
    IRMetadata,
    Integration,
    Permission,
    Screen,
    SecurityPolicy,
    StorageBinding,
    TelemetryHook,
    TraceabilityLink,
    Transition,
    Variable,
)
from app.models.grounded_spec import (
    Actor,
    APIRequirement,
    Assumption,
    Contradiction,
    DomainEntity,
    EntityAttribute,
    EvidenceLink,
    GroundedSpecModel,
    Metadata,
    NonFunctionalRequirement,
    PersistenceRequirement,
    PlatformConstraint,
    SecurityRequirement,
    UIRequirement,
    UserFlow,
    FlowStep,
)
from app.validators.app_ir_validator import AppIRValidator
from app.validators.grounded_spec_validator import GroundedSpecValidator


def make_valid_spec() -> GroundedSpecModel:
    evidence = [EvidenceLink(doc_ref_id="doc-1", evidence_type="explicit")]
    return GroundedSpecModel(
        metadata=Metadata(
            workspace_id="ws_1",
            conversation_id="conv_1",
            prompt_turn_id="turn_1",
            template_revision_id="rev_1",
        ),
        target_platform="telegram_mini_app",
        preview_profile="telegram_mock",
        product_goal="Build a validated consultation booking mini-app.",
        actors=[
            Actor(
                actor_id="actor_1",
                name="User",
                role="customer",
                description="Primary end user.",
                evidence=evidence,
            )
        ],
        domain_entities=[
            DomainEntity(
                entity_id="entity_1",
                name="Submission",
                description="Collected form data.",
                attributes=[EntityAttribute(name="name", type="string", required=True)],
                evidence=evidence,
            )
        ],
        user_flows=[
            UserFlow(
                flow_id="flow_1",
                name="Booking flow",
                goal="Submit the booking form.",
                steps=[FlowStep(step_id="step_1", order=1, actor_id="actor_1", action="Open the form")],
                acceptance_criteria=["The form can be submitted successfully."],
                evidence=evidence,
            )
        ],
        ui_requirements=[
            UIRequirement(
                req_id="ui_1",
                category="form",
                description="Show a booking form.",
                priority="must",
                evidence=evidence,
            )
        ],
        api_requirements=[
            APIRequirement(
                api_req_id="api_1",
                name="Create booking",
                method="POST",
                path="/api/submissions",
                purpose="Store booking data.",
                request_fields=[],
                response_fields=[],
                evidence=evidence,
            )
        ],
        persistence_requirements=[
            PersistenceRequirement(
                persistence_req_id="persist_1",
                entity_id="entity_1",
                operation="create",
                storage_type="postgres",
                evidence=evidence,
            )
        ],
        integration_requirements=[],
        security_requirements=[
            SecurityRequirement(
                security_req_id="sec_1",
                category="telegram_initdata",
                rule="Validate initData on the server.",
                severity="critical",
                evidence=evidence,
            )
        ],
        platform_constraints=[
            PlatformConstraint(
                constraint_id="platform_1",
                category="sdk",
                rule="Use Telegram WebApp SDK.",
                severity="critical",
                evidence=evidence,
            )
        ],
        non_functional_requirements=[
            NonFunctionalRequirement(
                nfr_id="nfr_1",
                category="observability",
                description="Preserve traceability.",
                priority="must",
                evidence=evidence,
            )
        ],
        assumptions=[Assumption(assumption_id="a_1", text="Single flow", status="active", rationale="v1 scope")],
        unknowns=[],
        contradictions=[],
        doc_refs=[
            {
                "doc_ref_id": "doc-1",
                "source_type": "project_doc",
                "file_path": "docs/README.md",
                "chunk_id": "chunk-1",
                "relevance": 1.0,
            }
        ],
    )


def make_valid_ir() -> AppIRModel:
    return AppIRModel(
        metadata=IRMetadata(workspace_id="ws_1", grounded_spec_version="1.0.0", template_revision_id="rev_1"),
        app_id="app_1",
        title="Booking mini-app",
        platform="telegram_mini_app",
        preview_profile="telegram_mock",
        entry_screen_id="screen_form",
        terminal_screen_ids=["screen_success"],
        variables=[
            Variable(
                variable_id="var_name",
                name="name",
                type="string",
                required=True,
                source="user_input",
                trust_level="untrusted",
                scope="screen",
            ),
            Variable(
                variable_id="var_submission_id",
                name="submission_id",
                type="uuid",
                required=False,
                source="validated_init_data",
                trust_level="validated",
                scope="session",
            ),
        ],
        entities=[Entity(entity_id="entity_1", name="Submission", fields=[DataField(name="name", type="string", required=True)])],
        screens=[
            Screen(
                screen_id="screen_form",
                kind="form",
                title="Form",
                components=[
                    Component(
                        component_id="cmp_name",
                        type="input",
                        label="Name",
                        binding_variable_id="var_name",
                        required=True,
                        validators=[],
                    ),
                    Component(
                        component_id="cmp_submit",
                        type="button",
                        label="Submit",
                        binding_variable_id="var_submission_id",
                        required=False,
                        validators=[],
                    ),
                ],
                actions=[
                    Action(
                        action_id="action_submit",
                        type="submit_form",
                        source_component_id="cmp_submit",
                        integration_id="integration_submit",
                        success_transition_id="transition_success",
                    )
                ],
            ),
            Screen(screen_id="screen_success", kind="success", title="Success", components=[], actions=[]),
        ],
        transitions=[
            Transition(
                transition_id="transition_success",
                from_screen_id="screen_form",
                to_screen_id="screen_success",
                trigger="submit_success",
            )
        ],
        integrations=[
            Integration(
                integration_id="integration_submit",
                name="Submit",
                type="rest",
                method="POST",
                path="/api/submissions",
                request_schema=[],
                response_schema=[],
                auth_type="telegram_initdata",
            )
        ],
        storage_bindings=[
            StorageBinding(
                binding_id="binding_1",
                entity_id="entity_1",
                storage_type="postgres",
                table_or_collection="submissions",
            )
        ],
        auth_model=AuthModel(mode="telegram_session", telegram_initdata_validation_required=True),
        permissions=[Permission(permission_id="perm_1", name="submit", description="submit form")],
        security=SecurityPolicy(
            trusted_sources=["validated_init_data"],
            untrusted_sources=["user_input"],
            secret_handling="server_env_only",
            pii_variables=[],
        ),
        telemetry_hooks=[TelemetryHook(event_name="form_submit", trigger_type="form_submit", action_id="action_submit")],
        assumptions=[],
        open_questions=[],
        traceability=[
            TraceabilityLink(
                trace_id="trace_1",
                target_type="screen",
                target_id="screen_form",
                source_kind="doc_ref",
                source_ref="doc-1",
            )
        ],
    )


def test_contract_files_exist_and_expose_required_keys() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    spec_contract = json.loads((repo_root / "contracts" / "grounded-spec.v1.json").read_text(encoding="utf-8"))
    ir_contract = json.loads((repo_root / "contracts" / "app-ir.v1.json").read_text(encoding="utf-8"))

    assert "product_goal" in spec_contract["properties"]
    assert "user_flows" in spec_contract["properties"]
    assert "screens" in ir_contract["properties"]
    assert "traceability" in ir_contract["properties"]


def test_grounded_spec_validator_blocks_critical_contradictions() -> None:
    spec = make_valid_spec().model_copy(
        update={
            "contradictions": [
                Contradiction(
                    contradiction_id="c_1",
                    description="Conflict",
                    left_side="without backend",
                    right_side="save to database",
                    severity="critical",
                )
            ]
        }
    )
    result = GroundedSpecValidator().validate(spec)
    assert result.valid is False
    assert result.blocking is True
    assert any(issue.code == "spec.contradictions.critical" for issue in result.issues)


def test_app_ir_validator_blocks_missing_bindings() -> None:
    ir = make_valid_ir()
    ir.screens[0].components[0].binding_variable_id = "var_missing"
    result = AppIRValidator().validate(ir)
    assert result.valid is False
    assert result.blocking is True
    assert any(issue.code == "ir.binding_variable_id" for issue in result.issues)


def test_app_ir_validator_blocks_trusted_user_input() -> None:
    ir = make_valid_ir()
    ir.variables[0].trust_level = "trusted"
    result = AppIRValidator().validate(ir)
    assert result.valid is False
    assert any(issue.code == "ir.trusted_user_input" for issue in result.issues)

