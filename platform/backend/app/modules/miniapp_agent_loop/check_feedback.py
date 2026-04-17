from __future__ import annotations

from typing import Any

from app.models.domain import ValidationSnapshot


class WorkspaceLoopCheckFeedback:
    @staticmethod
    def progress_snapshot(
        results: list[Any],
        preview_details: dict[str, Any],
        validation_snapshot: ValidationSnapshot | None,
    ) -> dict[str, Any]:
        failed_results = [result for result in results if getattr(result, "status", None) == "failed"]
        failed_checks = sorted(str(getattr(result, "name", "")) for result in failed_results)
        details_markers = sorted(
            {
                f"{getattr(result, 'name', '')}:{str(getattr(result, 'details', '') or '').strip()[:180]}"
                for result in failed_results
            }
        )
        preview_status = str(preview_details.get("status") or "")
        blocking_validation = bool(validation_snapshot.blocking) if validation_snapshot is not None else False
        failure_summary = " | ".join(marker for marker in details_markers[:4] if marker)
        failure_class = failed_checks[0] if failed_checks else None
        return {
            "failed_checks": failed_checks,
            "details_markers": details_markers,
            "preview_status": preview_status,
            "blocking_validation": blocking_validation,
            "failed_count": len(failed_checks),
            "failure_summary": failure_summary or None,
            "failure_class": failure_class,
        }

    @staticmethod
    def is_progress(previous: dict[str, Any], current: dict[str, Any]) -> bool:
        if current["failed_count"] < previous["failed_count"]:
            return True
        if len(current["details_markers"]) < len(previous["details_markers"]):
            return True
        if previous["preview_status"] != "running" and current["preview_status"] == "running":
            return True
        if previous["blocking_validation"] and not current["blocking_validation"]:
            return True
        return False

    @staticmethod
    def progress_signature(snapshot: dict[str, Any]) -> str:
        signature = "|".join(snapshot.get("details_markers") or snapshot.get("failed_checks") or [])
        return signature or "workspace_loop_failure"

