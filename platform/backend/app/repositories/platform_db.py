from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.models.threads import ArtifactRecord, ItemRecord, RolloutEventRecord, ThreadRecord, TurnRecord


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PlatformDb:
    """SQLite-backed append-oriented store for thread/turn/item state."""

    CURRENT_SCHEMA_VERSION = 1

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

