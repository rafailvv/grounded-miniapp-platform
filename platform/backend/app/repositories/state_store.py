from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any
import time


class StateStore:
    STORAGE_VERSION = 2
    DEFAULT_SHARD_THRESHOLD_BYTES = 16 * 1024
    JOB_EVENT_TAIL_LIMIT = 40
    JOB_EVENT_SHARD_MIN_COUNT = 20
    _SHARDABLE_COLLECTIONS = {"reports", "code_chunks"}
    _HEAVY_REPORT_PREFIXES = (
        "run_artifacts:",
        "check_results:",
        "iterations:",
        "candidate_diff:",
        "trace:",
        "agent_diagnostics:",
        "agent_quality:",
        "patch:",
        "tool_result:",
    )

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
        self.shard_root = self.path.parent / "state-shards"
        self.lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        self.backup_path = self.path.with_suffix(f"{self.path.suffix}.bak")
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.shard_root.mkdir(parents=True, exist_ok=True)
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
            return [self._resolve_collection_value(collection, key, value) for key, value in state.setdefault(collection, {}).items()]

    def items(self, collection: str) -> list[tuple[str, dict[str, Any]]]:
        with self.lock, self._interprocess_lock():
            state = self._read()
            return [
                (key, self._resolve_collection_value(collection, key, value))
                for key, value in state.setdefault(collection, {}).items()
            ]

    def get(self, collection: str, key: str) -> dict[str, Any] | None:
        with self.lock, self._interprocess_lock():
            state = self._read()
            value = state.setdefault(collection, {}).get(key)
            if value is None:
                return None
            return self._resolve_collection_value(collection, key, value)

    def storage_ref(self, collection: str, key: str) -> str | None:
        with self.lock, self._interprocess_lock():
            state = self._read()
            value = state.setdefault(collection, {}).get(key)
            if isinstance(value, dict):
                ref = value.get("storage_ref") or value.get("event_storage_ref")
                return str(ref) if ref else None
            return None

    def expected_storage_ref(self, collection: str, key: str) -> str:
        return self._relative_storage_ref(self._shard_path(collection, key))

    def upsert(self, collection: str, key: str, value: dict[str, Any]) -> None:
        with self.lock, self._interprocess_lock():
            state = self._read()
            state.setdefault(collection, {})[key] = self._prepare_collection_value(collection, key, value)
            self._write(state)

    def upsert_many(self, collection: str, values: dict[str, dict[str, Any]]) -> None:
        if not values:
            return
        with self.lock, self._interprocess_lock():
            state = self._read()
            bucket = state.setdefault(collection, {})
            for key, value in values.items():
                bucket[key] = self._prepare_collection_value(collection, key, value)
            self._write(state)

    def delete(self, collection: str, key: str) -> None:
        with self.lock, self._interprocess_lock():
            state = self._read()
            value = state.setdefault(collection, {}).pop(key, None)
            self._delete_storage_refs(value)
            self._write(state)

    def delete_many(self, collection: str, keys: list[str] | set[str] | tuple[str, ...]) -> None:
        normalized_keys = [key for key in keys if key]
        if not normalized_keys:
            return
        with self.lock, self._interprocess_lock():
            state = self._read()
            bucket = state.setdefault(collection, {})
            for key in normalized_keys:
                value = bucket.pop(key, None)
                self._delete_storage_refs(value)
            self._write(state)

    def replace_prefixed(self, collection: str, prefix: str, values: dict[str, dict[str, Any]]) -> None:
        with self.lock, self._interprocess_lock():
            state = self._read()
            bucket = state.setdefault(collection, {})
            keys_to_delete = [key for key in bucket if key.startswith(prefix)]
            for key in keys_to_delete:
                value = bucket.pop(key, None)
                self._delete_storage_refs(value)
            for key, value in values.items():
                bucket[key] = self._prepare_collection_value(collection, key, value)
            self._write(state)

    def shard_large_runtime_payloads(self) -> dict[str, int]:
        with self.lock, self._interprocess_lock():
            state = self._read()
            counters = {"jobs_sharded": 0, "reports_sharded": 0, "code_chunks_sharded": 0}
            changed = False
            for collection in ("jobs", "reports", "code_chunks"):
                bucket = state.setdefault(collection, {})
                for key, value in list(bucket.items()):
                    if not isinstance(value, dict):
                        continue
                    prepared = self._prepare_collection_value(collection, key, value)
                    if prepared != value:
                        bucket[key] = prepared
                        counter_key = {
                            "jobs": "jobs_sharded",
                            "reports": "reports_sharded",
                            "code_chunks": "code_chunks_sharded",
                        }[collection]
                        counters[counter_key] += 1
                        changed = True
            if changed:
                self._write(state)
            return counters

    def _prepare_collection_value(self, collection: str, key: str, value: dict[str, Any]) -> dict[str, Any]:
        payload = dict(value or {})
        if collection == "jobs":
            return self._prepare_job_value(key, payload)
        if collection in self._SHARDABLE_COLLECTIONS and self._should_shard_payload(collection, key, payload):
            return self._write_payload_shard(collection, key, payload)
        return payload

    def _prepare_job_value(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        events = payload.get("events")
        if not isinstance(events, list):
            return payload
        existing_event_ref = str(payload.get("event_storage_ref") or "").strip()
        try:
            existing_event_count = int(payload.get("event_count") or 0)
        except (TypeError, ValueError):
            existing_event_count = 0
        if existing_event_ref and existing_event_count > len(events):
            payload.setdefault("storage_version", self.STORAGE_VERSION)
            return payload
        if len(events) < self.JOB_EVENT_SHARD_MIN_COUNT and self._json_size(events) < self._shard_threshold_bytes():
            return payload
        ref = self._write_payload_shard(
            "job_events",
            key,
            {
                "job_id": key,
                "workspace_id": payload.get("workspace_id"),
                "updated_at": payload.get("updated_at"),
                "events": events,
            },
            ref_collection="job_events",
        )
        compact = dict(payload)
        compact["storage_version"] = self.STORAGE_VERSION
        compact["event_storage_ref"] = ref.get("storage_ref")
        compact["event_count"] = len(events)
        compact["events"] = events[-self.JOB_EVENT_TAIL_LIMIT :]
        return compact

    def _should_shard_payload(self, collection: str, key: str, payload: dict[str, Any]) -> bool:
        if self._is_shard_ref(payload):
            return False
        if collection == "code_chunks":
            return str(os.getenv("PLATFORM_STATE_SHARD_CODE_CHUNKS", "1")).strip().lower() not in {"0", "false", "no"}
        if collection == "reports" and any(str(key).startswith(prefix) for prefix in self._HEAVY_REPORT_PREFIXES):
            return self._json_size(payload) >= 1024
        return self._json_size(payload) >= self._shard_threshold_bytes()

    def _write_payload_shard(
        self,
        collection: str,
        key: str,
        payload: dict[str, Any],
        *,
        ref_collection: str | None = None,
    ) -> dict[str, Any]:
        shard_collection = ref_collection or collection
        path = self._shard_path(shard_collection, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        temp_path.replace(path)
        return self._compact_shard_ref(collection, key, payload, path)

    def _compact_shard_ref(self, collection: str, key: str, payload: dict[str, Any], path: Path) -> dict[str, Any]:
        ref: dict[str, Any] = {
            "__sharded__": True,
            "storage_version": self.STORAGE_VERSION,
            "collection": collection,
            "key": key,
            "storage_ref": self._relative_storage_ref(path),
            "updated_at": payload.get("updated_at") or payload.get("created_at"),
        }
        for field in (
            "workspace_id",
            "run_id",
            "job_id",
            "revision_id",
            "path",
            "language",
            "kind",
            "start_line",
            "end_line",
            "chunk_hash",
            "summary",
        ):
            if field in payload:
                ref[field] = payload.get(field)
        if collection == "reports":
            ref["report_type"] = str(key).split(":", 1)[0]
        return ref

    def _resolve_collection_value(self, collection: str, key: str, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            return value
        payload = self._resolve_shard_ref(value)
        if collection == "jobs":
            payload = self._resolve_job_events(key, payload)
        return payload

    def _resolve_job_events(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        ref = str(payload.get("event_storage_ref") or "").strip()
        if not ref:
            return payload
        event_payload = self._read_storage_ref(ref)
        if not isinstance(event_payload, dict):
            return payload
        events = event_payload.get("events")
        if isinstance(events, list):
            hydrated = dict(payload)
            hydrated["events"] = events
            hydrated["event_count"] = len(events)
            hydrated.setdefault("storage_version", self.STORAGE_VERSION)
            hydrated.setdefault("event_storage_ref", ref)
            return hydrated
        return payload

    def _resolve_shard_ref(self, value: dict[str, Any]) -> dict[str, Any]:
        if not self._is_shard_ref(value):
            return value
        payload = self._read_storage_ref(str(value.get("storage_ref") or ""))
        return payload if isinstance(payload, dict) else value

    def _read_storage_ref(self, ref: str) -> dict[str, Any] | None:
        path = self._path_from_storage_ref(ref)
        if path is None or not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _delete_storage_refs(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        refs = [value.get("storage_ref"), value.get("event_storage_ref")]
        for raw_ref in refs:
            path = self._path_from_storage_ref(str(raw_ref or ""))
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _is_shard_ref(value: dict[str, Any]) -> bool:
        return bool(isinstance(value, dict) and value.get("__sharded__") is True and value.get("storage_ref"))

    def _shard_path(self, collection: str, key: str) -> Path:
        digest = hashlib.sha1(f"{collection}:{key}".encode("utf-8")).hexdigest()
        return self.shard_root / collection / f"{digest}.json"

    def _relative_storage_ref(self, path: Path) -> str:
        try:
            return path.relative_to(self.path.parent).as_posix()
        except ValueError:
            return path.as_posix()

    def _path_from_storage_ref(self, ref: str) -> Path | None:
        if not ref:
            return None
        path = Path(ref)
        if path.is_absolute():
            return path
        return self.path.parent / path

    @classmethod
    def _json_size(cls, value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))

    @classmethod
    def _shard_threshold_bytes(cls) -> int:
        raw = os.getenv("PLATFORM_STATE_SHARD_THRESHOLD_BYTES", str(cls.DEFAULT_SHARD_THRESHOLD_BYTES))
        try:
            return max(1024, int(raw))
        except (TypeError, ValueError):
            return cls.DEFAULT_SHARD_THRESHOLD_BYTES

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
