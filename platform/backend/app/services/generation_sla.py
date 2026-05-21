from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.models.common import GenerationMode


@dataclass(frozen=True)
class GenerationSlaProfile:
    mode: str
    label: str
    objective: str
    required_checks: tuple[str, ...]
    optional_checks: tuple[str, ...] = field(default_factory=tuple)
    proof_requirements: tuple[str, ...] = field(default_factory=tuple)
    final_gate: tuple[str, ...] = field(default_factory=tuple)
    context_policy: str = "standard"
    worker_policy: str = "serial"
    max_repair_attempts: int = 1
    audit_level: str = "light"
    output_style: str = "concise"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("required_checks", "optional_checks", "proof_requirements", "final_gate"):
            payload[key] = list(payload[key])
        return payload


SECOND_QUEUE_CAPABILITIES: tuple[dict[str, Any], ...] = (
    {"id": "doctor", "title": "/doctor platform + workspace diagnostics", "priority": 1, "status": "partially_available", "endpoint": "/doctor"},
    {"id": "cost", "title": "/cost generation cost accounting", "priority": 1, "status": "partially_available", "endpoint": "/system/observability"},
    {"id": "structured_diff", "title": "/diff structured patch review", "priority": 1, "status": "tool_available", "tool": "inspect_diff"},
    {"id": "review", "title": "/review code and product review for generated apps", "priority": 1, "status": "available", "endpoint": "/runs/{run_id}/review"},
    {"id": "context_view", "title": "Context view: what the agent actually saw", "priority": 2, "status": "partially_available", "endpoint": "/runs/{run_id}/context-pressure"},
    {"id": "resume_thread_lifecycle", "title": "/resume and thread lifecycle", "priority": 2, "status": "available", "endpoint": "/runs/{run_id}/resume"},
    {"id": "rewind_rollback", "title": "/rewind and rollback applied draft", "priority": 2, "status": "available", "endpoint": "/runs/{run_id}/rollback"},
    {"id": "prompt_suggestions", "title": "Prompt suggestions and speculation", "priority": 2, "status": "available", "endpoint": "/runs/{run_id}/prompt-suggestions"},
    {"id": "output_styles", "title": "Output styles for final responses", "priority": 3, "status": "planned"},
    {"id": "command_palette", "title": "Workbench keybinding and command palette improvements", "priority": 3, "status": "planned"},
    {"id": "ide_bridge", "title": "PyCharm/WebStorm IDE bridge", "priority": 4, "status": "conditional"},
    {"id": "remote_sessions", "title": "Remote sessions for remote generation runtime", "priority": 4, "status": "conditional"},
)


