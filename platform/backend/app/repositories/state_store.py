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
    _SHARDABLE_COLLECTIONS = {"jobs", "runs", "reports", "code_chunks", "background_tasks"}
    _HEAVY_REPORT_PREFIXES = (
        "run_artifacts:",
        "check_results:",
        "iterations:",
        "candidate_diff:",
        "trace:",
        "agent_activity:",
        "agent_diagnostics:",
        "agent_quality:",
        "patch:",
        "tool_result:",
        "tool_trace:",
        "large_tool_outputs:",
        "browser_proof:",
        "browser_replay_proof:",
        "browser_replay_scenario:",
        "acceptance_tests:",
        "compaction_summaries:",
        "acceptance_contract:",
        "implementation_plan:",
        "file_state_cache:",
        "turn_diff:",
        "environment_snapshot:",
        "tool_batch_summaries:",
        "worker_mailbox:",
        "worker_mailbox_v2:",
        "worker_mailbox_message:",
        "worker_sessions:",
        "worker_session:",
        "worker_turn:",
        "worker_ownership:",
        "draft_isolation:",
        "draft_gate:",
        "draft_apply_decision:",
        "draft_variant:",
        "guardian_gate:",
        "guardian_semantic_review:",
        "guardian_review_packet:",
        "scratchpad:",
        "agent_memory_store:",
        "memory_stage1:",
        "memory_pipeline:",
        "memory_consolidation:",
        "session_memory:",
        "simplify:",
        "worker_drafts:",
        "worker_merge:",
        "trace_bundle:",
        "trace_reducer:",
        "command_policy:",
        "verification_report:",
        "debug_run:",
        "stuck_run:",
        "doctor_workspace:",
        "rollout_trace:",
        "rollout_trace_evidence:",
        "exec_trace:",
        "process_outputs:",
        "lsp_context:",
        "lsp_symbol_index:",
        "lsp_references:",
        "lsp_route_graph:",
        "context_manager:",
        "context_manifest:",
        "context_pressure:",
        "run_compaction:",
        "run_compaction_boundary:",
        "run_compaction_boundaries:",
        "microcompact:",
        "microcompacts:",
        "post_compact_message:",
        "hook_trace:",
        "semantic_graph:",
        "worker_prefix:",
        "replay_trace:",
        "miniapp_contract:",
        "route_registry:",
        "contract_compile:",
        "repair_recipes:",
        "repair_case:",
        "repair_cases:",
        "project_instructions:",
        "slash_commands:",
        "slash_command:",
        "acceptance_scenarios:",
        "visual_qa:",
        "magic_doc:",
        "background_task_output:",
    )

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
            "background_tasks": {},
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
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), default=str)
        temp_path.replace(self.path)
        backup_path = self.path.with_suffix(f".{uuid.uuid4().hex}.bak.tmp")
        with backup_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), default=str)
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
            bucket = state.setdefault(collection, {})
            existing = bucket.get(key)
            prepared = self._prepare_collection_value(collection, key, value)
            bucket[key] = prepared
            if self._can_skip_index_write(collection, existing, prepared):
                return
            self._write(state)

    def upsert_many(self, collection: str, values: dict[str, dict[str, Any]]) -> None:
        if not values:
            return
        with self.lock, self._interprocess_lock():
            state = self._read()
            bucket = state.setdefault(collection, {})
            needs_index_write = False
            for key, value in values.items():
                existing = bucket.get(key)
                prepared = self._prepare_collection_value(collection, key, value)
                bucket[key] = prepared
                if not self._can_skip_index_write(collection, existing, prepared):
                    needs_index_write = True
            if needs_index_write:
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
            counters = {"jobs_sharded": 0, "runs_sharded": 0, "reports_sharded": 0, "code_chunks_sharded": 0}
            changed = False
            for collection in ("jobs", "runs", "reports", "code_chunks"):
                bucket = state.setdefault(collection, {})
                for key, value in list(bucket.items()):
                    if not isinstance(value, dict):
                        continue
                    prepared = self._prepare_collection_value(collection, key, value)
                    if prepared != value:
                        bucket[key] = prepared
                        counter_key = {
                            "jobs": "jobs_sharded",
                            "runs": "runs_sharded",
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
            prepared = self._prepare_runtime_index_value(collection, key, self._prepare_job_value(key, payload))
            if self._should_shard_payload(collection, key, prepared):
                return self._write_payload_shard(collection, key, prepared)
            return prepared
        if collection == "runs":
            prepared = self._prepare_runtime_index_value(collection, key, payload)
            if self._should_shard_payload(collection, key, prepared):
                return self._write_payload_shard(collection, key, prepared)
            return prepared
        if collection in self._SHARDABLE_COLLECTIONS and self._should_shard_payload(collection, key, payload):
            return self._write_payload_shard(collection, key, payload)
        return payload

    def _prepare_runtime_index_value(self, collection: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        compact = dict(payload or {})
        if compact.get("agent_transcript_ref") and isinstance(compact.get("agent_turns"), list):
            compact["agent_turns"] = self._tail_index_list(compact.get("agent_turns"), limit=3)
        if compact.get("memory_ref") and isinstance(compact.get("agent_memory"), dict):
            compact["agent_memory"] = self._compact_index_mapping(compact.get("agent_memory") or {}, max_items=8, max_value_chars=700)
        if isinstance(compact.get("compaction_summaries"), list):
            compact["compaction_summaries"] = self._tail_index_list(compact.get("compaction_summaries"), limit=6)
        if isinstance(compact.get("repair_iterations"), list):
            compact["repair_iterations"] = self._tail_index_list(compact.get("repair_iterations"), limit=8)
        if isinstance(compact.get("agent_activity_events"), list):
            compact["agent_activity_events"] = self._tail_index_list(compact.get("agent_activity_events"), limit=40)
        if collection == "jobs" and isinstance(compact.get("executed_checks"), list):
            compact["executed_checks"] = self._tail_index_list(compact.get("executed_checks"), limit=20)
        return compact

    def _tail_index_list(self, raw_items: Any, *, limit: int) -> list[Any]:
        items = list(raw_items or []) if isinstance(raw_items, list) else []
        return [self._compact_index_value(item) for item in items[-limit:]]

    def _compact_index_mapping(self, raw: dict[str, Any], *, max_items: int, max_value_chars: int) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for key, value in list(raw.items())[:max_items]:
            compact[str(key)] = self._compact_index_value(value, max_chars=max_value_chars)
        if len(raw) > max_items:
            compact["_omitted_keys"] = len(raw) - max_items
        return compact

    def _compact_index_value(self, value: Any, *, max_chars: int = 1200) -> Any:
        if isinstance(value, dict):
            return self._compact_index_mapping(value, max_items=10, max_value_chars=max_chars)
        if isinstance(value, list):
            return [self._compact_index_value(item, max_chars=max_chars) for item in value[:12]]
        if isinstance(value, str) and len(value) > max_chars:
            return value[:max_chars] + f"... [truncated {len(value) - max_chars} chars]"
        return value

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
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), default=str)
        temp_path.replace(path)
        return self._compact_shard_ref(collection, key, payload, path)

    def _can_skip_index_write(self, collection: str, existing: Any, prepared: dict[str, Any]) -> bool:
        """Avoid rewriting the main state index for stable shard updates.

        Report/tool artifacts are append-heavy and can be much larger than the run
        index. Their shard path is deterministic by collection/key, so once the
        index already points at that shard, later payload updates only need to
        replace the shard file. This keeps the agent loop from blocking on a full
        platform-state rewrite after every tool result.
        """

        if collection not in self._SHARDABLE_COLLECTIONS:
            return False
        if not isinstance(existing, dict) or not self._is_shard_ref(existing) or not self._is_shard_ref(prepared):
            return False
        if existing.get("storage_ref") != prepared.get("storage_ref"):
            return False
        stable_fields = (
            "collection",
            "key",
            "workspace_id",
            "run_id",
            "job_id",
            "report_type",
            "path",
            "language",
            "kind",
            "start_line",
            "end_line",
            "chunk_hash",
            "summary",
        )
        for field in stable_fields:
            if existing.get(field) != prepared.get(field):
                return False
        return True

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
            "linked_run_id",
            "run_id",
            "job_id",
            "linked_job_id",
            "status",
            "apply_status",
            "current_stage",
            "progress_percent",
            "generation_mode",
            "mode",
            "intent",
            "llm_model",
            "token_usage",
            "failure_reason",
            "created_at",
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
