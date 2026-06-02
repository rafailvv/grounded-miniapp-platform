from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models.domain import RunCheckResult


ROLE_ORDER = ("client", "specialist", "manager")
COMPLETED_STATUSES = {"completed", "done", "passed"}
BLOCKED_STATUSES = {"blocked", "failed"}
ACTIVE_STATUSES = {"in_progress", "running", "started"}
LANE_ORDER = ("planner", "backend", "ui", "tests", "browser_verifier", "repair")


class RunTaskLedger:
    """Builds the model-visible product task ledger into API/runtime tasks."""

    SCHEMA = "grounded.run_task_ledger.v1"
    TASK_GRAPH_SCHEMA = "grounded.run_task_graph.v1"

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        workspace_id: str,
        implementation_plan: dict[str, Any] | None,
        run_status: str = "pending",
        current_stage: str = "",
        results: list[RunCheckResult | dict[str, Any]] | None = None,
        remaining_issues: list[dict[str, Any]] | None = None,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        plan = implementation_plan if isinstance(implementation_plan, dict) else {}
        raw_items = plan.get("runtime_task_ledger") if isinstance(plan.get("runtime_task_ledger"), list) else None
        source = "implementation_plan.runtime_task_ledger" if raw_items is not None else "implementation_plan.product_task_ledger"
        if raw_items is None:
            raw_items = plan.get("product_task_ledger") if isinstance(plan.get("product_task_ledger"), list) else []
        by_check = cls._results_by_name(results or [])
        issues = [item for item in (remaining_issues or []) if isinstance(item, dict)]
        items = [
            cls._task_from_item(
                item=item,
                index=index,
                run_id=run_id,
                source=source,
                run_status=run_status,
                current_stage=current_stage,
                by_check=by_check,
                issues=issues,
                updated_at=updated_at,
            )
            for index, item in enumerate(raw_items if isinstance(raw_items, list) else [], start=1)
            if isinstance(item, dict)
        ]
        counts = {
            "planned": sum(1 for item in items if item["status"] == "planned"),
            "in_progress": sum(1 for item in items if item["status"] == "in_progress"),
            "blocked": sum(1 for item in items if item["status"] == "blocked"),
            "completed": sum(1 for item in items if item["status"] == "completed"),
        }
        task_graph = cls._build_task_graph(
            run_id=run_id,
            workspace_id=workspace_id,
            items=items,
            plan=plan,
            run_status=run_status,
            counts=counts,
            updated_at=updated_at,
        )
        return {
            "schema": cls.SCHEMA,
            "run_id": run_id,
            "workspace_id": workspace_id,
            "source": source,
            "status": "blocked" if counts["blocked"] else "completed" if items and counts["completed"] == len(items) else "in_progress" if counts["in_progress"] else "planned",
            "counts": counts,
            "items": items,
            "task_graph": task_graph,
            "updated_at": updated_at or datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def blocking_issues(
        cls,
        *,
        run_id: str,
        workspace_id: str,
        implementation_plan: dict[str, Any] | None,
        run_status: str = "pending",
        current_stage: str = "",
        results: list[RunCheckResult | dict[str, Any]] | None = None,
        remaining_issues: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        ledger = cls.build(
            run_id=run_id,
            workspace_id=workspace_id,
            implementation_plan=implementation_plan,
            run_status=run_status,
            current_stage=current_stage,
            results=results,
            remaining_issues=remaining_issues,
        )
        issues: list[dict[str, Any]] = []
        for item in ledger["items"]:
            if item.get("status") == "completed":
                continue
            if item.get("status") == "planned" and run_status not in {"completed", "blocked", "failed"}:
                continue
            issues.append(
                {
                    "kind": "runtime_task_ledger",
                    "check": "runtime_task_ledger",
                    "details": f"Runtime task {item.get('task_id')} is {item.get('status')} and must be completed before finalization.",
                    "task_id": item.get("task_id"),
                    "title": item.get("title"),
                    "role": item.get("role"),
                    "owner": item.get("owner"),
                    "proof_status": item.get("proof_status"),
                    "target_files": item.get("files") or [],
                    "blocking": True,
                    "evidence": {
                        "blocker": item.get("blocker"),
                        "proof": item.get("proof") or {},
                    },
                }
            )
        return issues

    @classmethod
    def _task_from_item(
        cls,
        *,
        item: dict[str, Any],
        index: int,
        run_id: str,
        source: str,
        run_status: str,
        current_stage: str,
        by_check: dict[str, dict[str, Any]],
        issues: list[dict[str, Any]],
        updated_at: str | None,
    ) -> dict[str, Any]:
        task_id = str(item.get("task_id") or item.get("id") or f"{run_id}:ledger:{index}")
        role = str(item.get("role") or "").strip().lower()
        proof_checks = cls._string_list(item.get("proof_checks") or item.get("required_tests"))
        blocker = cls._matching_blocker(task_id=task_id, item=item, issues=issues)
        proof = cls._proof_for_checks(proof_checks, by_check)
        explicit_status = cls._normalize_status(str(item.get("status") or ""))
        derived_status = cls._derived_status(
            run_status=run_status,
            current_stage=current_stage,
            proof_checks=proof_checks,
            proof=proof,
            blocker=blocker,
        )
        status = derived_status if explicit_status == "planned" and derived_status == "completed" else explicit_status or derived_status
        title = str(
            item.get("title")
            or item.get("content")
            or item.get("description")
            or item.get("intent")
            or item.get("id")
            or f"Task {index}"
        ).strip()
        return {
            "task_id": task_id,
            "title": title,
            "phase": str(item.get("phase") or item.get("kind") or "product_task"),
            "lane": cls._lane_for_item(item, role),
            "status": status,
            "owner": str(item.get("owner") or cls._owner_for_item(item, role)),
            "role": role or None,
            "files": cls._string_list(item.get("owned_paths") or item.get("files")),
            "dependencies": cls._string_list(item.get("depends_on") or item.get("dependencies") or item.get("requires")),
            "proof": proof,
            "proof_status": cls._proof_status(proof_checks, proof),
            "proof_checks": proof_checks,
            "blocker": blocker,
            "artifact_refs": {
                "task_ledger": f"task_ledger:{run_id}",
                "source": source,
                **(item.get("artifact_refs") if isinstance(item.get("artifact_refs"), dict) else {}),
            },
            "proof_refs": cls._proof_refs(proof),
            "source": "runtime_task_ledger",
            "updated_at": item.get("updated_at") or updated_at,
        }

    @classmethod
    def _build_task_graph(
        cls,
        *,
        run_id: str,
        workspace_id: str,
        items: list[dict[str, Any]],
        plan: dict[str, Any],
        run_status: str,
        counts: dict[str, int],
        updated_at: str | None,
    ) -> dict[str, Any]:
        explicit = plan.get("task_graph") if isinstance(plan.get("task_graph"), dict) else {}
        raw_nodes = explicit.get("nodes") if isinstance(explicit.get("nodes"), list) else []
        if raw_nodes:
            nodes = [cls._graph_node_from_raw(node, index=index, updated_at=updated_at) for index, node in enumerate(raw_nodes, start=1) if isinstance(node, dict)]
        else:
            nodes = cls._graph_nodes_from_items(run_id=run_id, items=items, run_status=run_status, updated_at=updated_at)
        edges = cls._graph_edges(nodes, explicit_edges=explicit.get("edges") if isinstance(explicit.get("edges"), list) else [])
        nodes_by_id = {str(node.get("task_id")): node for node in nodes}
        for node in nodes:
            dependencies = [dep for dep in cls._string_list(node.get("dependencies")) if dep in nodes_by_id]
            node["dependencies"] = dependencies
            if node.get("lane") == "repair" and node.get("status") in {"planned", "in_progress"}:
                node["ready"] = True
                node["waiting_on"] = []
                continue
            if node.get("status") == "planned" and dependencies:
                blocked_by = [dep for dep in dependencies if nodes_by_id.get(dep, {}).get("status") != "completed"]
                if blocked_by:
                    node["ready"] = False
                    node["waiting_on"] = blocked_by
                    continue
            node["ready"] = node.get("status") in {"planned", "in_progress"} and not node.get("blocker")
            node["waiting_on"] = []
        graph_counts = {
            "nodes": len(nodes),
            "edges": len(edges),
            "ready": sum(1 for node in nodes if node.get("ready") is True),
            "blocked": sum(1 for node in nodes if node.get("status") == "blocked"),
            "completed": sum(1 for node in nodes if node.get("status") == "completed"),
        }
        next_tasks = [node for node in nodes if node.get("ready") is True][:5]
        blockers = [cls._graph_blocker(node) for node in nodes if node.get("status") == "blocked" or node.get("blocker")]
        return {
            "schema": cls.TASK_GRAPH_SCHEMA,
            "run_id": run_id,
            "workspace_id": workspace_id,
            "status": "blocked" if blockers else "completed" if nodes and graph_counts["completed"] == len(nodes) else "in_progress" if run_status == "running" or counts.get("in_progress") else "planned",
            "lanes": list(LANE_ORDER),
            "counts": graph_counts,
            "nodes": nodes,
            "edges": edges,
            "next_tasks": next_tasks,
            "blockers": blockers,
            "proof": cls._graph_proof(nodes),
            "updated_at": updated_at or datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def _graph_nodes_from_items(cls, *, run_id: str, items: list[dict[str, Any]], run_status: str, updated_at: str | None) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = [
            {
                "task_id": "planner.plan_ready",
                "title": "Planner produces executable product graph",
                "lane": "planner",
                "status": "completed" if items or run_status in {"running", "completed", "blocked", "failed"} else "planned",
                "dependencies": [],
                "owner": "planner",
                "files": [],
                "artifact_refs": {"implementation_plan": f"implementation_plan:{run_id}"},
                "proof": {"implementation_plan": {"status": "passed" if items else "missing"}},
                "proof_status": "passed" if items else "missing",
                "blocker": None,
                "source": "task_graph",
                "updated_at": updated_at,
            }
        ]
        backend_ids: list[str] = []
        ui_ids: list[str] = []
        test_ids: list[str] = []
        for item in items:
            node = {
                "task_id": item.get("task_id"),
                "title": item.get("title"),
                "lane": item.get("lane") or "ui",
                "status": item.get("status"),
                "dependencies": cls._string_list(item.get("dependencies")),
                "owner": item.get("owner"),
                "role": item.get("role"),
                "files": item.get("files") or [],
                "artifact_refs": item.get("artifact_refs") or {},
                "proof": item.get("proof") or {},
                "proof_refs": item.get("proof_refs") or [],
                "proof_status": item.get("proof_status"),
                "proof_checks": item.get("proof_checks") or [],
                "blocker": item.get("blocker"),
                "source": item.get("source") or "runtime_task_ledger",
                "updated_at": item.get("updated_at") or updated_at,
            }
            if not node["dependencies"]:
                node["dependencies"] = ["planner.plan_ready"]
            if node["lane"] == "backend":
                backend_ids.append(str(node["task_id"]))
            elif node["lane"] == "ui":
                ui_ids.append(str(node["task_id"]))
            elif node["lane"] in {"tests", "browser_verifier"}:
                test_ids.append(str(node["task_id"]))
            nodes.append(node)
        dependency_base = backend_ids + ui_ids or ["planner.plan_ready"]
        proof_checks = sorted({check for item in items for check in cls._string_list(item.get("proof_checks"))})
        if proof_checks and not any(node.get("lane") == "tests" for node in nodes):
            proof_status = cls._aggregate_proof_status(proof_checks, items)
            nodes.append(
                {
                    "task_id": "tests.generated_regression",
                    "title": "Tests prove generated product behavior",
                    "lane": "tests",
                    "status": cls._status_from_proof_status(proof_status),
                    "dependencies": dependency_base,
                    "owner": "tests_worker",
                    "files": ["miniapp/tests/**"],
                    "artifact_refs": {"task_ledger": f"task_ledger:{run_id}"},
                    "proof": cls._merge_proof(items, proof_checks),
                    "proof_status": proof_status,
                    "proof_checks": proof_checks,
                    "blocker": None,
                    "source": "task_graph",
                    "updated_at": updated_at,
                }
            )
        browser_checks = [check for check in proof_checks if "browser" in check or "preview" in check]
        if browser_checks and not any(node.get("lane") == "browser_verifier" for node in nodes):
            proof_status = cls._aggregate_proof_status(browser_checks, items)
            nodes.append(
                {
                    "task_id": "browser_verifier.final_product_proof",
                    "title": "Browser verifier proves the working product flow",
                    "lane": "browser_verifier",
                    "status": cls._status_from_proof_status(proof_status),
                    "dependencies": dependency_base,
                    "owner": "browser_verifier",
                    "files": [],
                    "artifact_refs": {"task_ledger": f"task_ledger:{run_id}"},
                    "proof": cls._merge_proof(items, browser_checks),
                    "proof_status": proof_status,
                    "proof_checks": browser_checks,
                    "blocker": None,
                    "source": "task_graph",
                    "updated_at": updated_at,
                }
            )
        blocked_ids = [str(node.get("task_id")) for node in nodes if node.get("status") == "blocked" or node.get("blocker")]
        if blocked_ids:
            nodes.append(
                {
                    "task_id": "repair.resolve_blockers",
                    "title": "Repair blocked tasks and rerun required proof",
                    "lane": "repair",
                    "status": "in_progress" if run_status in {"blocked", "failed"} else "planned",
                    "dependencies": blocked_ids,
                    "owner": "repair_worker",
                    "files": [],
                    "artifact_refs": {"task_ledger": f"task_ledger:{run_id}"},
                    "proof": {},
                    "proof_status": "pending",
                    "repair_context": {"blocked_tasks": blocked_ids},
                    "blocker": None,
                    "source": "task_graph",
                    "updated_at": updated_at,
                }
            )
        return nodes

    @classmethod
    def _graph_node_from_raw(cls, node: dict[str, Any], *, index: int, updated_at: str | None) -> dict[str, Any]:
        task_id = str(node.get("task_id") or node.get("id") or f"graph.node.{index}")
        return {
            "task_id": task_id,
            "title": str(node.get("title") or node.get("content") or task_id),
            "lane": str(node.get("lane") or node.get("phase") or "planner"),
            "status": cls._normalize_status(str(node.get("status") or "")) or "planned",
            "dependencies": cls._string_list(node.get("dependencies") or node.get("depends_on") or node.get("requires")),
            "owner": str(node.get("owner") or "agent"),
            "role": node.get("role"),
            "files": cls._string_list(node.get("files") or node.get("owned_paths")),
            "artifact_refs": node.get("artifact_refs") if isinstance(node.get("artifact_refs"), dict) else {},
            "proof": node.get("proof") if isinstance(node.get("proof"), dict) else {},
            "proof_refs": node.get("proof_refs") if isinstance(node.get("proof_refs"), list) else [],
            "proof_status": str(node.get("proof_status") or "not_required"),
            "proof_checks": cls._string_list(node.get("proof_checks") or node.get("required_tests")),
            "blocker": node.get("blocker") or None,
            "source": "implementation_plan.task_graph",
            "updated_at": node.get("updated_at") or updated_at,
        }

    @classmethod
    def _graph_edges(cls, nodes: list[dict[str, Any]], *, explicit_edges: list[Any]) -> list[dict[str, str]]:
        edges: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        node_ids = {str(node.get("task_id")) for node in nodes}
        for edge in explicit_edges:
            if not isinstance(edge, dict):
                continue
            source = str(edge.get("from") or edge.get("source") or "")
            target = str(edge.get("to") or edge.get("target") or "")
            if source in node_ids and target in node_ids and (source, target) not in seen:
                edges.append({"from": source, "to": target, "kind": str(edge.get("kind") or "depends_on")})
                seen.add((source, target))
        for node in nodes:
            target = str(node.get("task_id") or "")
            for source in cls._string_list(node.get("dependencies")):
                if source in node_ids and target in node_ids and source != target and (source, target) not in seen:
                    edges.append({"from": source, "to": target, "kind": "depends_on"})
                    seen.add((source, target))
        return edges

    @staticmethod
    def _graph_blocker(node: dict[str, Any]) -> dict[str, Any]:
        blocker = node.get("blocker") if isinstance(node.get("blocker"), dict) else {"details": node.get("blocker")} if node.get("blocker") else {}
        return {
            "task_id": node.get("task_id"),
            "lane": node.get("lane"),
            "title": node.get("title"),
            "status": node.get("status"),
            "details": blocker.get("details") or blocker.get("message") or blocker.get("likely_cause") or "Task is blocked.",
            "proof_status": node.get("proof_status"),
        }

    @staticmethod
    def _graph_proof(nodes: list[dict[str, Any]]) -> dict[str, Any]:
        proof_nodes = [node for node in nodes if node.get("proof_checks") or node.get("proof")]
        return {
            "required_nodes": len(proof_nodes),
            "passed_nodes": sum(1 for node in proof_nodes if node.get("proof_status") in {"passed", "not_required"}),
            "missing_nodes": [node.get("task_id") for node in proof_nodes if node.get("proof_status") == "missing"],
            "failed_nodes": [node.get("task_id") for node in proof_nodes if node.get("proof_status") == "failed"],
        }

    @staticmethod
    def _proof_refs(proof: dict[str, Any]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for check, payload in proof.items():
            if not isinstance(payload, dict):
                continue
            ref = payload.get("artifact_ref") or payload.get("report_ref") or payload.get("ref")
            if ref:
                refs.append({"check": check, "ref": ref})
        return refs

    @classmethod
    def _merge_proof(cls, items: list[dict[str, Any]], checks: list[str]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for check in checks:
            for item in items:
                proof = item.get("proof") if isinstance(item.get("proof"), dict) else {}
                if check in proof:
                    merged[check] = proof[check]
                    break
            merged.setdefault(check, {"status": "missing", "details": None})
        return merged

    @classmethod
    def _aggregate_proof_status(cls, checks: list[str], items: list[dict[str, Any]]) -> str:
        proof = cls._merge_proof(items, checks)
        return cls._proof_status(checks, proof)

    @staticmethod
    def _status_from_proof_status(proof_status: str) -> str:
        if proof_status == "passed":
            return "completed"
        if proof_status == "failed":
            return "blocked"
        return "planned"

    @staticmethod
    def _lane_for_item(item: dict[str, Any], role: str) -> str:
        lane = str(item.get("lane") or "").strip()
        if lane:
            return lane
        kind = str(item.get("kind") or item.get("phase") or "").lower()
        task_id = str(item.get("id") or item.get("task_id") or "").lower()
        checks = " ".join(str(check).lower() for check in (item.get("proof_checks") or item.get("required_tests") or []))
        if "repair" in kind or "repair" in task_id:
            return "repair"
        if kind == "backend" or "api" in task_id or "shared_state" in task_id:
            return "backend"
        if "browser" in checks or kind == "proof":
            return "browser_verifier"
        if "test" in checks or "test" in kind:
            return "tests"
        if role in ROLE_ORDER or kind in {"source", "update", "observer", "participant"}:
            return "ui"
        return "planner"

    @staticmethod
    def _results_by_name(results: list[RunCheckResult | dict[str, Any]]) -> dict[str, dict[str, Any]]:
        by_name: dict[str, dict[str, Any]] = {}
        for result in results:
            if isinstance(result, RunCheckResult):
                payload = result.model_dump(mode="json")
            elif isinstance(result, dict):
                payload = dict(result)
            else:
                continue
            name = str(payload.get("name") or "").strip()
            if name:
                by_name[name] = payload
        return by_name

    @classmethod
    def _derived_status(
        cls,
        *,
        run_status: str,
        current_stage: str,
        proof_checks: list[str],
        proof: dict[str, Any],
        blocker: dict[str, Any] | str | None,
    ) -> str:
        if blocker:
            return "blocked"
        if proof_checks and proof and all(str((proof.get(check) or {}).get("status") or "") == "passed" for check in proof_checks):
            return "completed"
        if run_status in {"completed"} and not proof_checks:
            return "completed"
        if run_status in {"blocked", "failed"}:
            return "blocked"
        if run_status == "running" or current_stage not in {"", "queued"}:
            return "in_progress"
        return "planned"

    @staticmethod
    def _normalize_status(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"pending", "planned"}:
            return "planned"
        if normalized in ACTIVE_STATUSES:
            return "in_progress"
        if normalized in BLOCKED_STATUSES:
            return "blocked"
        if normalized in COMPLETED_STATUSES:
            return "completed"
        return ""

    @staticmethod
    def _proof_for_checks(proof_checks: list[str], by_check: dict[str, dict[str, Any]]) -> dict[str, Any]:
        proof: dict[str, Any] = {}
        for check in proof_checks:
            result = by_check.get(check)
            proof[check] = {
                "status": str((result or {}).get("status") or "missing"),
                "details": (result or {}).get("details"),
            }
        return proof

    @staticmethod
    def _proof_status(proof_checks: list[str], proof: dict[str, Any]) -> str:
        if not proof_checks:
            return "not_required"
        statuses = {str((proof.get(check) or {}).get("status") or "missing") for check in proof_checks}
        if statuses == {"passed"}:
            return "passed"
        if statuses & {"failed", "blocked"}:
            return "failed"
        if "missing" in statuses:
            return "missing"
        return "pending"

    @classmethod
    def _matching_blocker(cls, *, task_id: str, item: dict[str, Any], issues: list[dict[str, Any]]) -> dict[str, Any] | str | None:
        role = str(item.get("role") or "").strip().lower()
        item_id = str(item.get("id") or task_id)
        for issue in issues:
            issue_task = str(issue.get("task_id") or "")
            issue_ledger = str(issue.get("ledger_item_id") or "")
            issue_role = str(issue.get("role") or "").strip().lower()
            if issue_task == task_id or issue_ledger == item_id or (role and issue_role == role and issue.get("kind") in {"product_task_ledger", "runtime_task_ledger"}):
                return issue
        return item.get("blocker") or None

    @staticmethod
    def _owner_for_item(item: dict[str, Any], role: str) -> str:
        if role in ROLE_ORDER:
            return f"{role}_surface_worker"
        kind = str(item.get("kind") or "")
        if "api" in str(item.get("id") or "").lower() or kind == "shared_state":
            return "backend_api_worker"
        return "coordinator"

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if str(item or "").strip()]
        if isinstance(value, tuple | set):
            return [str(item) for item in value if str(item or "").strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []
