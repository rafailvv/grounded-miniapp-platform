from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any


SECRET_RE = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|private[_-]?key|BEGIN [A-Z ]*PRIVATE KEY)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProductWorkerRole:
    worker_id: str
    worker_type: str
    aliases: tuple[str, ...]
    lane_id: str
    ownership_kind: str
    owner_scope: str
    allowed_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    role: str | None
    expected_proof: tuple[str, ...]
    merge_evidence: tuple[str, ...]
    writes: bool = True


PRODUCT_WORKERS: tuple[ProductWorkerRole, ...] = (
    ProductWorkerRole(
        worker_id="backend_api_worker",
        worker_type="backend_api_worker",
        aliases=("backend_worker", "api_worker", "persistence_api_worker", "persistence_worker"),
        lane_id="backend",
        ownership_kind="backend_api_persistence",
        owner_scope="Backend API, schemas, persistence, and shared role state",
        allowed_paths=("miniapp/app/routes", "miniapp/app/schemas.py", "miniapp/app/db.py"),
        forbidden_paths=("miniapp/app/main.py", "miniapp/app/static/client", "miniapp/app/static/specialist", "miniapp/app/static/manager", "miniapp/tests"),
        role=None,
        expected_proof=("api_workflow_smoke", "lsp_static_diagnostics"),
        merge_evidence=("owned_paths_only", "apply_result", "lsp_changed_files", "api_or_route_proof"),
    ),
    ProductWorkerRole(
        worker_id="client_surface_worker",
        worker_type="client_surface_worker",
        aliases=("role_ui_worker:client", "client_ui_worker"),
        lane_id="role_ui",
        ownership_kind="role_ui_surface",
        owner_scope="Client role surface and client child pages",
        allowed_paths=("miniapp/app/static/client",),
        forbidden_paths=("miniapp/app/static/specialist", "miniapp/app/static/manager", "miniapp/app/routes", "miniapp/tests"),
        role="client",
        expected_proof=("browser_flow_smoke:client", "mobile_layout:client"),
        merge_evidence=("owned_paths_only", "apply_result", "role_browser_or_mobile_proof"),
    ),
    ProductWorkerRole(
        worker_id="specialist_surface_worker",
        worker_type="specialist_surface_worker",
        aliases=("role_ui_worker:specialist", "specialist_ui_worker"),
        lane_id="role_ui",
        ownership_kind="role_ui_surface",
        owner_scope="Specialist role surface and specialist child pages",
        allowed_paths=("miniapp/app/static/specialist",),
        forbidden_paths=("miniapp/app/static/client", "miniapp/app/static/manager", "miniapp/app/routes", "miniapp/tests"),
        role="specialist",
        expected_proof=("browser_flow_smoke:specialist", "mobile_layout:specialist"),
        merge_evidence=("owned_paths_only", "apply_result", "role_browser_or_mobile_proof"),
    ),
    ProductWorkerRole(
        worker_id="manager_surface_worker",
        worker_type="manager_surface_worker",
        aliases=("role_ui_worker:manager", "manager_ui_worker"),
        lane_id="role_ui",
        ownership_kind="role_ui_surface",
        owner_scope="Manager role surface and manager child pages",
        allowed_paths=("miniapp/app/static/manager",),
        forbidden_paths=("miniapp/app/static/client", "miniapp/app/static/specialist", "miniapp/app/routes", "miniapp/tests"),
        role="manager",
        expected_proof=("browser_flow_smoke:manager", "mobile_layout:manager"),
        merge_evidence=("owned_paths_only", "apply_result", "role_browser_or_mobile_proof"),
    ),
    ProductWorkerRole(
        worker_id="test_verifier_worker",
        worker_type="test_verifier_worker",
        aliases=("tests_worker", "test_worker", "acceptance_tests_worker"),
        lane_id="tests",
        ownership_kind="generated_acceptance_tests",
        owner_scope="Generated acceptance tests and independent verification artifacts",
        allowed_paths=("miniapp/tests",),
        forbidden_paths=("miniapp/app/static/client", "miniapp/app/static/specialist", "miniapp/app/static/manager"),
        role=None,
        expected_proof=("generated_acceptance_tests", "api_workflow_smoke", "browser_flow_smoke"),
        merge_evidence=("owned_paths_only", "apply_result", "generated_tests_reference_product_paths"),
    ),
    ProductWorkerRole(
        worker_id="mobile_polish_worker",
        worker_type="mobile_polish_worker",
        aliases=("verifier_worker", "product_verifier_worker", "visual_verifier_worker"),
        lane_id="verifier",
        ownership_kind="read_only_product_verification",
        owner_scope="Mobile polish pass for role UI surfaces after green workflow",
        allowed_paths=("miniapp/app/static/client", "miniapp/app/static/specialist", "miniapp/app/static/manager", "miniapp/app/static/shared"),
        forbidden_paths=("miniapp/app/routes", "miniapp/app/schemas.py", "miniapp/app/db.py", "miniapp/tests"),
        role=None,
        expected_proof=("mobile_layout", "browser_flow_smoke"),
        merge_evidence=("read_only", "browser_proof", "mobile_layout", "blocker_findings"),
        writes=False,
    ),
    ProductWorkerRole(
        worker_id="repair_worker",
        worker_type="repair_worker",
        aliases=("targeted_repair_worker",),
        lane_id="repair",
        ownership_kind="targeted_repair_slice",
        owner_scope="Owned repair slice from a failure signature or repair packet",
        allowed_paths=("miniapp/app", "miniapp/tests"),
        forbidden_paths=("runtime", ".github", "docker"),
        role=None,
        expected_proof=("repair_packet_resolved", "latest_failed_check_passes"),
        merge_evidence=("repair_packet", "owned_paths_only", "latest_failed_check_passes"),
    ),
)


