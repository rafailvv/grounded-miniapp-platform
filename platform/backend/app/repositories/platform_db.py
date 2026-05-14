from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.models.event_journal import EventPayloadRecord, RunEventV2, ThreadEventV2
from app.models.threads import ArtifactRecord, ItemRecord, RolloutEventRecord, ThreadRecord, TurnRecord


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PlatformDb:
    """SQLite-backed append-oriented store for thread/turn/item state."""

    CURRENT_SCHEMA_VERSION = 2

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _migrate(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    thread_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0,
                    forked_from_thread_id TEXT,
                    current_turn_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_threads_workspace_created ON threads(workspace_id, created_at, thread_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    turn_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    linked_run_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_thread_created ON turns(thread_id, created_at, turn_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS items (
                    item_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT,
                    item_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_items_thread_sequence ON items(thread_id, sequence, item_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_items_turn_sequence ON items(turn_id, sequence, item_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rollout_events (
                    event_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT,
                    event_type TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_thread_sequence ON rollout_events(thread_id, sequence, event_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    thread_id TEXT,
                    turn_id TEXT,
                    artifact_type TEXT NOT NULL,
                    storage_ref TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS thread_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_thread_created ON thread_snapshots(thread_id, created_at, snapshot_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS exec_processes (
                    process_id TEXT PRIMARY KEY,
                    thread_id TEXT,
                    turn_id TEXT,
                    command TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fs_watchers (
                    watch_id TEXT PRIMARY KEY,
                    thread_id TEXT,
                    workspace_id TEXT,
                    path TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_run_events_run_sequence ON run_events(run_id, sequence, event_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS event_payloads (
                    payload_ref TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_event_payloads_sha256 ON event_payloads(sha256)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_events_v2 (
                    event_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_ref TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    source_ref TEXT,
                    idempotency_key TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(payload_ref) REFERENCES event_payloads(payload_ref)
                )
                """
            )
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_run_events_v2_run_sequence ON run_events_v2(run_id, sequence)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_run_events_v2_workspace_created ON run_events_v2(workspace_id, created_at, event_id)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_run_events_v2_idempotency ON run_events_v2(run_id, idempotency_key) WHERE idempotency_key IS NOT NULL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS thread_events_v2 (
                    event_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT,
                    run_id TEXT,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_ref TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    source_ref TEXT,
                    idempotency_key TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(payload_ref) REFERENCES event_payloads(payload_ref)
                )
                """
            )
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_events_v2_thread_sequence ON thread_events_v2(thread_id, sequence)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_thread_events_v2_workspace_created ON thread_events_v2(workspace_id, created_at, event_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_thread_events_v2_run ON thread_events_v2(run_id, sequence) WHERE run_id IS NOT NULL")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_events_v2_idempotency ON thread_events_v2(thread_id, idempotency_key) WHERE idempotency_key IS NOT NULL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_state_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_run_state_snapshots_run_created ON run_state_snapshots(run_id, created_at, snapshot_id)")
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (self.CURRENT_SCHEMA_VERSION, _utc_iso()),
            )

    @staticmethod
    def _dump(record: Any) -> str:
        if hasattr(record, "model_dump"):
            payload = record.model_dump(mode="json")
        else:
            payload = dict(record or {})
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _dump_json(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _run_event_v2_from_row(row: sqlite3.Row) -> RunEventV2:
        return RunEventV2.model_validate(dict(row))

    @staticmethod
    def _thread_event_v2_from_row(row: sqlite3.Row) -> ThreadEventV2:
        return ThreadEventV2.model_validate(dict(row))

    @staticmethod
    def _loads(row: sqlite3.Row, model: type[ThreadRecord] | type[TurnRecord] | type[ItemRecord] | type[RolloutEventRecord] | type[ArtifactRecord]):
        return model.model_validate(json.loads(str(row["payload_json"])))

    def upsert_thread(self, thread: ThreadRecord) -> ThreadRecord:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO threads(thread_id, workspace_id, title, status, archived, forked_from_thread_id, current_turn_id, payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    title=excluded.title,
                    status=excluded.status,
                    archived=excluded.archived,
                    current_turn_id=excluded.current_turn_id,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    thread.thread_id,
                    thread.workspace_id,
                    thread.title,
                    thread.status,
                    1 if thread.archived else 0,
                    thread.forked_from_thread_id,
                    thread.current_turn_id,
                    self._dump(thread),
                    thread.created_at.isoformat(),
                    thread.updated_at.isoformat(),
                ),
            )
        return thread

    def get_thread(self, thread_id: str) -> ThreadRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT payload_json FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
            return self._loads(row, ThreadRecord) if row else None

    def list_threads(self, *, workspace_id: str | None = None, include_archived: bool = False, limit: int = 50, cursor: str | None = None) -> tuple[list[ThreadRecord], str | None]:
        limit = max(1, min(int(limit or 50), 200))
        args: list[Any] = []
        where = []
        if workspace_id:
            where.append("workspace_id = ?")
            args.append(workspace_id)
        if not include_archived:
            where.append("archived = 0")
        if cursor:
            created_at, thread_id = self._decode_cursor(cursor)
            where.append("(created_at, thread_id) > (?, ?)")
            args.extend([created_at, thread_id])
        sql = "SELECT * FROM threads"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at, thread_id LIMIT ?"
        args.append(limit + 1)
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = self._encode_cursor(rows[-1]["created_at"], rows[-1]["thread_id"])
        return [self._loads(row, ThreadRecord) for row in rows], next_cursor

    def insert_turn(self, turn: TurnRecord) -> TurnRecord:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO turns(turn_id, thread_id, workspace_id, kind, status, linked_run_id, payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id) DO UPDATE SET
                    status=excluded.status,
                    linked_run_id=excluded.linked_run_id,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    turn.turn_id,
                    turn.thread_id,
                    turn.workspace_id,
                    turn.kind,
                    turn.status,
                    turn.linked_run_id,
                    self._dump(turn),
                    turn.created_at.isoformat(),
                    turn.updated_at.isoformat(),
                ),
            )
        return turn

    def get_turn(self, turn_id: str) -> TurnRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT payload_json FROM turns WHERE turn_id = ?", (turn_id,)).fetchone()
            return self._loads(row, TurnRecord) if row else None

    def list_turns(self, thread_id: str, *, limit: int = 100) -> list[TurnRecord]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM turns WHERE thread_id = ? ORDER BY created_at, turn_id LIMIT ?",
                (thread_id, max(1, min(limit, 500))),
            ).fetchall()
        return [self._loads(row, TurnRecord) for row in rows]

    def append_item(self, item: ItemRecord) -> ItemRecord:
        item.sequence = self._next_sequence("items", item.thread_id)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO items(item_id, thread_id, turn_id, item_type, status, sequence, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item.item_id, item.thread_id, item.turn_id, item.item_type, item.status, item.sequence, self._dump(item), item.created_at.isoformat()),
            )
        return item

    def list_items(self, thread_id: str, *, turn_id: str | None = None, after_sequence: int = 0, limit: int = 200) -> list[ItemRecord]:
        args: list[Any] = [thread_id, int(after_sequence or 0)]
        sql = "SELECT * FROM items WHERE thread_id = ? AND sequence > ?"
        if turn_id:
            sql += " AND turn_id = ?"
            args.append(turn_id)
        sql += " ORDER BY sequence, item_id LIMIT ?"
        args.append(max(1, min(limit, 1000)))
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._loads(row, ItemRecord) for row in rows]

    def append_event(self, event: RolloutEventRecord) -> RolloutEventRecord:
        event.sequence = self._next_sequence("rollout_events", event.thread_id)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rollout_events(event_id, thread_id, turn_id, event_type, sequence, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (event.event_id, event.thread_id, event.turn_id, event.event_type, event.sequence, self._dump(event), event.created_at.isoformat()),
            )
        return event

    def list_events(self, thread_id: str, *, after_sequence: int = 0, limit: int = 200) -> list[RolloutEventRecord]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rollout_events WHERE thread_id = ? AND sequence > ? ORDER BY sequence, event_id LIMIT ?",
                (thread_id, int(after_sequence or 0), max(1, min(limit, 1000))),
            ).fetchall()
        return [self._loads(row, RolloutEventRecord) for row in rows]

    def append_run_event_v2(
        self,
        *,
        workspace_id: str,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        actor: str = "system",
        summary: str = "",
        source_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> RunEventV2:
        created_at = _utc_iso()
        with self._lock, self._connect() as conn:
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM run_events_v2 WHERE run_id = ? AND idempotency_key = ?",
                    (run_id, idempotency_key),
                ).fetchone()
                if existing:
                    return self._run_event_v2_from_row(existing)
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM run_events_v2 WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            sequence = int(row["next_sequence"] if row else 1)
            event_id = f"run_evt_v2_{uuid4().hex}"
            payload_record = self._insert_event_payload(conn, event_id=event_id, payload=payload or {}, created_at=created_at)
            values = (
                event_id,
                workspace_id,
                run_id,
                sequence,
                event_type,
                actor or "system",
                payload_record.payload_ref,
                payload_record.payload_sha256,
                str(summary or "")[:500],
                source_ref,
                idempotency_key,
                created_at,
            )
            try:
                conn.execute(
                    """
                    INSERT INTO run_events_v2(event_id, workspace_id, run_id, sequence, event_type, actor, payload_ref, payload_sha256, summary, source_ref, idempotency_key, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            except sqlite3.IntegrityError:
                if idempotency_key:
                    existing = conn.execute(
                        "SELECT * FROM run_events_v2 WHERE run_id = ? AND idempotency_key = ?",
                        (run_id, idempotency_key),
                    ).fetchone()
                    if existing:
                        return self._run_event_v2_from_row(existing)
                raise
            created = conn.execute("SELECT * FROM run_events_v2 WHERE event_id = ?", (event_id,)).fetchone()
        assert created is not None
        return self._run_event_v2_from_row(created)

    def append_thread_event_v2(
        self,
        *,
        workspace_id: str,
        thread_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        turn_id: str | None = None,
        run_id: str | None = None,
        actor: str = "system",
        summary: str = "",
        source_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> ThreadEventV2:
        created_at = _utc_iso()
        with self._lock, self._connect() as conn:
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM thread_events_v2 WHERE thread_id = ? AND idempotency_key = ?",
                    (thread_id, idempotency_key),
                ).fetchone()
                if existing:
                    return self._thread_event_v2_from_row(existing)
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM thread_events_v2 WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            sequence = int(row["next_sequence"] if row else 1)
            event_id = f"thread_evt_v2_{uuid4().hex}"
            payload_record = self._insert_event_payload(conn, event_id=event_id, payload=payload or {}, created_at=created_at)
            values = (
                event_id,
                workspace_id,
                thread_id,
                turn_id,
                run_id,
                sequence,
                event_type,
                actor or "system",
                payload_record.payload_ref,
                payload_record.payload_sha256,
                str(summary or "")[:500],
                source_ref,
                idempotency_key,
                created_at,
            )
            try:
                conn.execute(
                    """
                    INSERT INTO thread_events_v2(event_id, workspace_id, thread_id, turn_id, run_id, sequence, event_type, actor, payload_ref, payload_sha256, summary, source_ref, idempotency_key, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            except sqlite3.IntegrityError:
                if idempotency_key:
                    existing = conn.execute(
                        "SELECT * FROM thread_events_v2 WHERE thread_id = ? AND idempotency_key = ?",
                        (thread_id, idempotency_key),
                    ).fetchone()
                    if existing:
                        return self._thread_event_v2_from_row(existing)
                raise
            created = conn.execute("SELECT * FROM thread_events_v2 WHERE event_id = ?", (event_id,)).fetchone()
        assert created is not None
        return self._thread_event_v2_from_row(created)

    def list_run_events_v2(self, run_id: str, *, after_sequence: int = 0, limit: int = 500) -> list[RunEventV2]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM run_events_v2 WHERE run_id = ? AND sequence > ? ORDER BY sequence, event_id LIMIT ?",
                (run_id, int(after_sequence or 0), max(1, min(limit, 2000))),
            ).fetchall()
        return [self._run_event_v2_from_row(row) for row in rows]

    def list_thread_events_v2(self, thread_id: str, *, after_sequence: int = 0, limit: int = 500) -> list[ThreadEventV2]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM thread_events_v2 WHERE thread_id = ? AND sequence > ? ORDER BY sequence, event_id LIMIT ?",
                (thread_id, int(after_sequence or 0), max(1, min(limit, 2000))),
            ).fetchall()
        return [self._thread_event_v2_from_row(row) for row in rows]

    def get_event_payload(self, payload_ref: str) -> EventPayloadRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM event_payloads WHERE payload_ref = ?", (payload_ref,)).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        return EventPayloadRecord(payload_ref=str(row["payload_ref"]), payload_sha256=str(row["sha256"]), created_at=str(row["created_at"]), payload=payload if isinstance(payload, dict) else {"value": payload})

    def find_event_by_payload_ref(self, payload_ref: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            run_row = conn.execute(
                "SELECT 'run' AS scope, event_id, workspace_id, run_id, NULL AS thread_id, sequence, event_type, created_at FROM run_events_v2 WHERE payload_ref = ?",
                (payload_ref,),
            ).fetchone()
            if run_row is not None:
                return dict(run_row)
            thread_row = conn.execute(
                "SELECT 'thread' AS scope, event_id, workspace_id, run_id, thread_id, sequence, event_type, created_at FROM thread_events_v2 WHERE payload_ref = ?",
                (payload_ref,),
            ).fetchone()
            return dict(thread_row) if thread_row is not None else None

    def _insert_event_payload(self, conn: sqlite3.Connection, *, event_id: str, payload: dict[str, Any], created_at: str) -> EventPayloadRecord:
        payload_json = self._dump_json(payload)
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        payload_ref = f"event_payload:{event_id}"
        conn.execute(
            """
            INSERT OR IGNORE INTO event_payloads(payload_ref, sha256, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (payload_ref, digest, payload_json, created_at),
        )
        return EventPayloadRecord(payload_ref=payload_ref, payload_sha256=digest, payload=payload, created_at=created_at)

    def append_run_event(self, run_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        created_at = _utc_iso()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            sequence = int(row["next_sequence"] if row else 1)
            record = {
                "event_id": f"run_evt_{uuid4().hex}",
                "run_id": run_id,
                "event_type": event_type,
                "sequence": sequence,
                "payload": payload or {},
                "created_at": created_at,
            }
            conn.execute(
                """
                INSERT INTO run_events(event_id, run_id, event_type, sequence, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record["event_id"],
                    run_id,
                    event_type,
                    sequence,
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                    created_at,
                ),
            )
        return record

    def list_run_events(self, run_id: str, *, after_sequence: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM run_events WHERE run_id = ? AND sequence > ? ORDER BY sequence, event_id LIMIT ?",
                (run_id, int(after_sequence or 0), max(1, min(limit, 2000))),
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]

    def insert_run_state_snapshot(self, *, run_id: str, reason: str, payload: dict[str, Any]) -> dict[str, Any]:
        created_at = _utc_iso()
        record = {
            "snapshot_id": f"run_state_{uuid4().hex}",
            "run_id": run_id,
            "reason": reason,
            "payload": payload,
            "created_at": created_at,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO run_state_snapshots(snapshot_id, run_id, reason, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record["snapshot_id"],
                    run_id,
                    reason,
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                    created_at,
                ),
            )
        return record

    def list_run_state_snapshots(self, run_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM run_state_snapshots WHERE run_id = ? ORDER BY created_at DESC, snapshot_id DESC LIMIT ?",
                (run_id, max(1, min(limit, 200))),
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]

    def insert_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO artifacts(artifact_id, thread_id, turn_id, artifact_type, storage_ref, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (artifact.artifact_id, artifact.thread_id, artifact.turn_id, artifact.artifact_type, artifact.storage_ref, self._dump(artifact), artifact.created_at.isoformat()),
            )
        return artifact

    def record_exec_process(self, process_id: str, payload: dict[str, Any]) -> None:
        now = _utc_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO exec_processes(process_id, thread_id, turn_id, command, status, payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(process_id) DO UPDATE SET status=excluded.status, payload_json=excluded.payload_json, updated_at=excluded.updated_at
                """,
                (
                    process_id,
                    payload.get("thread_id"),
                    payload.get("turn_id"),
                    str(payload.get("command") or ""),
                    str(payload.get("status") or "completed"),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                ),
            )

    def insert_thread_snapshot(self, *, snapshot_id: str, thread_id: str, turn_id: str | None, reason: str, payload: dict[str, Any]) -> dict[str, Any]:
        created_at = _utc_iso()
        record = {
            "snapshot_id": snapshot_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "reason": reason,
            "payload": payload,
            "created_at": created_at,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO thread_snapshots(snapshot_id, thread_id, turn_id, reason, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (snapshot_id, thread_id, turn_id, reason, json.dumps(record, ensure_ascii=False, separators=(",", ":")), created_at),
            )
        return record

    def list_thread_snapshots(self, thread_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM thread_snapshots WHERE thread_id = ? ORDER BY created_at DESC, snapshot_id DESC LIMIT ?",
                (thread_id, max(1, min(limit, 200))),
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]

    def _next_sequence(self, table: str, thread_id: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(f"SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM {table} WHERE thread_id = ?", (thread_id,)).fetchone()
            return int(row["next_sequence"] if row else 1)

    @staticmethod
    def _encode_cursor(created_at: str, item_id: str) -> str:
        return f"{created_at}|{item_id}"

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[str, str]:
        if "|" not in cursor:
            return cursor, ""
        created_at, item_id = cursor.split("|", 1)
        return created_at, item_id
