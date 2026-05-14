from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from app.models.common import GenerationMode


CoordinatorPhase = Literal[
    "planning",
    "reading",
    "searching",
    "editing",
    "checking",
    "browser_verifying",
    "repairing",
    "completed",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AgentTodoItem:
    task: str
    phase: CoordinatorPhase
    status: Literal["pending", "in_progress", "completed"] = "pending"
    updated_at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, str]:
        return {"task": self.task, "phase": self.phase, "status": self.status, "updated_at": self.updated_at}


class AgentCoordinator:
    """Run coordinator for phase progress, todo state, and worker decisions."""

    PHASES: tuple[CoordinatorPhase, ...] = (
        "planning",
        "reading",
        "searching",
        "editing",
        "checking",
        "browser_verifying",
        "repairing",
        "completed",
    )

    def __init__(self, *, run_id: str, generation_mode: GenerationMode, implementation_plan: dict[str, Any]) -> None:
        self.run_id = run_id
        self.generation_mode = generation_mode
        self.implementation_plan = dict(implementation_plan or {})
        self.todo = self._build_todo_plan(generation_mode)
        self.phase_events: list[dict[str, Any]] = []

    def _build_todo_plan(self, generation_mode: GenerationMode) -> list[AgentTodoItem]:
        base = [
            AgentTodoItem("Extract product roles, state, actions, API, tests, and mobile constraints", "planning"),
            AgentTodoItem("Inspect current draft files and semantic maps", "reading"),
            AgentTodoItem("Patch backend, role UI, styles, and generated checks", "editing"),
            AgentTodoItem("Run static, API, and generated validation", "checking"),
            AgentTodoItem("Run real browser and mobile workflow proof", "browser_verifying"),
            AgentTodoItem("Repair exact failing slice until strict proof passes", "repairing"),
        ]
        if generation_mode == GenerationMode.QUALITY:
            base.insert(-1, AgentTodoItem("Run post-green mobile design verification pass", "browser_verifying"))
        elif generation_mode == GenerationMode.BALANCED:
            base.insert(-1, AgentTodoItem("Run balanced polish and consistency verification", "browser_verifying"))
        base.append(AgentTodoItem("Finalize applied run and reports", "completed"))
        return base

    def start_phase(self, phase: CoordinatorPhase, summary: str = "") -> dict[str, Any]:
        for item in self.todo:
            if item.status == "in_progress" and item.phase != phase:
                item.status = "pending"
                item.updated_at = _now()
        for item in self.todo:
            if item.phase == phase and item.status == "pending":
                item.status = "in_progress"
                item.updated_at = _now()
                break
        event = {"phase": phase, "status": "started", "summary": summary, "created_at": _now()}
        self.phase_events.append(event)
        return event

    def verification_completed(self) -> bool:
        return any(item.phase == "browser_verifying" and item.status == "completed" for item in self.todo)

    def ready_to_finalize(self) -> bool:
        return self.verification_completed() and all(
            item.status == "completed" or item.phase in {"repairing", "completed"} for item in self.todo
        )

    def incomplete_required_todos(self) -> list[dict[str, str]]:
        return [
            item.as_dict()
            for item in self.todo
            if item.status != "completed" and item.phase not in {"repairing", "completed"}
        ]

    def complete_phase(self, phase: CoordinatorPhase, summary: str = "") -> dict[str, Any]:
        for item in self.todo:
            if item.phase == phase and item.status in {"pending", "in_progress"}:
                item.status = "completed"
                item.updated_at = _now()
        event = {"phase": phase, "status": "completed", "summary": summary, "created_at": _now()}
        self.phase_events.append(event)
        return event

    def worker_specs(self) -> list[dict[str, Any]]:
        if self.generation_mode == GenerationMode.FAST:
            return []
        specs = [
            {"worker_id": "backend_api_worker", "phase": "editing", "owner_scope": "backend API and shared persistence"},
            {"worker_id": "client_surface_worker", "phase": "editing", "owner_scope": "client role app"},
            {"worker_id": "specialist_surface_worker", "phase": "editing", "owner_scope": "specialist role app"},
            {"worker_id": "manager_surface_worker", "phase": "editing", "owner_scope": "manager role app"},
            {"worker_id": "test_verifier_worker", "phase": "checking", "owner_scope": "generated checks"},
        ]
        if self.generation_mode == GenerationMode.QUALITY:
            specs.append({"worker_id": "mobile_polish_worker", "phase": "browser_verifying", "owner_scope": "independent workflow proof and mobile polish"})
        return specs

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "generation_mode": str(getattr(self.generation_mode, "value", self.generation_mode)),
            "todo_plan": [item.as_dict() for item in self.todo],
            "phase_events": list(self.phase_events),
            "worker_specs": self.worker_specs(),
            "verification_completed": self.verification_completed(),
            "ready_to_finalize": self.ready_to_finalize(),
            "incomplete_required_todos": self.incomplete_required_todos(),
        }
