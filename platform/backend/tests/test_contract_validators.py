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
    RoleActionGroup,
    RoleRouteGroup,
    RouteDefinition,
    Screen,
    ScreenDataSource,
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
from app.modules.miniapp_validation.build_validator import BuildValidator
from app.validators.connectivity_validator import ConnectivityValidator
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
                storage_type="sqlite",
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
        route_groups=[
            RoleRouteGroup(
                role="client",
                entry_path="/",
                routes=[
                    RouteDefinition(
                        route_id="route_client_form",
                        role="client",
                        path="/",
                        screen_id="screen_form",
                        is_entry=True,
                    ),
                    RouteDefinition(
                        route_id="route_client_success",
                        role="client",
                        path="/success",
                        screen_id="screen_success",
                    ),
                ],
            ),
            RoleRouteGroup(
                role="specialist",
                entry_path="/",
                routes=[
                    RouteDefinition(
                        route_id="route_specialist_form",
                        role="specialist",
                        path="/",
                        screen_id="screen_form",
                        is_entry=True,
                    )
                ],
            ),
            RoleRouteGroup(
                role="manager",
                entry_path="/",
                routes=[
                    RouteDefinition(
                        route_id="route_manager_form",
                        role="manager",
                        path="/",
                        screen_id="screen_form",
                        is_entry=True,
                    )
                ],
            ),
        ],
        screen_data_sources=[
            ScreenDataSource(
                source_id="source_form",
                screen_id="screen_form",
                kind="form",
                state_key="forms.form",
                role="client",
            )
        ],
        role_action_groups=[
            RoleActionGroup(role="client", action_ids=["action_submit"]),
            RoleActionGroup(role="specialist", action_ids=["action_submit"]),
            RoleActionGroup(role="manager", action_ids=["action_submit"]),
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
                storage_type="sqlite",
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


def _write_workspace_file(workspace_root: Path, relative_path: str, content: str) -> None:
    destination = workspace_root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")


def _create_workspace_scaffold(workspace_root: Path) -> None:
    _write_workspace_file(workspace_root, "miniapp/app/main.py", "app = object()\n")
    _write_workspace_file(workspace_root, "miniapp/requirements.txt", "fastapi\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/client/index.html", "<link rel=\"stylesheet\" href=\"/static/client/styles.css\" /><script src=\"/static/client/app.js\"></script><main>client</main>\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/client/styles.css", "body { color: #111; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/client/app.js", "console.log('client index');\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/client/profile/index.html", "<link rel=\"stylesheet\" href=\"/static/client/profile/styles.css\" /><script src=\"/static/client/profile/app.js\"></script><main>client profile</main>\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/client/profile/styles.css", "body { color: #222; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/client/profile/app.js", "console.log('client profile');\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/index.html", "<link rel=\"stylesheet\" href=\"/static/specialist/styles.css\" /><script src=\"/static/specialist/app.js\"></script><main>specialist</main>\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/styles.css", "body { color: #333; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/app.js", "console.log('specialist index');\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/profile/index.html", "<link rel=\"stylesheet\" href=\"/static/specialist/profile/styles.css\" /><script src=\"/static/specialist/profile/app.js\"></script><main>specialist profile</main>\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/profile/styles.css", "body { color: #444; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/profile/app.js", "console.log('specialist profile');\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/index.html", "<link rel=\"stylesheet\" href=\"/static/manager/styles.css\" /><script src=\"/static/manager/app.js\"></script><main>manager</main>\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/styles.css", "body { color: #555; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/app.js", "console.log('manager index');\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/profile/index.html", "<link rel=\"stylesheet\" href=\"/static/manager/profile/styles.css\" /><script src=\"/static/manager/profile/app.js\"></script><main>manager profile</main>\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/profile/styles.css", "body { color: #666; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/profile/app.js", "console.log('manager profile');\n")
    _write_workspace_file(workspace_root, "docker/docker-compose.yml", "services: {}\n")
    _write_workspace_file(workspace_root, "artifacts/grounded_spec.json", "{}\n")


def _multi_page_graph() -> dict:
    return {
        "flow_mode": "multi_page",
        "roles": {
            "client": {
                "routes_file": "miniapp/app/static/client/index.html",
                "pages": [
                    {"route_path": "/client", "file_path": "miniapp/app/static/client/index.html", "style_path": "miniapp/app/static/client/styles.css", "script_path": "miniapp/app/static/client/app.js"},
                    {"route_path": "/client/profile", "file_path": "miniapp/app/static/client/profile/index.html", "style_path": "miniapp/app/static/client/profile/styles.css", "script_path": "miniapp/app/static/client/profile/app.js"},
                ],
            },
            "specialist": {
                "routes_file": "miniapp/app/static/specialist/index.html",
                "pages": [
                    {"route_path": "/specialist", "file_path": "miniapp/app/static/specialist/index.html", "style_path": "miniapp/app/static/specialist/styles.css", "script_path": "miniapp/app/static/specialist/app.js"},
                    {"route_path": "/specialist/profile", "file_path": "miniapp/app/static/specialist/profile/index.html", "style_path": "miniapp/app/static/specialist/profile/styles.css", "script_path": "miniapp/app/static/specialist/profile/app.js"},
                ],
            },
            "manager": {
                "routes_file": "miniapp/app/static/manager/index.html",
                "pages": [
                    {"route_path": "/manager", "file_path": "miniapp/app/static/manager/index.html", "style_path": "miniapp/app/static/manager/styles.css", "script_path": "miniapp/app/static/manager/app.js"},
                    {"route_path": "/manager/profile", "file_path": "miniapp/app/static/manager/profile/index.html", "style_path": "miniapp/app/static/manager/profile/styles.css", "script_path": "miniapp/app/static/manager/profile/app.js"},
                ],
            },
        },
    }


def _write_connectivity_artifacts(workspace_root: Path, *, api_path: str = "/api/orders") -> None:
    graph = {
        "flow_mode": "multi_page",
        "roles": {
            "client": {
                "routes_file": "miniapp/app/static/client/index.html",
                "pages": [
                    {
                        "route_path": "/client",
                        "file_path": "miniapp/app/static/client/index.html",
                        "style_path": "miniapp/app/static/client/styles.css",
                        "script_path": "miniapp/app/static/client/app.js",
                        "title": "Shop",
                        "description": "Browse live orders",
                        "data_dependencies": ["orders"],
                        "loading_state": "Loading orders...",
                        "error_state": "Unable to load orders.",
                    }
                ],
            }
        },
    }
    spec = {
        "api_requirements": [
            {
                "api_req_id": "api_1",
                "name": "List orders",
                "method": "GET",
                "path": api_path,
                "purpose": "Load customer orders",
            }
        ]
    }
    _write_workspace_file(workspace_root, "artifacts/generated_app_graph.json", json.dumps(graph))
    _write_workspace_file(workspace_root, "artifacts/grounded_spec.json", json.dumps(spec))
    _write_workspace_file(workspace_root, "miniapp/app/static/client/styles.css", "body { color: #111; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/client/app.js", "console.log('client bootstrap');\n")
    _write_workspace_file(workspace_root, "miniapp/app/routes/__init__.py", "")


def test_build_validator_accepts_nested_page_asset_links(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _write_workspace_file(workspace_root, "miniapp/app/main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    _write_workspace_file(workspace_root, "miniapp/requirements.txt", "fastapi\n")
    _write_workspace_file(workspace_root, "docker/docker-compose.yml", "services: {}\n")
    _write_workspace_file(
        workspace_root,
        "artifacts/generated_app_graph.json",
        json.dumps(
            {
                "flow_mode": "multi_page",
                "roles": {
                    "client": {
                        "pages": [
                            {
                                "route_path": "/client/request_new",
                                "file_path": "miniapp/app/static/client/request_new/index.html",
                                "style_path": "miniapp/app/static/client/request_new/styles.css",
                                "script_path": "miniapp/app/static/client/request_new/app.js",
                                "title": "New request",
                            },
                            {
                                "route_path": "/client/profile",
                                "file_path": "miniapp/app/static/client/profile/index.html",
                                "style_path": "miniapp/app/static/client/profile/styles.css",
                                "script_path": "miniapp/app/static/client/profile/app.js",
                                "title": "Profile",
                                "page_kind": "profile",
                            },
                        ]
                    },
                    "specialist": {
                        "pages": [
                            {
                                "route_path": "/specialist",
                                "file_path": "miniapp/app/static/specialist/index.html",
                                "style_path": "miniapp/app/static/specialist/styles.css",
                                "script_path": "miniapp/app/static/specialist/app.js",
                                "title": "Desk",
                                "is_entry": True,
                            },
                            {
                                "route_path": "/specialist/profile",
                                "file_path": "miniapp/app/static/specialist/profile/index.html",
                                "style_path": "miniapp/app/static/specialist/profile/styles.css",
                                "script_path": "miniapp/app/static/specialist/profile/app.js",
                                "title": "Profile",
                                "page_kind": "profile",
                            },
                        ]
                    },
                    "manager": {
                        "pages": [
                            {
                                "route_path": "/manager",
                                "file_path": "miniapp/app/static/manager/index.html",
                                "style_path": "miniapp/app/static/manager/styles.css",
                                "script_path": "miniapp/app/static/manager/app.js",
                                "title": "Overview",
                                "is_entry": True,
                            },
                            {
                                "route_path": "/manager/profile",
                                "file_path": "miniapp/app/static/manager/profile/index.html",
                                "style_path": "miniapp/app/static/manager/profile/styles.css",
                                "script_path": "miniapp/app/static/manager/profile/app.js",
                                "title": "Profile",
                                "page_kind": "profile",
                            },
                        ]
                    },
                },
            }
        ),
    )
    _write_workspace_file(workspace_root, "artifacts/grounded_spec.json", json.dumps({"api_requirements": [], "persistence_requirements": []}))
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/request_new/index.html",
        '<link rel="stylesheet" href="/static/shared/base.css" /><link rel="stylesheet" href="/static/client/request_new/styles.css" /><script src="/static/client/request_new/app.js"></script><main id="request-form"></main>\n',
    )
    _write_workspace_file(workspace_root, "miniapp/app/static/client/request_new/styles.css", "body { color: #111; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/client/request_new/app.js", "document.getElementById('request-form');\n")
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/profile/index.html",
        '<link rel="stylesheet" href="/static/shared/base.css" /><link rel="stylesheet" href="/static/client/profile/styles.css" /><script src="/static/client/profile/app.js"></script><main></main>\n',
    )
    _write_workspace_file(workspace_root, "miniapp/app/static/client/profile/styles.css", "body { color: #222; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/client/profile/app.js", "console.log('profile');\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/index.html", '<link rel="stylesheet" href="/static/shared/base.css" /><link rel="stylesheet" href="/static/specialist/styles.css" /><script src="/static/specialist/app.js"></script><main></main>\n')
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/styles.css", "body { color: #333; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/app.js", "console.log('specialist');\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/profile/index.html", '<link rel="stylesheet" href="/static/shared/base.css" /><link rel="stylesheet" href="/static/specialist/profile/styles.css" /><script src="/static/specialist/profile/app.js"></script><main></main>\n')
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/profile/styles.css", "body { color: #444; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/profile/app.js", "console.log('specialist profile');\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/index.html", '<link rel="stylesheet" href="/static/shared/base.css" /><link rel="stylesheet" href="/static/manager/styles.css" /><script src="/static/manager/app.js"></script><main></main>\n')
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/styles.css", "body { color: #555; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/app.js", "console.log('manager');\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/profile/index.html", '<link rel="stylesheet" href="/static/shared/base.css" /><link rel="stylesheet" href="/static/manager/profile/styles.css" /><script src="/static/manager/profile/app.js"></script><main></main>\n')
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/profile/styles.css", "body { color: #666; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/profile/app.js", "console.log('manager profile');\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/shared/base.css", "body { margin: 0; }\n")
    _write_workspace_file(
        workspace_root,
        "miniapp/app/db.py",
        "from sqlalchemy import create_engine\nfrom sqlalchemy.orm import DeclarativeBase, sessionmaker\nengine = create_engine('sqlite:///test.db')\nSessionLocal = sessionmaker(bind=engine)\nclass Base(DeclarativeBase):\n    pass\n",
    )
    _write_workspace_file(workspace_root, "miniapp/app/schemas.py", "from pydantic import BaseModel\nclass Placeholder(BaseModel):\n    value: str\n")

    issues = BuildValidator().validate(workspace_root)

    issue_codes = {issue.code for issue in issues}
    assert "build.page_missing_style_link" not in issue_codes
    assert "build.page_missing_script_link" not in issue_codes


def test_build_validator_reports_invalid_generated_page_entries_instead_of_crashing(tmp_path: Path) -> None:
    workspace_path = tmp_path / "workspace"
    (workspace_path / "miniapp" / "app").mkdir(parents=True)
    (workspace_path / "miniapp" / "app" / "generated").mkdir(parents=True)
    (workspace_path / "docker").mkdir(parents=True)
    (workspace_path / "artifacts").mkdir(parents=True)

    (workspace_path / "miniapp" / "app" / "main.py").write_text("app = None\n", encoding="utf-8")
    (workspace_path / "miniapp" / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (workspace_path / "docker" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (workspace_path / "artifacts" / "grounded_spec.json").write_text(json.dumps(make_valid_spec().model_dump(mode="json")), encoding="utf-8")
    (workspace_path / "artifacts" / "generated_app_graph.json").write_text(
        json.dumps(
            {
                "flow_mode": "multi_page",
                "roles": {
                    "client": {
                        "routes_file": "miniapp/app/routes/client.py",
                        "pages": ["client_index"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (workspace_path / "miniapp" / "app" / "generated" / "route_manifest.json").write_text(json.dumps({"routes": []}), encoding="utf-8")
    (workspace_path / "miniapp" / "app" / "generated" / "runtime_manifest.json").write_text(json.dumps({"pages": []}), encoding="utf-8")

    issues = BuildValidator().validate(workspace_path)

    assert "build.invalid_generated_page_entry" in {issue.code for issue in issues}


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
                    left_side="without miniapp",
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


def test_build_validator_accepts_distinct_multi_page_role_graph(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    graph = _multi_page_graph()
    for role_payload in graph["roles"].values():
        pages = role_payload.get("pages") or []
        if len(pages) > 1:
            pages[1]["page_kind"] = "profile"
    _write_workspace_file(workspace_root, "artifacts/generated_app_graph.json", json.dumps(graph))

    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/index.html",
        "<main><section>book a new consultation</section></main>\n",
    )
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/profile.html",
        "<main><section>client profile editor</section></main>\n",
    )
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/specialist/index.html",
        "<main><section>process the live queue</section></main>\n",
    )
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/profile.html", "<main><section>specialist profile</section></main>\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/index.html", "<main><section>supervise operational health</section></main>\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/profile.html", "<main><section>manager profile</section></main>\n")

    issues = BuildValidator().validate(workspace_root)
    issue_codes = {issue.code for issue in issues}
    assert "build.placeholder_role_surface" not in issue_codes
    assert "build.placeholder_page" not in issue_codes
    assert "build.identical_role_pages" not in issue_codes


def test_build_validator_flags_placeholder_role_pages_without_identical_page_rail(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    graph = _multi_page_graph()
    for role_payload in graph["roles"].values():
        pages = role_payload.get("pages") or []
        if len(pages) > 1:
            pages[1]["page_kind"] = "profile"
    _write_workspace_file(workspace_root, "artifacts/generated_app_graph.json", json.dumps(graph))

    placeholder_html = "<main>RoleCabinetHomePage</main>\n"
    _write_workspace_file(workspace_root, "miniapp/app/static/client/index.html", placeholder_html)
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/index.html", placeholder_html)
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/index.html", placeholder_html)

    _write_workspace_file(workspace_root, "miniapp/app/static/client/profile.html", "<main><section>catalog</section></main>\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/profile.html", "<main><section>queue</section></main>\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/profile.html", "<main><section>dashboard</section></main>\n")

    issues = BuildValidator().validate(workspace_root)
    issue_codes = {issue.code for issue in issues}
    assert "build.placeholder_page" in issue_codes
    assert "build.identical_role_pages" not in issue_codes


def test_build_validator_flags_loading_first_role_root_surface(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    graph = _multi_page_graph()
    for role_payload in graph["roles"].values():
        pages = role_payload.get("pages") or []
        if pages:
            pages[0]["data_dependencies"] = ["requests"]
    _write_workspace_file(workspace_root, "artifacts/generated_app_graph.json", json.dumps(graph))
    _write_workspace_file(workspace_root, "miniapp/app/generated/route_manifest.json", json.dumps(graph))
    _write_workspace_file(workspace_root, "miniapp/app/generated/runtime_manifest.json", json.dumps({"roles": {}}))
    _write_workspace_file(workspace_root, "miniapp/app/static/shared/base.css", ".page-shell { padding-top: 76px; }\n")
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/index.html",
        '<html><head><link rel="stylesheet" href="/static/shared/base.css" /><link rel="stylesheet" href="/static/client/styles.css" /></head><body><main class="page-shell"><section>Loading your workspace...</section><div id="requests-panel"></div></main><script src="/static/preview_bridge.js" defer></script><script src="/static/client/app.js" defer></script></body></html>\n',
    )
    _write_workspace_file(workspace_root, "miniapp/app/static/client/styles.css", ".page-shell { padding-top: 76px; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/client/app.js", "console.log('client');\n")

    issues = BuildValidator().validate(workspace_root)
    issue_codes = {issue.code for issue in issues}

    assert "build.loading_first_root_surface" in issue_codes


def test_build_validator_accepts_content_first_root_surface_without_pseudo_data(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    graph = _multi_page_graph()
    for role_payload in graph["roles"].values():
        pages = role_payload.get("pages") or []
        if pages:
            pages[0]["data_dependencies"] = ["requests"]
    _write_workspace_file(workspace_root, "artifacts/generated_app_graph.json", json.dumps(graph))
    _write_workspace_file(workspace_root, "miniapp/app/generated/route_manifest.json", json.dumps(graph))
    _write_workspace_file(workspace_root, "miniapp/app/generated/runtime_manifest.json", json.dumps({"roles": {}}))
    _write_workspace_file(workspace_root, "miniapp/app/static/shared/base.css", ".page-shell { padding-top: 76px; }\n")
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/index.html",
        '<html><head><link rel="stylesheet" href="/static/shared/base.css" /><link rel="stylesheet" href="/static/client/styles.css" /></head><body><main class="page-shell"><section class="summary-card"><h1>Requests</h1><p>Track approvals and returns.</p></section><section class="primary-actions"><a href="/client/create">Create request</a></section><section class="empty-state"><h2>No requests yet</h2><p>Create the first request to start the workflow.</p></section></main><script src="/static/preview_bridge.js" defer></script><script src="/static/client/app.js" defer></script></body></html>\n',
    )
    _write_workspace_file(workspace_root, "miniapp/app/static/client/styles.css", ".page-shell { padding-top: 76px; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/client/app.js", "console.log('client');\n")

    issues = BuildValidator().validate(workspace_root)
    issue_codes = {issue.code for issue in issues}

    assert "build.loading_first_root_surface" not in issue_codes


def test_build_validator_flags_route_self_import(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    _write_workspace_file(
        workspace_root,
        "miniapp/app/routes/time_slots.py",
        "from app.routes.time_slots import list_time_slots\n",
    )

    issues = BuildValidator().validate(workspace_root)

    assert any(issue.code == "build.route_self_import" for issue in issues)


def test_build_validator_flags_in_memory_route_store_for_workflow_app(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    _write_workspace_file(
        workspace_root,
        "artifacts/grounded_spec.json",
        json.dumps(
            {
                "target_platform": "telegram_mini_app",
                "preview_profile": "telegram_mock",
                "product_goal": "Persistent request workflow",
                "actors": [],
                "domain_entities": [{ "entity_id": "request", "name": "Request", "description": "", "attributes": [] }],
                "user_flows": [{ "flow_id": "flow_1", "name": "Flow", "goal": "Persist requests", "steps": [], "acceptance_criteria": [] }],
                "ui_requirements": [],
                "api_requirements": [{ "api_req_id": "api_1", "description": "List requests", "path": "/api/requests", "method": "GET" }],
                "persistence_requirements": [{ "persistence_req_id": "persist_1", "description": "Store requests", "storage_type": "sqlite", "entity_ids": ["request"] }],
                "integration_requirements": [],
                "security_requirements": [],
                "platform_constraints": [],
                "non_functional_requirements": [],
                "assumptions": [],
                "unknowns": [],
                "contradictions": [],
                "doc_refs": [],
                "metadata": {},
            }
        ),
    )
    _write_workspace_file(
        workspace_root,
        "miniapp/app/routes/requests.py",
        "REQUESTS = {}\n",
    )
    _write_workspace_file(workspace_root, "miniapp/app/db.py", "engine = object()\n")
    _write_workspace_file(workspace_root, "miniapp/app/schemas.py", "class RequestSchema: ...\n")

    issues = BuildValidator().validate(workspace_root)

    assert any(issue.code == "build.in_memory_route_store" for issue in issues)


def test_build_validator_flags_inline_route_schema_model_for_workflow_app(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    _write_workspace_file(
        workspace_root,
        "artifacts/grounded_spec.json",
        json.dumps(
            {
                "target_platform": "telegram_mini_app",
                "preview_profile": "telegram_mock",
                "product_goal": "Persistent request workflow",
                "actors": [],
                "domain_entities": [{ "entity_id": "request", "name": "Request", "description": "", "attributes": [] }],
                "user_flows": [{ "flow_id": "flow_1", "name": "Flow", "goal": "Persist requests", "steps": [], "acceptance_criteria": [] }],
                "ui_requirements": [],
                "api_requirements": [{ "api_req_id": "api_1", "description": "Create request", "path": "/api/requests", "method": "POST" }],
                "persistence_requirements": [{ "persistence_req_id": "persist_1", "description": "Store requests", "storage_type": "sqlite", "entity_ids": ["request"] }],
                "integration_requirements": [],
                "security_requirements": [],
                "platform_constraints": [],
                "non_functional_requirements": [],
                "assumptions": [],
                "unknowns": [],
                "contradictions": [],
                "doc_refs": [],
                "metadata": {},
            }
        ),
    )
    _write_workspace_file(
        workspace_root,
        "miniapp/app/routes/requests.py",
        "from pydantic import BaseModel\n\nclass RequestCreate(BaseModel):\n    title: str\n",
    )
    _write_workspace_file(workspace_root, "miniapp/app/db.py", "engine = object()\n")
    _write_workspace_file(workspace_root, "miniapp/app/schemas.py", "class RequestSchema: ...\n")

    issues = BuildValidator().validate(workspace_root)

    assert any(issue.code == "build.inline_route_schema_model" for issue in issues)


def test_build_validator_flags_profile_contract_db_drift(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    _write_workspace_file(
        workspace_root,
        "artifacts/grounded_spec.json",
        json.dumps(
            {
                "target_platform": "telegram_mini_app",
                "preview_profile": "telegram_mock",
                "product_goal": "Persistent request workflow",
                "actors": [],
                "domain_entities": [{"entity_id": "request", "name": "Request", "description": "", "attributes": []}],
                "user_flows": [{"flow_id": "flow_1", "name": "Flow", "goal": "Persist requests", "steps": [], "acceptance_criteria": []}],
                "ui_requirements": [],
                "api_requirements": [{"api_req_id": "api_1", "description": "List requests", "path": "/api/requests", "method": "GET"}],
                "persistence_requirements": [{"persistence_req_id": "persist_1", "description": "Store requests", "storage_type": "sqlite", "entity_ids": ["request"]}],
                "integration_requirements": [],
                "security_requirements": [],
                "platform_constraints": [],
                "non_functional_requirements": [],
                "assumptions": [],
                "unknowns": [],
                "contradictions": [],
                "doc_refs": [],
                "metadata": {},
            }
        ),
    )
    _write_workspace_file(
        workspace_root,
        "miniapp/app/db.py",
        "from sqlalchemy import create_engine\nfrom sqlalchemy.orm import DeclarativeBase, sessionmaker\nengine = create_engine('sqlite:///test.db')\nSessionLocal = sessionmaker(bind=engine)\nclass Base(DeclarativeBase):\n    pass\n",
    )
    _write_workspace_file(workspace_root, "miniapp/app/schemas.py", "from pydantic import BaseModel\nclass RequestSchema(BaseModel):\n    title: str\n")
    _write_workspace_file(
        workspace_root,
        "miniapp/app/routes/profiles.py",
        "from app.db import RoleProfileRecord, SessionLocal\n",
    )

    issues = BuildValidator().validate(workspace_root)

    assert any(issue.code == "build.profile_contract_db_drift" for issue in issues)


def test_build_validator_flags_unexpected_auth_reference(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/app.js",
        'fetch("/api/auth");\n',
    )

    issues = BuildValidator().validate(workspace_root)

    assert any(issue.code == "build.unexpected_auth_reference" for issue in issues)


def test_build_validator_flags_duplicate_runtime_route_owners(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    _write_workspace_file(
        workspace_root,
        "miniapp/app/routes/runtime.py",
        'from fastapi import APIRouter\nrouter = APIRouter(prefix="/api/runtime")\n@router.get("/{role}/manifest")\ndef runtime_manifest(role: str):\n    return {"role": role}\n',
    )
    _write_workspace_file(
        workspace_root,
        "miniapp/app/routes/workload.py",
        'from fastapi import APIRouter\nrouter = APIRouter(prefix="/api/runtime")\n@router.get("/{role}/manifest")\ndef workload_manifest(role: str):\n    return {"role": role}\n',
    )

    issues = BuildValidator().validate(workspace_root)

    assert any(issue.code == "build.duplicate_runtime_route_provider" for issue in issues)


def test_build_validator_flags_runtime_action_writes_and_seeded_artifacts(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/app.js",
        'fetch("/api/runtime/client/actions/approve_request", { method: "POST" });\n',
    )
    _write_workspace_file(
        workspace_root,
        "miniapp/app/generated/role_seed.json",
        json.dumps({"roles": {"client": {"profile": {"first_name": "Ivan", "last_name": "Ivanov"}}}}),
    )

    issues = BuildValidator().validate(workspace_root)

    assert any(issue.code == "build.runtime_action_write_contract" for issue in issues)
    assert any(issue.code == "build.seeded_generated_artifact" for issue in issues)


def test_build_validator_flags_placeholder_persistence_handlers(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    _write_workspace_file(
        workspace_root,
        "miniapp/app/routes/requests.py",
        "def get_request():\n    placeholder = {'id': 'sample'}\n    return placeholder\n",
    )
    _write_workspace_file(
        workspace_root,
        "miniapp/app/routes/assignments.py",
        "INSERT OR IGNORE INTO requests\n",
    )
    _write_workspace_file(
        workspace_root,
        "miniapp/app/routes/profiles.py",
        "DEFAULT_PROFILES = {'client': {'first_name': 'Ivan'}}\n",
    )

    issues = BuildValidator().validate(workspace_root)

    assert any(issue.code == "build.placeholder_request_read" for issue in issues)
    assert any(issue.code == "build.placeholder_assignment_write" for issue in issues)
    assert any(issue.code == "build.placeholder_profile_seed" for issue in issues)


def test_build_validator_flags_fake_persistence_form_without_api_write(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    graph = _multi_page_graph()
    _write_workspace_file(workspace_root, "artifacts/generated_app_graph.json", json.dumps(graph))
    _write_workspace_file(workspace_root, "miniapp/app/generated/route_manifest.json", json.dumps(graph))
    _write_workspace_file(workspace_root, "miniapp/app/generated/runtime_manifest.json", json.dumps({"roles": {}}))
    _write_workspace_file(workspace_root, "miniapp/app/static/shared/base.css", ".page-shell { padding-top: 76px; }\n")
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/index.html",
        '<html><head><link rel="stylesheet" href="/static/shared/base.css" /><link rel="stylesheet" href="/static/client/styles.css" /></head><body><main class="page-shell" style="padding-top: max(76px, calc(var(--telegram-top-safe-offset) + 12px));"><form id="request-form"><button type="submit">Create</button></form></main><script src="/static/preview_bridge.js" defer></script><script src="/static/client/app.js" defer></script></body></html>\n',
    )
    _write_workspace_file(workspace_root, "miniapp/app/static/client/styles.css", ".page-shell { padding-top: 76px; }\n")
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/app.js",
        'document.getElementById("request-form")?.addEventListener("submit", (event) => { event.preventDefault(); console.log("saved"); });\n',
    )

    issues = BuildValidator().validate(workspace_root)
    issue_codes = {issue.code for issue in issues}

    assert "build.fake_persistence_flow" in issue_codes


def test_build_validator_flags_hardcoded_live_list_without_api_read(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    graph = _multi_page_graph()
    _write_workspace_file(workspace_root, "artifacts/generated_app_graph.json", json.dumps(graph))
    _write_workspace_file(workspace_root, "miniapp/app/generated/route_manifest.json", json.dumps(graph))
    _write_workspace_file(workspace_root, "miniapp/app/generated/runtime_manifest.json", json.dumps({"roles": {}}))
    _write_workspace_file(workspace_root, "miniapp/app/static/shared/base.css", ".page-shell { padding-top: 76px; }\n")
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/index.html",
        '<html><head><link rel="stylesheet" href="/static/shared/base.css" /><link rel="stylesheet" href="/static/client/styles.css" /></head><body><main class="page-shell" style="padding-top: max(76px, calc(var(--telegram-top-safe-offset) + 12px));"><section id="request-list"></section></main><script src="/static/preview_bridge.js" defer></script><script src="/static/client/app.js" defer></script></body></html>\n',
    )
    _write_workspace_file(workspace_root, "miniapp/app/static/client/styles.css", ".page-shell { padding-top: 76px; }\n")
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/app.js",
        'const requests = [{ id: "req_1", title: "Hardcoded request", status: "open" }];\nconst list = document.getElementById("request-list"); if (list) { list.textContent = requests.map((item) => item.title).join(", "); }\n',
    )

    issues = BuildValidator().validate(workspace_root)
    issue_codes = {issue.code for issue in issues}

    assert "build.hardcoded_live_list" in issue_codes


def test_build_validator_accepts_profile_persistence_via_template_literal_api_contract(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    graph = _multi_page_graph()
    client_pages = graph["roles"]["client"]["pages"]
    client_pages[0]["route_path"] = "/profile"
    client_pages[0]["page_kind"] = "profile"
    client_pages[0]["file_path"] = "miniapp/app/static/client/profile/index.html"
    client_pages[0]["script_path"] = "miniapp/app/static/client/profile/app.js"
    client_pages[0]["style_path"] = "miniapp/app/static/client/profile/styles.css"
    _write_workspace_file(workspace_root, "artifacts/generated_app_graph.json", json.dumps(graph))
    _write_workspace_file(workspace_root, "miniapp/app/generated/route_manifest.json", json.dumps(graph))
    _write_workspace_file(workspace_root, "miniapp/app/generated/runtime_manifest.json", json.dumps({"roles": {}}))
    _write_workspace_file(workspace_root, "miniapp/app/static/shared/base.css", ".page-shell { padding-top: 76px; }\n")
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/profile/index.html",
        '<html><head><link rel="stylesheet" href="/static/shared/base.css" /><link rel="stylesheet" href="/static/client/profile/styles.css" /></head><body><main class="page-shell" style="padding-top: max(76px, calc(var(--telegram-top-safe-offset) + 12px));"><form id="profile-form"><button id="save-button" type="submit">Save</button></form></main><script src="/static/preview_bridge.js" defer></script><script src="/static/client/profile/app.js" defer></script></body></html>\n',
    )
    _write_workspace_file(workspace_root, "miniapp/app/static/client/profile/styles.css", ".page-shell { padding-top: 76px; }\n")
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/profile/app.js",
        "const role = 'client';\nfetch(`/api/profiles/${role}`).then((response) => response.json());\ndocument.getElementById('profile-form')?.addEventListener('submit', async (event) => { event.preventDefault(); await fetch(`/api/profiles/${role}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ first_name: 'Ada', last_name: 'Lovelace', email: 'ada@example.com', phone: '+70000000000', photo_url: null }) }); });\n",
    )

    issues = BuildValidator().validate(workspace_root)
    issue_codes = {issue.code for issue in issues}

    assert "build.fake_persistence_flow" not in issue_codes


def test_build_validator_flags_missing_shell_style_and_dom_contract_drift(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    graph = _multi_page_graph()
    for role_payload in graph["roles"].values():
        pages = role_payload.get("pages") or []
        if len(pages) > 1:
            pages[1]["page_kind"] = "profile"
    _write_workspace_file(workspace_root, "artifacts/generated_app_graph.json", json.dumps(graph))
    _write_workspace_file(workspace_root, "miniapp/app/generated/route_manifest.json", json.dumps({"roles": {}}))
    _write_workspace_file(workspace_root, "miniapp/app/generated/runtime_manifest.json", json.dumps({"roles": {}}))
    _write_workspace_file(workspace_root, "miniapp/app/static/client/index.html", "<main>client routes</main>\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/index.html", "<main>specialist routes</main>\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/index.html", "<main>manager routes</main>\n")
    _write_workspace_file(workspace_root, "miniapp/app/db.py", "from sqlalchemy import create_engine\nfrom sqlalchemy.orm import DeclarativeBase, sessionmaker\nengine = create_engine('sqlite:///test.db')\nSessionLocal = sessionmaker(bind=engine)\nclass Base(DeclarativeBase):\n    pass\n")
    _write_workspace_file(workspace_root, "miniapp/app/schemas.py", "from pydantic import BaseModel\nclass RequestSchema(BaseModel):\n    title: str\n")
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/index.html",
        '<html><body><main class="page-shell"><div id="request-list"></div></main><script src="/static/client/app.js"></script></body></html>\n',
    )
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/styles.css",
        ".page-shell { padding-top: 76px; }\n",
    )
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/app.js",
        'document.getElementById("request-list"); document.getElementById("request-state");\n',
    )
    _write_workspace_file(workspace_root, "miniapp/app/static/client/profile/index.html", '<html><head><link rel="stylesheet" href="/static/shared/base.css" /><link rel="stylesheet" href="/static/client/profile/styles.css" /></head><body><div id="profile-card"></div><script src="/static/client/profile/app.js"></script></body></html>\n')
    _write_workspace_file(workspace_root, "miniapp/app/static/client/profile/styles.css", ".profile { color: #000; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/client/profile/app.js", 'document.getElementById("profile-card");\n')
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/index.html", '<html><head><link rel="stylesheet" href="/static/shared/base.css" /><link rel="stylesheet" href="/static/specialist/styles.css" /></head><body><div id="task-list"></div><script src="/static/specialist/app.js"></script></body></html>\n')
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/styles.css", ".page-shell { padding-top: 76px; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/app.js", 'document.getElementById("task-list");\n')
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/profile/index.html", '<html><head><link rel="stylesheet" href="/static/shared/base.css" /><link rel="stylesheet" href="/static/specialist/profile/styles.css" /></head><body><div id="profile-card"></div><script src="/static/specialist/profile/app.js"></script></body></html>\n')
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/profile/styles.css", ".profile { color: #000; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/specialist/profile/app.js", 'document.getElementById("profile-card");\n')
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/index.html", '<html><head><link rel="stylesheet" href="/static/shared/base.css" /><link rel="stylesheet" href="/static/manager/styles.css" /></head><body><div id="request-list"></div><script src="/static/manager/app.js"></script></body></html>\n')
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/styles.css", ".page-shell { padding-top: 76px; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/app.js", 'document.getElementById("request-list");\n')
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/profile/index.html", '<html><head><link rel="stylesheet" href="/static/shared/base.css" /><link rel="stylesheet" href="/static/manager/profile/styles.css" /></head><body><div id="profile-card"></div><script src="/static/manager/profile/app.js"></script></body></html>\n')
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/profile/styles.css", ".profile { color: #000; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/manager/profile/app.js", 'document.getElementById("profile-card");\n')
    _write_workspace_file(workspace_root, "miniapp/app/static/shared/base.css", ".page-shell { padding-top: 76px; }\n")

    issues = BuildValidator().validate(workspace_root)
    issue_codes = {issue.code for issue in issues}

    assert "build.page_missing_shell_style_link" in issue_codes
    assert "build.page_script_dom_contract" in issue_codes


def test_connectivity_validator_flags_missing_backend_route_for_dynamic_page(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    _write_connectivity_artifacts(workspace_root)
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/index.html",
        """
        <main>
          <section>Loading orders...</section>
          <section>Unable to load orders.</section>
          <script src="/static/client/app.js"></script>
        </main>
        """,
    )

    issues = ConnectivityValidator().validate(workspace_root)
    assert any(issue.code == "connectivity.missing_backend_route" for issue in issues)


def test_connectivity_validator_reads_page_specific_script_dependencies(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    _write_connectivity_artifacts(workspace_root)
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/index.html",
        """
        <main>
          <section>Loading orders...</section>
          <section>Unable to load orders.</section>
          <script src="/static/client/orders.js"></script>
        </main>
        """,
    )
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/orders.js",
        """
        async function loadOrders() {
          const response = await fetch('/api/orders');
          return response.json();
        }
        """,
    )

    issues = ConnectivityValidator().validate(workspace_root)
    assert any(issue.code == "connectivity.missing_backend_route" for issue in issues)


def test_connectivity_validator_flags_missing_static_asset_reference(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    _write_workspace_file(workspace_root, "artifacts/generated_app_graph.json", json.dumps(_multi_page_graph()))
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/index.html",
        """
        <main>
          <section>Catalog</section>
          <script src="/static/client/cart.js"></script>
        </main>
        """,
    )

    issues = ConnectivityValidator().validate(workspace_root)
    assert any(issue.code == "connectivity.missing_static_asset" for issue in issues)


def test_connectivity_validator_flags_unwired_dynamic_page(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    _write_connectivity_artifacts(workspace_root)
    _write_workspace_file(workspace_root, "miniapp/app/routes/orders.py", "from fastapi import APIRouter\nrouter = APIRouter()\n")
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/index.html",
        """
        <main>
          <section>Loading orders...</section>
          <section>Unable to load orders.</section>
          <section>No items yet.</section>
        </main>
        """,
    )

    issues = ConnectivityValidator().validate(workspace_root)
    issue_codes = {issue.code for issue in issues}
    assert "connectivity.unwired_page_dependency" in issue_codes
    assert "connectivity.placeholder_dynamic_page" in issue_codes


def test_connectivity_validator_flags_missing_loading_and_error_states(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    _write_connectivity_artifacts(workspace_root)
    _write_workspace_file(workspace_root, "miniapp/app/routes/orders.py", "from fastapi import APIRouter\nrouter = APIRouter()\n")
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/index.html",
        """
        <main>
          <section id="orders-root"></section>
          <script src="/static/client/app.js"></script>
        </main>
        """,
    )
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/app.js",
        "async function loadOrders() { const response = await fetch('/api/orders'); return response.json(); }\n",
    )

    issues = ConnectivityValidator().validate(workspace_root)
    issue_codes = {issue.code for issue in issues}
    assert "connectivity.missing_ui_loading_state" in issue_codes
    assert "connectivity.missing_ui_error_state" in issue_codes


def test_connectivity_validator_accepts_semantic_loading_and_error_state_markers(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    _write_connectivity_artifacts(workspace_root)
    _write_workspace_file(workspace_root, "miniapp/app/routes/orders.py", "from fastapi import APIRouter\nrouter = APIRouter()\n")
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/index.html",
        """
        <main>
          <section id="orders-loading" data-ui-state="loading" hidden></section>
          <section id="orders-error" data-ui-state="error" hidden></section>
          <section id="orders-root"></section>
          <script src="/static/client/app.js"></script>
        </main>
        """,
    )
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/app.js",
        """
        async function loadOrders() {
          const loading = document.getElementById("orders-loading");
          const error = document.getElementById("orders-error");
          if (loading) loading.hidden = false;
          if (error) error.hidden = true;
          const response = await fetch('/api/orders');
          return response.json();
        }
        """,
    )

    issues = ConnectivityValidator().validate(workspace_root)
    issue_codes = {issue.code for issue in issues}

    assert "connectivity.missing_ui_loading_state" not in issue_codes
    assert "connectivity.missing_ui_error_state" not in issue_codes


def test_connectivity_validator_accepts_api_reference_with_matching_route(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    _write_connectivity_artifacts(workspace_root, api_path="/api/categories")
    _write_workspace_file(workspace_root, "miniapp/app/routes/categories.py", "from fastapi import APIRouter\nrouter = APIRouter()\n")
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/index.html",
        """
        <main>
          <section>Loading orders...</section>
          <section>Unable to load orders.</section>
          <script src="/static/client/app.js"></script>
        </main>
        """,
    )
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/app.js",
        """
        async function loadCategories() {
          const response = await fetch('/api/categories');
          return response.json();
        }
        """,
    )

    issues = ConnectivityValidator().validate(workspace_root)
    assert not any(issue.code == "connectivity.missing_backend_route" for issue in issues)


def test_connectivity_validator_does_not_infer_route_names_from_plain_english_dependencies(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    graph = {
        "flow_mode": "multi_page",
        "roles": {
            "client": {
                "routes_file": "miniapp/app/routes/client.py",
                "pages": [
                    {
                        "route_path": "/client/create",
                        "file_path": "miniapp/app/static/client/create/index.html",
                        "style_path": "miniapp/app/static/client/create/styles.css",
                        "script_path": "miniapp/app/static/client/create/app.js",
                        "title": "Create request",
                        "description": "Create request and assign specialist after manager review",
                        "data_dependencies": [
                            "Create request and assign specialist after manager review"
                        ],
                        "loading_state": "Loading request data...",
                        "error_state": "Unable to load request data.",
                    }
                ],
            }
        },
    }
    spec = {
        "api_requirements": [
            {
                "api_req_id": "api_1",
                "name": "Create request",
                "method": "POST",
                "path": "/api/requests",
                "purpose": "Create request and assign specialist after manager review",
            }
        ]
    }
    _write_workspace_file(workspace_root, "artifacts/generated_app_graph.json", json.dumps(graph))
    _write_workspace_file(workspace_root, "artifacts/grounded_spec.json", json.dumps(spec))
    _write_workspace_file(
        workspace_root,
        "miniapp/app/static/client/create/index.html",
        """
        <main>
          <section>Loading request data...</section>
          <section>Unable to load request data.</section>
        </main>
        """,
    )
    _write_workspace_file(workspace_root, "miniapp/app/static/client/create/styles.css", "body { color: #111; }\n")
    _write_workspace_file(workspace_root, "miniapp/app/static/client/create/app.js", "console.log('create');\n")
    _write_workspace_file(workspace_root, "miniapp/app/routes/__init__.py", "")

    issues = ConnectivityValidator().validate(workspace_root)
    missing_route_locations = {issue.location for issue in issues if issue.code == "connectivity.missing_backend_route"}
    assert "miniapp/app/routes/action.py" not in missing_route_locations
    assert "miniapp/app/routes/and.py" not in missing_route_locations


def test_build_validator_flags_invalid_route_import_root(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    _create_workspace_scaffold(workspace_root)
    _write_workspace_file(
        workspace_root,
        "miniapp/app/db.py",
        "from sqlalchemy import create_engine\nfrom sqlalchemy.orm import DeclarativeBase, sessionmaker\nengine = create_engine('sqlite:///test.db')\nSessionLocal = sessionmaker(bind=engine)\nclass Base(DeclarativeBase):\n    pass\n",
    )
    _write_workspace_file(workspace_root, "miniapp/app/schemas.py", "from pydantic import BaseModel\nclass Placeholder(BaseModel):\n    value: str\n")
    _write_workspace_file(
        workspace_root,
        "artifacts/grounded_spec.json",
        json.dumps({"api_requirements": [{"path": "/api/requests"}], "persistence_requirements": [{"entity_id": "request"}]}),
    )
    _write_workspace_file(
        workspace_root,
        "miniapp/app/routes/requests.py",
        "from miniapp.app import db, schemas\n",
    )

    issues = BuildValidator().validate(workspace_root)
    issue_codes = {issue.code for issue in issues}
    assert "build.invalid_route_import_root" in issue_codes