SLA_PROFILES: dict[GenerationMode, GenerationSlaProfile] = {
    GenerationMode.BASIC: GenerationSlaProfile(
        mode="basic",
        label="Basic scaffold",
        objective="Small low-risk scaffold or exploratory edit with only static sanity checks.",
        required_checks=("changed_files_static",),
        optional_checks=("api_workflow_smoke",),
        proof_requirements=("static import/build sanity",),
        final_gate=("meaningful diff",),
        context_policy="tiny",
        worker_policy="serial",
        max_repair_attempts=1,
        audit_level="none",
        output_style="short",
    ),
    GenerationMode.FAST: GenerationSlaProfile(
        mode="fast",
        label="Fast happy path",
        objective="Ship one working happy path quickly with minimal but real product proof.",
        required_checks=("api_workflow_smoke", "browser_flow_smoke"),
        optional_checks=("changed_files_static", "generated_app_python_tests", "generated_app_js_tests"),
        proof_requirements=("one prompt-derived happy path", "persisted state marker", "single role/browser route proof"),
        final_gate=("meaningful product diff", "happy path browser proof"),
        context_policy="focused",
        worker_policy="serial",
        max_repair_attempts=2,
        audit_level="light",
        output_style="concise",
    ),
    GenerationMode.BALANCED: GenerationSlaProfile(
        mode="balanced",
        label="Balanced product proof",
        objective="Cover role flows, persistence, generated tests, and mobile usability.",
        required_checks=("api_workflow_smoke", "browser_flow_smoke", "generated_app_python_tests", "generated_app_js_tests"),
        optional_checks=("visual_regression", "prompt_completion_audit"),
        proof_requirements=("role coverage", "shared persistence", "mobile layout", "generated app tests"),
        final_gate=("requirement traceability", "prompt completion audit", "repair case sync"),
        context_policy="standard",
        worker_policy="serial with repair cases",
        max_repair_attempts=5,
        audit_level="standard",
        output_style="implementation_summary",
    ),
    GenerationMode.QUALITY: GenerationSlaProfile(
        mode="quality",
        label="Quality browser + visual proof",
        objective="Deep generated-app proof with browser scenarios, analytics, edge states, and visual snapshots.",
        required_checks=("api_workflow_smoke", "browser_flow_smoke", "generated_app_python_tests", "generated_app_js_tests"),
        optional_checks=("visual_regression", "observability", "guardian_review"),
        proof_requirements=("browser proof", "analytics/observability signals", "edge states", "visual snapshots", "mobile overflow/overlap scan"),
        final_gate=("requirement traceability", "prompt completion audit", "visual regression report", "final review gate"),
        context_policy="broad",
        worker_policy="parallel owned branches when contract is ready",
        max_repair_attempts=6,
        audit_level="deep",
        output_style="proof_first",
    ),
    GenerationMode.PRODUCTION: GenerationSlaProfile(
        mode="production",
        label="Production release gate",
        objective="Release-grade proof with security, export, docs, regression, and full audit evidence.",
        required_checks=("api_workflow_smoke", "browser_flow_smoke", "generated_app_python_tests", "generated_app_js_tests"),
        optional_checks=("visual_regression", "security_summary", "export_bundle", "documentation", "regression_suite", "observability"),
        proof_requirements=("full role regression", "security/privacy review", "exportable artifact", "operator docs", "visual regression", "full prompt audit"),
        final_gate=("security summary", "export proof", "docs proof", "regression proof", "prompt-to-artifact completion audit"),
        context_policy="release",
        worker_policy="parallel workers plus final release review",
        max_repair_attempts=8,
        audit_level="release",
        output_style="release_report",
    ),
}


def normalize_generation_mode(value: GenerationMode | str | None) -> GenerationMode:
    if isinstance(value, GenerationMode):
        return value
    text = str(value or "").strip()
    if text:
        try:
            return GenerationMode(text)
        except ValueError:
            pass
    return GenerationMode.BALANCED


class GenerationSla:
    @staticmethod
    def profile(mode: GenerationMode | str | None) -> GenerationSlaProfile:
        return SLA_PROFILES[normalize_generation_mode(mode)]

    @staticmethod
    def required_checks(mode: GenerationMode | str | None) -> tuple[str, ...]:
        return GenerationSla.profile(mode).required_checks

    @staticmethod
    def requires_full_audit(mode: GenerationMode | str | None) -> bool:
        return normalize_generation_mode(mode) in {GenerationMode.BALANCED, GenerationMode.QUALITY, GenerationMode.PRODUCTION}

    @staticmethod
    def requires_visual_snapshots(mode: GenerationMode | str | None) -> bool:
        return normalize_generation_mode(mode) in {GenerationMode.QUALITY, GenerationMode.PRODUCTION}

    @staticmethod
    def treats_as_quality(mode: GenerationMode | str | None) -> bool:
        return normalize_generation_mode(mode) in {GenerationMode.QUALITY, GenerationMode.PRODUCTION}

    @staticmethod
    def manifest() -> dict[str, Any]:
        return {
            "schema": "grounded.generation_sla.v1",
            "default_mode": GenerationMode.BALANCED.value,
            "modes": [SLA_PROFILES[mode].to_dict() for mode in (GenerationMode.FAST, GenerationMode.BALANCED, GenerationMode.QUALITY, GenerationMode.PRODUCTION, GenerationMode.BASIC)],
            "second_queue": list(SECOND_QUEUE_CAPABILITIES),
            "compatibility": {
                "basic": "kept for legacy low-proof scaffolds",
                "production": "additive mode; quality remains accepted and maps to deep proof",
            },
        }
