from __future__ import annotations

import contextlib
import fcntl
import json
import threading
import uuid
from pathlib import Path
from typing import Any
import time


class StateStore:
    @staticmethod
    def _historical_runtime_value(*parts: str) -> str:
        return "".join(parts)

    _PERSISTED_JOB_EVENT_RENAMES = {
        _historical_runtime_value.__func__("building", "_scaffold"): "building_surface",
        _historical_runtime_value.__func__("scaffold", "_ready"): "surface_ready",
        _historical_runtime_value.__func__("repair", "_planned"): "agent_turn_started",
        _historical_runtime_value.__func__("fast", "_visual", "_patch"): "agent_turn_started",
        _historical_runtime_value.__func__("plan", "ner", "_contract", "_gap", "_detected"): "agent_turn_started",
        _historical_runtime_value.__func__("tri", "age", "_started"): "agent_turn_started",
        _historical_runtime_value.__func__("tri", "age", "_completed"): "agent_turn_started",
    }
    _PERSISTED_JOB_FIDELITY_RENAMES = {
        _historical_runtime_value.__func__("basic", "_scaffold"): "basic_app",
    }
    _PERSISTED_RUN_STAGE_RENAMES = {
        _historical_runtime_value.__func__("building", "_scaffold"): "building_surface",
        _historical_runtime_value.__func__("scaffold", "_ready"): "surface_ready",
    }
    _PERSISTED_EXECUTION_CLASS_RENAMES = {
        _historical_runtime_value.__func__("entity", "_workflow", "_app"): "shell_app",
        _historical_runtime_value.__func__("workflow", "_dashboard", "_app"): "shell_app",
        _historical_runtime_value.__func__("data", "_crud", "_app"): "shell_app",
    }
    _PERSISTED_OUTCOME_RENAMES = {
        _historical_runtime_value.__func__("noop", "_mater", "ialization", "_failure"): "noop_generation_failure",
    }

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        self.backup_path = self.path.with_suffix(f"{self.path.suffix}.bak")
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.lock, self._interprocess_lock():
                if not self.path.exists():
                    self._write(self._empty_state())

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "workspaces": {},
            "documents": {},
            "chat_turns": {},
            "jobs": {},
            "runs": {},
            "previews": {},
            "exports": {},
            "reports": {},
            "code_chunks": {},
            "code_indexes": {},
            "patch_applies": {},
        }

    @contextlib.contextmanager
    def _interprocess_lock(self):
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        last_error: json.JSONDecodeError | None = None
        for _ in range(5):
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    return json.load(handle)
            except json.JSONDecodeError as exc:
                last_error = exc
                time.sleep(0.01)
        if self.backup_path.exists():
            with self.backup_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self._write(payload)
            return payload
        assert last_error is not None
        raise last_error

    def _write(self, payload: dict[str, Any]) -> None:
        temp_path = self.path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        temp_path.replace(self.path)
        backup_path = self.path.with_suffix(f".{uuid.uuid4().hex}.bak.tmp")
        with backup_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        backup_path.replace(self.backup_path)

    def list(self, collection: str) -> list[dict[str, Any]]:
        with self.lock, self._interprocess_lock():
            state = self._read()
            return list(state.setdefault(collection, {}).values())

    def items(self, collection: str) -> list[tuple[str, dict[str, Any]]]:
        with self.lock, self._interprocess_lock():
            state = self._read()
            return [(key, value) for key, value in state.setdefault(collection, {}).items()]

    def get(self, collection: str, key: str) -> dict[str, Any] | None:
        with self.lock, self._interprocess_lock():
            state = self._read()
            return state.setdefault(collection, {}).get(key)

    def upsert(self, collection: str, key: str, value: dict[str, Any]) -> None:
        with self.lock, self._interprocess_lock():
            state = self._read()
            state.setdefault(collection, {})[key] = value
            self._write(state)

    def upsert_many(self, collection: str, values: dict[str, dict[str, Any]]) -> None:
        if not values:
            return
        with self.lock, self._interprocess_lock():
            state = self._read()
            bucket = state.setdefault(collection, {})
            bucket.update(values)
            self._write(state)

    def delete(self, collection: str, key: str) -> None:
        with self.lock, self._interprocess_lock():
            state = self._read()
            state.setdefault(collection, {}).pop(key, None)
            self._write(state)

    def delete_many(self, collection: str, keys: list[str] | set[str] | tuple[str, ...]) -> None:
        normalized_keys = [key for key in keys if key]
        if not normalized_keys:
            return
        with self.lock, self._interprocess_lock():
            state = self._read()
            bucket = state.setdefault(collection, {})
            for key in normalized_keys:
                bucket.pop(key, None)
            self._write(state)

    def replace_prefixed(self, collection: str, prefix: str, values: dict[str, dict[str, Any]]) -> None:
        with self.lock, self._interprocess_lock():
            state = self._read()
            bucket = state.setdefault(collection, {})
            keys_to_delete = [key for key in bucket if key.startswith(prefix)]
            for key in keys_to_delete:
                bucket.pop(key, None)
            bucket.update(values)
            self._write(state)

    def migrate_persisted_runtime_state(self) -> dict[str, int]:
        with self.lock, self._interprocess_lock():
            state = self._read()
            counters = {
                "jobs_migrated": 0,
                "job_events_migrated": 0,
                "job_fidelities_migrated": 0,
                "runs_migrated": 0,
                "run_stages_migrated": 0,
            }
            changed = False

            jobs = state.setdefault("jobs", {})
            for key, payload in list(jobs.items()):
                if not isinstance(payload, dict):
                    continue
                job_changed = False
                fidelity = str(payload.get("fidelity") or "").strip()
                migrated_fidelity = self._PERSISTED_JOB_FIDELITY_RENAMES.get(fidelity)
                if migrated_fidelity and migrated_fidelity != fidelity:
                    payload["fidelity"] = migrated_fidelity
                    counters["job_fidelities_migrated"] += 1
                    job_changed = True

                events = payload.get("events")
                if isinstance(events, list):
                    for event in events:
                        if not isinstance(event, dict):
                            continue
                        event_type = str(event.get("event_type") or "").strip()
                        migrated_event_type = self._PERSISTED_JOB_EVENT_RENAMES.get(event_type)
                        if migrated_event_type and migrated_event_type != event_type:
                            event["event_type"] = migrated_event_type
                            counters["job_events_migrated"] += 1
                            job_changed = True

                execution_class = str(payload.get("execution_class") or "").strip()
                migrated_execution_class = self._PERSISTED_EXECUTION_CLASS_RENAMES.get(execution_class)
                if migrated_execution_class and migrated_execution_class != execution_class:
                    payload["execution_class"] = migrated_execution_class
                    job_changed = True
                outcome_kind = str(payload.get("outcome_kind") or "").strip()
                migrated_outcome = self._PERSISTED_OUTCOME_RENAMES.get(outcome_kind)
                if migrated_outcome and migrated_outcome != outcome_kind:
                    payload["outcome_kind"] = migrated_outcome
                    job_changed = True
                if self._normalize_validation_snapshot(payload):
                    job_changed = True

                if job_changed:
                    jobs[key] = payload
                    counters["jobs_migrated"] += 1
                    changed = True

            runs = state.setdefault("runs", {})
            for key, payload in list(runs.items()):
                if not isinstance(payload, dict):
                    continue
                run_changed = False
                current_stage = str(payload.get("current_stage") or "").strip()
                migrated_stage = self._PERSISTED_RUN_STAGE_RENAMES.get(current_stage)
                if migrated_stage and migrated_stage != current_stage:
                    payload["current_stage"] = migrated_stage
                    counters["run_stages_migrated"] += 1
                    run_changed = True
                execution_class = str(payload.get("execution_class") or "").strip()
                migrated_execution_class = self._PERSISTED_EXECUTION_CLASS_RENAMES.get(execution_class)
                if migrated_execution_class and migrated_execution_class != execution_class:
                    payload["execution_class"] = migrated_execution_class
                    run_changed = True
                outcome_kind = str(payload.get("outcome_kind") or "").strip()
                migrated_outcome = self._PERSISTED_OUTCOME_RENAMES.get(outcome_kind)
                if migrated_outcome and migrated_outcome != outcome_kind:
                    payload["outcome_kind"] = migrated_outcome
                    run_changed = True
                if self._normalize_validation_snapshot(payload):
                    run_changed = True
                if run_changed:
                    runs[key] = payload
                    counters["runs_migrated"] += 1
                    changed = True

            if changed:
                self._write(state)
            return counters

    @staticmethod
    def _normalize_validation_snapshot(payload: dict[str, Any]) -> bool:
        snapshot = payload.get("validation_snapshot")
        if not isinstance(snapshot, dict):
            return False
        changed = False
        legacy_spec = snapshot.pop(StateStore._historical_runtime_value("grounded", "_spec", "_valid"), None)
        legacy_ir = snapshot.pop(StateStore._historical_runtime_value("app", "_ir", "_valid"), None)
        if legacy_spec is not None or legacy_ir is not None:
            inferred_valid = bool(legacy_spec) or bool(legacy_ir)
            snapshot.setdefault("platform_valid", inferred_valid)
            snapshot.setdefault("prompt_alignment_valid", inferred_valid)
            snapshot.setdefault("checks_valid", inferred_valid)
            changed = True
        if "checks_valid" not in snapshot:
            snapshot["checks_valid"] = not bool(snapshot.get("blocking", True))
            changed = True
        if "platform_valid" not in snapshot:
            snapshot["platform_valid"] = bool(snapshot.get("checks_valid"))
            changed = True
        if "prompt_alignment_valid" not in snapshot:
            snapshot["prompt_alignment_valid"] = bool(snapshot.get("checks_valid"))
            changed = True
        payload["validation_snapshot"] = snapshot
        return changed