def canonical_worker_id(worker_id: str) -> str:
    value = str(worker_id or "").strip()
    for role in PRODUCT_WORKERS:
        if value == role.worker_id or value in role.aliases:
            return role.worker_id
    return value


def role_for_worker(worker_id: str) -> ProductWorkerRole | None:
    canonical = str(worker_id or "").strip()
    for role in PRODUCT_WORKERS:
        if canonical == role.worker_id:
            return role
    return None


def worker_refs(workspace_id: str, run_id: str, worker_id: str) -> dict[str, str]:
    canonical = canonical_worker_id(worker_id)
    return {
        "context_ref": f"worker_context:{workspace_id}:{run_id}:{canonical}",
        "memory_snapshot_ref": f"worker_memory_snapshot:{workspace_id}:{run_id}:{canonical}",
        "output_ref": f"worker_output:{workspace_id}:{run_id}:{canonical}",
        "merge_decision_ref": f"worker_manager_merge_decision:{workspace_id}:{run_id}",
    }


def ownership_for_worker(worker_id: str) -> dict[str, Any]:
    role = role_for_worker(worker_id)
    if role is None:
        return {
            "allowed_paths": [],
            "forbidden_paths": [],
            "exclusive_write": False,
            "role": None,
            "expected_proof": [],
        }
    return {
        "allowed_paths": list(role.allowed_paths),
        "forbidden_paths": list(role.forbidden_paths),
        "exclusive_write": role.writes,
        "role": role.role,
        "lane_id": role.lane_id,
        "ownership_kind": role.ownership_kind,
        "expected_proof": list(role.expected_proof),
        "merge_evidence": list(role.merge_evidence),
    }


def product_owner_contract(worker_id: str) -> dict[str, Any]:
    role = role_for_worker(canonical_worker_id(worker_id))
    if role is None:
        return {
            "worker_id": canonical_worker_id(worker_id),
            "status": "unknown_worker",
            "lane_id": "unknown",
            "ownership_kind": "unknown",
            "writes": False,
            "allowed_paths": [],
            "forbidden_paths": [],
            "expected_proof": [],
            "merge_evidence": [],
        }
    return {
        "worker_id": role.worker_id,
        "worker_type": role.worker_type,
        "alias_ids": list(role.aliases),
        "lane_id": role.lane_id,
        "ownership_kind": role.ownership_kind,
        "owner_scope": role.owner_scope,
        "writes": role.writes,
        "role": role.role,
        "allowed_paths": list(role.allowed_paths),
        "forbidden_paths": list(role.forbidden_paths),
        "expected_proof": list(role.expected_proof),
        "merge_evidence": list(role.merge_evidence),
        "merge_policy": "merge only owned paths with structural proof now and runtime/browser proof before final readiness",
    }


def path_is_allowed(worker_id: str, path: str) -> bool:
    role = role_for_worker(worker_id)
    normalized = str(path or "").strip().replace("\\", "/")
    if role is None:
        return False
    if any(normalized == prefix.rstrip("/") or normalized.startswith(prefix.rstrip("/") + "/") for prefix in role.forbidden_paths):
        return False
    return any(normalized == prefix.rstrip("/") or normalized.startswith(prefix.rstrip("/") + "/") for prefix in role.allowed_paths)


def select_memory_items(memory: dict[str, Any], *, worker_id: str, limit: int = 12) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    role = role_for_worker(worker_id)
    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    role_terms = {str(role.role or ""), canonical_worker_id(worker_id)}
    role_terms = {item for item in role_terms if item}
    for item in memory.get("items") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("summary") or "")
        if SECRET_RE.search(text) or SECRET_RE.search(str(item)):
            rejected.append({"reason": "secret_like_material", "kind": item.get("kind"), "text_excerpt": text[:120]})
            continue
        haystack = f"{text} {item.get('kind') or ''} {item.get('scope') or ''}".lower()
        if not role_terms or not any(term.lower() in haystack for term in role_terms):
            if item.get("kind") not in {"preference", "product_decision", "working_pattern", "failure_signature", "avoidance"}:
                continue
        selected.append({k: v for k, v in item.items() if k not in {"raw", "secret"}})
        if len(selected) >= limit:
            break
    return selected, rejected


def stale_path_checks(workspace_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    refs: set[str] = set()
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        for key in ("path", "paths", "files"):
            value = item.get(key)
            if isinstance(value, str):
                refs.add(value)
            elif isinstance(value, list):
                refs.update(str(entry) for entry in value if isinstance(entry, str))
    checks = [
        {"path": ref, "exists": (workspace_root / ref).exists()}
        for ref in sorted(refs)
        if ref.startswith(("miniapp/", "app/", "tests/"))
    ]
    return {"status": "stale" if any(not item["exists"] for item in checks) else "fresh_or_unreferenced", "items": checks[:40]}
