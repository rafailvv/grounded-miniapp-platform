from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any

from app.models.output_artifacts import CommandOutputArtifact, HeadTailOutput, OutputArtifactIndex, OutputArtifactRef
from app.repositories.state_store import StateStore
from app.services.event_journal import EventJournalService


DEFAULT_OUTPUT_ARTIFACT_CAP_CHARS = 2_000_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _head_tail(content: str, *, max_chars: int) -> dict[str, Any]:
    text = str(content or "")
    cap = max(200, int(max_chars or 6000))
    if len(text) <= cap:
        return {
            "head": text,
            "tail": "",
            "excerpt": text,
            "total_chars": len(text),
            "omitted_chars": 0,
            "chunk_count": 1 if text else 0,
        }
    head_chars = max(100, cap // 2)
    tail_chars = max(100, cap - head_chars)
    head = text[:head_chars]
    tail = text[-tail_chars:]
    omitted = max(0, len(text) - len(head) - len(tail))
    return {
        "head": head,
        "tail": tail,
        "excerpt": f"{head}\n...[omitted {omitted} chars]...\n{tail}",
        "total_chars": len(text),
        "omitted_chars": omitted,
        "chunk_count": 1,
    }


class OutputArtifactService:
    """Persist full command/tool outputs while exposing bounded head/tail views."""

    def __init__(
        self,
        store: StateStore,
        *,
        event_journal_service: EventJournalService | None = None,
        max_content_chars: int = DEFAULT_OUTPUT_ARTIFACT_CAP_CHARS,
    ) -> None:
        self.store = store
        self.event_journal_service = event_journal_service
        self.max_content_chars = max(10_000, int(max_content_chars or DEFAULT_OUTPUT_ARTIFACT_CAP_CHARS))

    def store_command_output(
        self,
        *,
        workspace_id: str,
        run_id: str,
        process_id: str,
        stream: str,
        command: str,
        content: str,
        head_tail: dict[str, Any],
        exit_code: int | None = None,
        semantic_status: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not content:
            return None
        normalized_stream = stream if stream in {"stdout", "stderr", "combined", "tool"} else "stdout"
        raw = str(content)
        truncated_full = len(raw) > self.max_content_chars
        stored_content = raw[: self.max_content_chars]
        sha256 = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
        artifact_id = f"{process_id}:{normalized_stream}:{sha256[:24]}"
        ref = f"exec_output:{workspace_id}:{run_id}:{artifact_id}"
        head_tail_payload = {
            **dict(head_tail or {}),
            "sha256": sha256,
            "artifact_ref": ref,
            "truncated_full": truncated_full,
        }
        artifact = CommandOutputArtifact(
            ref=ref,
            artifact_id=artifact_id,
            workspace_id=workspace_id,
            run_id=run_id,
            process_id=process_id,
            stream=normalized_stream,  # type: ignore[arg-type]
            command=command,
            exit_code=exit_code,
            semantic_status=semantic_status,
            sha256=sha256,
            chars=len(raw),
            truncated_full=truncated_full,
            head_tail=HeadTailOutput.model_validate(head_tail_payload),
            content=stored_content,
            created_at=_now(),
            metadata=dict(metadata or {}),
        ).model_dump(mode="json", by_alias=True)
        self.store.upsert("reports", ref, artifact)
        summary = self._append_index(workspace_id=workspace_id, run_id=run_id, artifact=artifact)
        self._journal_created(workspace_id=workspace_id, run_id=run_id, summary=summary, process_id=process_id)
        return summary

    def store_tool_output(
        self,
        *,
        workspace_id: str,
        run_id: str,
        tool_call_id: str,
        tool: str,
        content: str,
        output_cap_chars: int = 6000,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return self.store_command_output(
            workspace_id=workspace_id,
            run_id=run_id,
            process_id=f"tool:{tool_call_id}",
            stream="tool",
            command=f"tool:{tool}",
            content=content,
            head_tail=_head_tail(content, max_chars=output_cap_chars),
            semantic_status="completed",
            metadata={
                "source": "tool_result_spill",
                "tool": tool,
                "tool_call_id": tool_call_id,
                **dict(metadata or {}),
            },
        )

    def list_run(self, run_id: str, *, workspace_id: str | None = None) -> dict[str, Any]:
        payload = self.store.get("reports", f"exec_outputs:{run_id}")
        if isinstance(payload, dict):
            return OutputArtifactIndex.model_validate(payload).model_dump(mode="json", by_alias=True)
        return OutputArtifactIndex(workspace_id=workspace_id or "", run_id=run_id).model_dump(mode="json", by_alias=True)

    def get(self, run_id: str, artifact_id: str, *, workspace_id: str | None = None) -> dict[str, Any]:
        index = self.list_run(run_id, workspace_id=workspace_id)
        for item in index.get("items") or []:
            if not isinstance(item, dict):
                continue
            if item.get("artifact_id") == artifact_id or item.get("ref") == artifact_id:
                payload = self.store.get("reports", str(item.get("ref") or ""))
                if isinstance(payload, dict):
                    return payload
        direct_ref = artifact_id if artifact_id.startswith("exec_output:") else ""
        if direct_ref:
            payload = self.store.get("reports", direct_ref)
            if isinstance(payload, dict) and payload.get("run_id") == run_id:
                return payload
        raise KeyError(f"Output artifact not found: {artifact_id}")

    def _append_index(self, *, workspace_id: str, run_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
        key = f"exec_outputs:{run_id}"
        index = self.store.get("reports", key)
        if not isinstance(index, dict):
            index = OutputArtifactIndex(workspace_id=workspace_id, run_id=run_id).model_dump(mode="json", by_alias=True)
        items = [item for item in index.get("items") or [] if isinstance(item, dict)]
        summary = OutputArtifactRef(
            ref=str(artifact["ref"]),
            artifact_id=str(artifact["artifact_id"]),
            kind="tool_output" if artifact.get("stream") == "tool" else "exec_output",
            stream=artifact["stream"],
            sha256=str(artifact["sha256"]),
            chars=int(artifact.get("chars") or 0),
            omitted_chars=int(((artifact.get("head_tail") or {}).get("omitted_chars")) or 0),
            truncated_full=bool(artifact.get("truncated_full")),
        ).model_dump(mode="json")
        if not any(item.get("ref") == summary["ref"] for item in items):
            items.append(summary)
        index["workspace_id"] = workspace_id
        index["run_id"] = run_id
        index["items"] = items[-500:]
        index["updated_at"] = _now()
        typed = OutputArtifactIndex.model_validate(index).model_dump(mode="json", by_alias=True)
        self.store.upsert("reports", key, typed)
        return summary

    def _journal_created(self, *, workspace_id: str, run_id: str, summary: dict[str, Any], process_id: str) -> None:
        if self.event_journal_service is None:
            return
        try:
            self.event_journal_service.append_run(
                workspace_id=workspace_id,
                run_id=run_id,
                event_type="output.artifact_created",
                actor="system",
                payload={"process_id": process_id, "artifact": summary},
                summary="Command output artifact created.",
                source_ref=str(summary.get("ref") or ""),
                idempotency_key=f"output.artifact_created:{summary.get('ref')}",
            )
            self.event_journal_service.append_run(
                workspace_id=workspace_id,
                run_id=run_id,
                event_type="output.head_tail_attached",
                actor="system",
                payload={
                    "process_id": process_id,
                    "artifact_ref": summary.get("ref"),
                    "stream": summary.get("stream"),
                    "chars": summary.get("chars"),
                    "omitted_chars": summary.get("omitted_chars"),
                },
                summary="Head/tail output view attached.",
                source_ref=str(summary.get("ref") or ""),
                idempotency_key=f"output.head_tail_attached:{summary.get('ref')}",
            )
        except Exception:
            pass
