from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import time
from typing import Any, Callable, Iterable

from app.services.generation_runtime_config import ToolExecutionBatch


class GenerationToolOrchestrator:
    _PARALLEL_SAFE_TOOLS = {"list_files", "read_files", "search_files"}

    def __init__(self, *, max_concurrency: int = 8) -> None:
        self.max_concurrency = max(1, int(max_concurrency))

    @classmethod
    def is_parallel_safe(cls, request_item: dict[str, Any]) -> bool:
        tool_name = str((request_item or {}).get("tool") or "").strip().lower()
        return tool_name in cls._PARALLEL_SAFE_TOOLS

    def build_batches(self, tool_requests: Iterable[dict[str, Any]]) -> list[ToolExecutionBatch]:
        batches: list[ToolExecutionBatch] = []
        pending_parallel: list[dict[str, Any]] = []

        def flush_parallel() -> None:
            nonlocal pending_parallel
            if pending_parallel:
                batches.append(ToolExecutionBatch(kind="parallel_read", requests=list(pending_parallel)))
                pending_parallel = []

        for request_item in tool_requests:
            normalized = dict(request_item or {})
            if self.is_parallel_safe(normalized):
                pending_parallel.append(normalized)
                continue
            flush_parallel()
            batches.append(ToolExecutionBatch(kind="serial", requests=[normalized]))
        flush_parallel()
        return batches

    def execute(
        self,
        *,
        tool_requests: list[dict[str, Any]],
        run_request: Callable[[dict[str, Any]], Any],
    ) -> tuple[list[Any], int]:
        started = time.perf_counter()
        results: list[Any] = []
        for batch in self.build_batches(tool_requests):
            if batch.kind == "parallel_read" and len(batch.requests) > 1:
                max_workers = min(self.max_concurrency, len(batch.requests))
                with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="tool-batch") as executor:
                    futures = [executor.submit(run_request, request_item) for request_item in batch.requests]
                    for future in futures:
                        results.append(future.result())
                continue
            for request_item in batch.requests:
                results.append(run_request(request_item))
        return results, int((time.perf_counter() - started) * 1000)
