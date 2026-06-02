from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable


SECRET_PATTERNS = (
    re.compile(r"(api[_-]?key|token|secret|password)\s*=", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    except TypeError:
        return str(value)


def _head_tail(content: str, *, max_chars: int) -> dict[str, Any]:
    text = str(content or "")
    cap = max(200, int(max_chars or 1200))
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


def _secret_like(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def _dedupe_strings(values: list[Any], *, limit: int = 40) -> list[str]:
    items: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in items:
            items.append(text)
    return items[:limit]


class ToolResultSummarizer:
    """Canonical model-visible tool result boundary."""

    @classmethod
    def summarize(
        cls,
        *,
        workspace_id: str,
        run_id: str,
        canonical_tool: str,
        model_tool: str,
        tool_call_id: str,
        result: dict[str, Any],
        output_cap_chars: int,
        artifact_spill_policy: str,
        result_summarization: dict[str, Any] | None = None,
        output_spill_writer: Callable[[str, dict[str, Any]], dict[str, Any] | None] | None = None,
        output_artifact_writer: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        policy = dict(result_summarization or {})
        max_inline = max(200, int(policy.get("max_inline_chars") or min(output_cap_chars or 1200, 1200)))
        spill_threshold = max(200, int(output_cap_chars or max_inline))
        encoded = _json_text(result)
        digest = hashlib.sha256(encoded.encode("utf-8", errors="replace")).hexdigest()
        original_chars = len(encoded)
        is_large = original_chars > spill_threshold
        secret_like = _secret_like(encoded)
        artifact_policy = str(artifact_spill_policy or "on_truncation")
        policy_spill = str(policy.get("spill_full_result") or "on_truncation")
        should_spill = (
            not secret_like
            and artifact_policy != "never"
            and (artifact_policy == "always" or policy_spill in {"always", "always_for_large_output"} or is_large)
        )
        artifacts: list[dict[str, Any]] = []
        if should_spill:
            artifact = cls._write_artifact(
                workspace_id=workspace_id,
                run_id=run_id,
                canonical_tool=canonical_tool,
                model_tool=model_tool,
                tool_call_id=tool_call_id,
                digest=digest,
                encoded=encoded,
                output_cap_chars=max_inline,
                output_spill_writer=output_spill_writer,
                output_artifact_writer=output_artifact_writer,
            )
            if artifact:
                artifacts.append(cls._artifact_ref(artifact))
        artifact_ref = str(artifacts[0].get("ref") or "") if artifacts else ""
        omitted_chars = max(0, original_chars - min(original_chars, max_inline))
        summary = cls._summary(
            canonical_tool=canonical_tool,
            model_tool=model_tool,
            tool_call_id=tool_call_id,
            result=result,
            policy=policy,
            digest=digest,
            original_chars=original_chars,
            inline_chars=0 if is_large or secret_like else original_chars,
            omitted_chars=omitted_chars if is_large or secret_like else 0,
            artifact_ref=artifact_ref,
            artifacts=artifacts,
            secret_redacted=secret_like,
        )
        if is_large or secret_like:
            compacted = {
                "schema": "grounded.tool_result_compact.v1",
                "tool": str(result.get("tool") or model_tool or canonical_tool),
                "tool_use_id": tool_call_id,
                "status": str(result.get("status") or result.get("outcome") or "completed"),
                "result_summary": summary,
                "artifact_ref": artifact_ref or None,
                "artifacts": artifacts,
                "sha256": digest,
                "original_chars": original_chars,
                "omitted_chars": omitted_chars,
                "has_more": bool(artifact_ref),
            }
            if secret_like:
                compacted["excerpt"] = "[tool result omitted: secret-like material detected]"
                compacted["secret_redacted"] = True
            else:
                compacted["excerpt"] = _head_tail(encoded, max_chars=max_inline)["excerpt"]
            truncation = {
                "truncated": True,
                "sha256": digest,
                "original_chars": original_chars,
                "inline_chars": len(str(compacted.get("excerpt") or "")),
                "omitted_chars": omitted_chars,
                "artifact_ref": artifact_ref,
                "spilled": bool(artifact_ref),
                "spill_policy": artifact_policy,
                "summarized": True,
                "secret_redacted": secret_like,
            }
            return {"result": compacted, "summary": summary, "truncation": truncation, "artifacts": artifacts}
        enriched = dict(result)
        enriched["result_summary"] = summary
        if artifacts:
            enriched["artifact_ref"] = artifact_ref
            enriched["artifacts"] = artifacts
        return {
            "result": enriched,
            "summary": summary,
            "truncation": {
                "truncated": False,
                "sha256": digest,
                "original_chars": original_chars,
                "inline_chars": original_chars,
                "omitted_chars": 0,
                "artifact_ref": artifact_ref,
                "spilled": bool(artifact_ref),
                "spill_policy": artifact_policy,
                "summarized": True,
            },
            "artifacts": artifacts,
        }

    @classmethod
    def compact_for_context(
        cls,
        *,
        tool_result: dict[str, Any],
        max_inline_chars: int = 1200,
    ) -> dict[str, Any]:
        encoded = _json_text(tool_result)
        digest = hashlib.sha256(encoded.encode("utf-8", errors="replace")).hexdigest()
        secret_like = _secret_like(encoded)
        tool = str(tool_result.get("tool") or tool_result.get("name") or tool_result.get("type") or "")
        tool_use_id = str(tool_result.get("tool_use_id") or tool_result.get("call_id") or tool_result.get("id") or "")
        summary = cls._summary(
            canonical_tool=tool,
            model_tool=tool,
            tool_call_id=tool_use_id,
            result=tool_result,
            policy={"mode": "transcript_summary", "max_inline_chars": max_inline_chars},
            digest=digest,
            original_chars=len(encoded),
            inline_chars=0 if secret_like else min(len(encoded), max_inline_chars),
            omitted_chars=max(0, len(encoded) - max_inline_chars),
            artifact_ref=str(tool_result.get("artifact_ref") or tool_result.get("microcompact_ref") or ""),
            artifacts=[item for item in tool_result.get("artifacts") or [] if isinstance(item, dict)] if isinstance(tool_result.get("artifacts"), list) else [],
            secret_redacted=secret_like,
        )
        excerpt = "[tool result omitted: secret-like material detected]" if secret_like else _head_tail(encoded, max_chars=max_inline_chars)["excerpt"]
        return {
            "schema": "grounded.tool_result_compact.v1",
            "tool": tool,
            "tool_use_id": tool_use_id,
            "status": str(tool_result.get("status") or tool_result.get("outcome") or "completed"),
            "result_summary": summary,
            "sha256": digest,
            "digest": digest,
            "original_chars": len(encoded),
            "omitted_chars": max(0, len(encoded) - len(excerpt)),
            "artifact_ref": summary.get("artifact_ref") or None,
            "microcompact_ref": tool_result.get("microcompact_ref"),
            "excerpt": excerpt,
            "secret_redacted": secret_like,
            "has_more": bool(summary.get("artifact_ref") or tool_result.get("microcompact_ref")),
        }

    @classmethod
    def _summary(
        cls,
        *,
        canonical_tool: str,
        model_tool: str,
        tool_call_id: str,
        result: dict[str, Any],
        policy: dict[str, Any],
        digest: str,
        original_chars: int,
        inline_chars: int,
        omitted_chars: int,
        artifact_ref: str,
        artifacts: list[dict[str, Any]],
        secret_redacted: bool,
    ) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for key, value in result.items():
            if isinstance(value, list):
                counts[f"{key}_count"] = len(value)
            elif isinstance(value, dict):
                counts[f"{key}_keys"] = len(value)
        failed_checks = result.get("failed_checks") if isinstance(result.get("failed_checks"), list) else []
        workflow_results = result.get("workflow_results") if isinstance(result.get("workflow_results"), list) else []
        changed_files = result.get("changed_files") if isinstance(result.get("changed_files"), list) else []
        output_artifacts = result.get("output_artifacts") if isinstance(result.get("output_artifacts"), list) else []
        refs = [
            result.get("artifact_ref"),
            result.get("microcompact_ref"),
            result.get("stdout_ref"),
            result.get("stderr_ref"),
            artifact_ref,
            *[item.get("ref") for item in artifacts if isinstance(item, dict)],
            *[item.get("ref") for item in output_artifacts if isinstance(item, dict)],
        ]
        return {
            "schema": "grounded.tool_result_summary.v1",
            "tool": canonical_tool,
            "model_tool": model_tool,
            "tool_call_id": tool_call_id,
            "status": str(result.get("status") or result.get("outcome") or "completed"),
            "mode": str(policy.get("mode") or "structured_summary"),
            "policy": policy,
            "result_keys": sorted(str(key) for key in result.keys())[:32],
            "counts": counts,
            "changed_files": _dedupe_strings(changed_files),
            "failed_checks": failed_checks[:12],
            "workflow_results": workflow_results[:12],
            "semantic_status": result.get("semantic_status"),
            "exit_code": result.get("exit_code"),
            "command_canonical": result.get("command_canonical") if isinstance(result.get("command_canonical"), dict) else {},
            "execution_classification": result.get("execution_classification") if isinstance(result.get("execution_classification"), dict) else {},
            "failure_signature": result.get("failure_signature"),
            "sha256": digest,
            "digest": digest,
            "original_chars": original_chars,
            "inline_chars": inline_chars,
            "omitted_chars": omitted_chars,
            "artifact_ref": artifact_ref or None,
            "artifact_refs": _dedupe_strings(refs),
            "artifact_count": len(artifacts) + len(output_artifacts),
            "secret_redacted": secret_redacted,
            "summarized": True,
        }

    @staticmethod
    def _write_artifact(
        *,
        workspace_id: str,
        run_id: str,
        canonical_tool: str,
        model_tool: str,
        tool_call_id: str,
        digest: str,
        encoded: str,
        output_cap_chars: int,
        output_spill_writer: Callable[[str, dict[str, Any]], dict[str, Any] | None] | None,
        output_artifact_writer: Callable[[dict[str, Any]], dict[str, Any] | None] | None,
    ) -> dict[str, Any] | None:
        if output_spill_writer is not None:
            return output_spill_writer(
                f"tool-output:{run_id}:{tool_call_id}:{digest[:12]}",
                {"tool": canonical_tool, "model_tool": model_tool, "tool_call_id": tool_call_id, "sha256": digest, "result_json": encoded},
            )
        if output_artifact_writer is None:
            return None
        return output_artifact_writer(
            {
                "workspace_id": workspace_id,
                "run_id": run_id,
                "process_id": f"tool:{tool_call_id}",
                "stream": "tool",
                "command": f"tool:{canonical_tool}",
                "content": encoded,
                "head_tail": _head_tail(encoded, max_chars=output_cap_chars),
                "semantic_status": "completed",
                "metadata": {
                    "source": "tool_result_summarizer",
                    "tool": canonical_tool,
                    "model_tool": model_tool,
                    "tool_call_id": tool_call_id,
                    "sha256": digest,
                },
            }
        )

    @staticmethod
    def _artifact_ref(artifact: dict[str, Any]) -> dict[str, Any]:
        return {
            **artifact,
            "kind": "tool_result",
            "mime_type": "application/json",
            "label": "Full tool result",
        }
