from __future__ import annotations

import re


class MiniappGenerationContractApiRoutesCrud:
    @staticmethod
    def _deterministic_requests_route_source() -> str:
        return MiniappGenerationContractApiRoutesCrud._deterministic_resource_route_source("requests")

    @staticmethod
    def _deterministic_resource_route_source(resource_slug: str) -> str:
        normalized_slug = str(resource_slug or "").strip().strip("/").replace("\\", "/")
        normalized_slug = normalized_slug.replace("-", "_")
        normalized_slug = normalized_slug or "records"
        resource_path = normalized_slug.replace("_", "-")
        singular_slug = normalized_slug[:-1] if normalized_slug.endswith("s") and len(normalized_slug) > 3 else normalized_slug
        if singular_slug.endswith("ie"):
            singular_slug = singular_slug[:-2] + "y"
        schema_prefix = "".join(part.capitalize() for part in re.split(r"[_-]+", singular_slug) if part) or "Record"
        source = f'''from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, get_args, get_origin
from uuid import uuid4

from fastapi import APIRouter, Body, HTTPException
from pydantic import ValidationError
from sqlalchemy import DateTime, MetaData, String, Table, func, inspect, select
from sqlalchemy.orm import Mapped, mapped_column

import app.db as db_module
import app.schemas as schemas_module
from app.db import Base, SessionLocal, engine

RESOURCE_SLUG = "{resource_path}"
RESOURCE_SINGULAR = "{singular_slug}"
SCHEMA_PREFIX = "{schema_prefix}"
_DYNAMIC_RESOURCE_MODEL: type[Any] | None = None
_REFLECTED_RESOURCE_MODEL: type[Any] | None = None

router = APIRouter(prefix="/api", tags=[RESOURCE_SLUG])


def _candidate_table_names() -> set[str]:
    return {{
        RESOURCE_SLUG,
        RESOURCE_SLUG.replace("-", "_"),
        RESOURCE_SINGULAR,
        RESOURCE_SINGULAR.replace("-", "_"),
    }}


def _candidate_schema_names() -> list[str]:
    return [SCHEMA_PREFIX]


def _resource_model() -> type[Any]:
    for value in vars(db_module).values():
        if not isinstance(value, type):
            continue
        if value is Base or not issubclass(value, Base):
            continue
        table_name = str(getattr(value, "__tablename__", "") or "").strip().lower()
        if table_name and table_name in _candidate_table_names():
            return value
    for prefix in _candidate_schema_names():
        for candidate in (f"{{prefix}}Record", f"{{prefix}}Item", f"{{prefix}}Row"):
            value = getattr(db_module, candidate, None)
            if isinstance(value, type):
                return value
    reflected = _reflected_resource_model()
    if reflected is not None:
        return reflected
    global _DYNAMIC_RESOURCE_MODEL
    if _DYNAMIC_RESOURCE_MODEL is None:
        table_name = RESOURCE_SLUG.replace("-", "_")
        _DYNAMIC_RESOURCE_MODEL = type(
            f"{{SCHEMA_PREFIX}}Record",
            (Base,),
            {{
                "__tablename__": table_name,
                "__annotations__": {{
                    "id": Mapped[str],
                    "title": Mapped[str],
                    "details": Mapped[str],
                    "status": Mapped[str | None],
                    "created_at": Mapped[datetime],
                    "updated_at": Mapped[datetime],
                }},
                "id": mapped_column(String(64), primary_key=True),
                "title": mapped_column(String(255), default=""),
                "details": mapped_column(String(4096), default=""),
                "status": mapped_column(String(64), nullable=True),
                "created_at": mapped_column(DateTime(timezone=True), default=_now),
                "updated_at": mapped_column(DateTime(timezone=True), default=_now),
            }},
        )
        Base.metadata.create_all(bind=engine)
    return _DYNAMIC_RESOURCE_MODEL


def _reflected_resource_model() -> type[Any] | None:
    global _REFLECTED_RESOURCE_MODEL
    if _REFLECTED_RESOURCE_MODEL is not None:
        return _REFLECTED_RESOURCE_MODEL
    try:
        inspector = inspect(engine)
        table_name = next(
            (
                name
                for name in inspector.get_table_names()
                if str(name or "").strip().lower() in _candidate_table_names()
            ),
            None,
        )
        if not table_name:
            return None
        metadata = MetaData()
        table = Table(str(table_name), metadata, autoload_with=engine)
    except Exception:
        return None
    _REFLECTED_RESOURCE_MODEL = type(
        f"{{SCHEMA_PREFIX}}ReflectedRecord",
        (Base,),
        {{
            "__table__": table,
            "__module__": __name__,
        }},
    )
    return _REFLECTED_RESOURCE_MODEL


def _schema_model(suffixes: tuple[str, ...]) -> type[Any] | None:
    for prefix in _candidate_schema_names():
        for suffix in suffixes:
            value = getattr(schemas_module, f"{{prefix}}{{suffix}}", None)
            if isinstance(value, type):
                return value
    return None


def _create_schema_model() -> type[Any] | None:
    return _schema_model(("Create",))


def _update_schema_model() -> type[Any] | None:
    return _schema_model(("Update",))


def _read_schema_model() -> type[Any] | None:
    return _schema_model(("Read", "Summary", "Detail"))


def _list_schema_model() -> type[Any] | None:
    return _schema_model(("ListResponse",))


def _schema_field_names(schema_model: type[Any] | None) -> set[str]:
    if schema_model is None:
        return set()
    fields = getattr(schema_model, "model_fields", {{}}) or {{}}
    return {{str(name) for name in fields.keys()}}


def _passthrough_read_fields() -> set[str]:
    primary_key = _primary_key_name()
    fields = {{
        primary_key,
        "id",
        "record_id",
        f"{{RESOURCE_SINGULAR}}_id",
        "request_id",
    }}
    fields.update(_schema_field_names(_update_schema_model()))
    if "status" in _column_keys():
        fields.add("status")
    return {{field for field in fields if field}}


def _status_literals() -> list[str]:
    for schema_model in (_update_schema_model(), _read_schema_model(), _create_schema_model()):
        if schema_model is None:
            continue
        fields = getattr(schema_model, "model_fields", {{}}) or {{}}
        status_field = fields.get("status")
        if status_field is None:
            continue
        annotation = getattr(status_field, "annotation", None)
        origin = get_origin(annotation)
        if origin is None:
            continue
        literals = [str(item) for item in get_args(annotation) if isinstance(item, str)]
        if literals:
            return literals
    return []


def _normalize_status(value: Any) -> Any:
    if value is None:
        return None
    status = str(value).strip()
    if not status:
        return None
    allowed = _status_literals()
    if not allowed or status in allowed:
        return status
    if status == "in_progress":
        for candidate in ("claimed", "in_review", "issued", "open", "active", "processing"):
            if candidate in allowed:
                return candidate
    return allowed[0]


def _model_inspector():
    return inspect(_resource_model())


def _primary_key_name() -> str:
    primary_keys = list(_model_inspector().primary_key)
    if not primary_keys:
        return "id"
    return str(primary_keys[0].key)


def _primary_key_python_type() -> type[Any]:
    primary_keys = list(_model_inspector().primary_key)
    if not primary_keys:
        return str
    column = primary_keys[0]
    try:
        return column.type.python_type
    except Exception:
        return str


def _coerce_primary_key(value: str) -> Any:
    python_type = _primary_key_python_type()
    if python_type is str:
        return str(value)
    try:
        return python_type(value)
    except Exception:
        return value


def _column_keys() -> list[str]:
    return [str(column.key) for column in _model_inspector().columns]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_payload(payload: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    schema_model = _update_schema_model() if partial else _create_schema_model()
    data = dict(payload or {{}})
    if "status" in data:
        normalized_status = _normalize_status(data.get("status"))
        if normalized_status is not None:
            data["status"] = normalized_status
    if schema_model is None:
        return data
    try:
        validated = schema_model.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    dumped = validated.model_dump(exclude_none=partial)
    if "status" in dumped:
        normalized_status = _normalize_status(dumped.get("status"))
        if normalized_status is not None:
            dumped["status"] = normalized_status
    return dumped


def _normalize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _record_to_payload(record: Any) -> dict[str, Any]:
    payload = {{
        key: _normalize_value(getattr(record, key))
        for key in _column_keys()
    }}
    primary_key = _primary_key_name()
    record_id = payload.get(primary_key)
    if record_id is not None:
        record_id = str(record_id)
        payload.setdefault(primary_key, record_id)
        payload.setdefault("id", record_id)
        payload.setdefault("record_id", record_id)
        payload.setdefault(f"{{RESOURCE_SINGULAR}}_id", record_id)
        payload.setdefault("request_id", record_id)
    read_schema = _read_schema_model()
    if read_schema is None:
        return payload
    try:
        serialized = read_schema.model_validate(payload).model_dump(mode="json")
    except ValidationError:
        return payload
    if not isinstance(serialized, dict):
        return payload
    for field in _passthrough_read_fields():
        if field in payload and field not in serialized:
            serialized[field] = payload[field]
    return serialized


def _apply_model_defaults(record: Any) -> None:
    primary_key = _primary_key_name()
    if getattr(record, primary_key, None) in (None, "") and _primary_key_python_type() is str:
        setattr(record, primary_key, uuid4().hex[:12])
    now = _now()
    for field in ("created_at", "updated_at"):
        if field in _column_keys() and getattr(record, field, None) is None:
            setattr(record, field, now)


def _apply_payload(record: Any, payload: dict[str, Any], *, partial: bool) -> None:
    mutable_columns = set(_column_keys())
    primary_key = _primary_key_name()
    mutable_columns.discard(primary_key)
    for key, value in payload.items():
        if key not in mutable_columns:
            continue
        if value is None and partial:
            continue
        setattr(record, key, value)
    if "updated_at" in mutable_columns:
        setattr(record, "updated_at", _now())


def _list_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    list_schema = _list_schema_model()
    base_payload = {{"items": items}}
    if list_schema is None:
        return base_payload
    try:
        return list_schema.model_validate(base_payload).model_dump(mode="json")
    except ValidationError:
        return base_payload


@router.get(f"/{{RESOURCE_SLUG}}/status-counts")
def list_status_counts() -> dict[str, Any]:
    model = _resource_model()
    if "status" not in _column_keys():
        return {{"items": []}}
    with SessionLocal() as session:
        rows = session.execute(
            select(getattr(model, "status"), func.count()).group_by(getattr(model, "status"))
        ).all()
    return {{
        "items": [
            {{"status": str(status or ""), "count": int(count or 0)}}
            for status, count in rows
        ]
    }}


@router.get(f"/{{RESOURCE_SLUG}}")
def list_records() -> dict[str, Any]:
    model = _resource_model()
    order_field = "updated_at" if "updated_at" in _column_keys() else "created_at" if "created_at" in _column_keys() else _primary_key_name()
    with SessionLocal() as session:
        rows = session.scalars(select(model).order_by(getattr(model, order_field).desc())).all()
    return _list_payload([_record_to_payload(item) for item in rows])


@router.post(f"/{{RESOURCE_SLUG}}")
def create_record(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    model = _resource_model()
    normalized = _normalize_payload(payload, partial=False)
    record = model()
    _apply_model_defaults(record)
    _apply_payload(record, normalized, partial=False)
    with SessionLocal() as session:
        session.add(record)
        session.commit()
        session.refresh(record)
        return _record_to_payload(record)


@router.get(f"/{{RESOURCE_SLUG}}/{{{{item_id}}}}")
def get_record(item_id: str) -> dict[str, Any]:
    model = _resource_model()
    with SessionLocal() as session:
        record = session.get(model, _coerce_primary_key(item_id))
        if record is None:
            raise HTTPException(status_code=404, detail="Record not found")
        return _record_to_payload(record)


@router.put(f"/{{RESOURCE_SLUG}}/{{{{item_id}}}}")
@router.patch(f"/{{RESOURCE_SLUG}}/{{{{item_id}}}}")
def update_record(item_id: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    model = _resource_model()
    normalized = _normalize_payload(payload, partial=True)
    with SessionLocal() as session:
        record = session.get(model, _coerce_primary_key(item_id))
        if record is None:
            raise HTTPException(status_code=404, detail="Record not found")
        _apply_payload(record, normalized, partial=True)
        session.add(record)
        session.commit()
        session.refresh(record)
        return _record_to_payload(record)
'''
        return source

    @staticmethod
    def _deterministic_comments_route_source() -> str:
        return """from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, HTTPException
from sqlalchemy import text

from app.db import engine

router = APIRouter(prefix="/api/comments", tags=["comments"])


def _ensure_tables() -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS requests ("
            "id TEXT PRIMARY KEY, "
            "title TEXT, "
            "description TEXT, "
            "client_name TEXT, "
            "phone TEXT, "
            "preferred_time TEXT, "
            "comment TEXT, "
            "status TEXT, "
            "assigned_specialist TEXT, "
            "item_type TEXT, "
            "item_label TEXT, "
            "start_date TEXT, "
            "end_date TEXT, "
            "reason TEXT, "
            "specialist_notes TEXT, "
            "created_at TEXT, "
            "updated_at TEXT)"
        ))
        conn.execute(text("CREATE TABLE IF NOT EXISTS comments (id TEXT PRIMARY KEY, request_id TEXT, comment TEXT, author_role TEXT, created_at TEXT)"))


@router.get("")
@router.get("/")
def list_comments(request_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
    _ensure_tables()
    query = "SELECT id, request_id, comment, author_role, created_at FROM comments"
    params: dict[str, Any] = {}
    if request_id:
        query += " WHERE request_id = :request_id"
        params["request_id"] = request_id
    query += " ORDER BY created_at ASC"
    with engine.begin() as conn:
        rows = conn.execute(text(query), params).fetchall()
    return {
        "items": [
            {
                "comment_id": row._mapping["id"],
                "request_id": row._mapping["request_id"],
                "comment": row._mapping["comment"] or "",
                "author_role": row._mapping["author_role"] or "",
                "created_at": row._mapping["created_at"],
            }
            for row in rows
        ]
    }


@router.post("")
@router.post("/")
def create_comment(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    _ensure_tables()
    comment_id = uuid4().hex[:12]
    request_id = str(payload.get("request_id") or payload.get("id") or "")
    if not request_id:
        raise HTTPException(status_code=400, detail="request_id required")
    record = {
        "id": comment_id,
        "request_id": request_id,
        "comment": str(payload.get("comment") or payload.get("note") or ""),
        "author_role": str(payload.get("author_role") or "specialist"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with engine.begin() as conn:
        request_row = conn.execute(text("SELECT id FROM requests WHERE id = :id"), {"id": request_id}).first()
        if request_row is None:
            raise HTTPException(status_code=404, detail="Request not found")
        conn.execute(text("INSERT INTO comments (id, request_id, comment, author_role, created_at) VALUES (:id, :request_id, :comment, :author_role, :created_at)"), record)
    return {"comment_id": comment_id, **record}
"""

    @staticmethod
    def _deterministic_assignments_route_source() -> str:
        return """from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from sqlalchemy import text

from app.db import engine

router = APIRouter(prefix="/api/assignments", tags=["assignments"])


def _ensure_tables() -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS requests ("
            "id TEXT PRIMARY KEY, "
            "title TEXT, "
            "description TEXT, "
            "client_name TEXT, "
            "phone TEXT, "
            "preferred_time TEXT, "
            "comment TEXT, "
            "status TEXT, "
            "assigned_specialist TEXT, "
            "item_type TEXT, "
            "item_label TEXT, "
            "start_date TEXT, "
            "end_date TEXT, "
            "reason TEXT, "
            "specialist_notes TEXT, "
            "created_at TEXT, "
            "updated_at TEXT)"
        ))


@router.post("")
@router.patch("/{request_id}")
def assign_request(request_id: str | None = None, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    assignment_request_id = str(request_id or payload.get("request_id") or payload.get("id") or "")
    specialist = str(payload.get("specialist_id") or payload.get("assignee_id") or payload.get("assigned_specialist") or "")
    now = datetime.now(timezone.utc).isoformat()
    _ensure_tables()
    with engine.begin() as conn:
        row = conn.execute(text("SELECT id FROM requests WHERE id = :id"), {"id": assignment_request_id}).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Request not found")
        conn.execute(text("UPDATE requests SET assigned_specialist = :specialist, updated_at = :updated_at WHERE id = :id"), {"id": assignment_request_id, "specialist": specialist, "updated_at": now})
    return {"request_id": assignment_request_id, "assigned_specialist": specialist, "updated_at": now}
"""
