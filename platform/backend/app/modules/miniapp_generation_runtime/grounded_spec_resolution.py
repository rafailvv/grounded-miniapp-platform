from __future__ import annotations

from concurrent.futures import ALL_COMPLETED, Future, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, wait
import time
from typing import Any, Callable

from app.models.common import PreviewProfile, TargetPlatform
from app.models.domain import utc_now
from app.models.grounded_spec import GroundedSpecModel


class GroundedSpecResolutionRuntime:
    def generate_grounded_spec_pair_with_timeout(
        self,
        *,
        timeout_seconds: float,
        submit_with_context: Callable[..., Future[Any]],
        generate_grounded_spec_pair: Callable[..., tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
        **kwargs: Any,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="grounded-spec-total")
        future = submit_with_context(executor, generate_grounded_spec_pair, **kwargs)
        try:
            return future.result(timeout=timeout_seconds)
        except FuturesTimeoutError as exc:
            executor.shutdown(wait=False, cancel_futures=True)
            raise TimeoutError(
                f"Timed out waiting for grounded spec generation after {int(timeout_seconds)}s."
            ) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=False)

    def resolve_grounded_spec_fast(
        self,
        *,
        timeout_seconds: float,
        resolve_grounded_spec_fast_with_timeout: Callable[..., dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            return resolve_grounded_spec_fast_with_timeout(timeout_seconds=timeout_seconds, **kwargs)
        except TimeoutError as exc:
            return {
                "error": (
                    "Fast GroundedSpec generation timed out before a valid response was produced. "
                    f"Thin grounded-spec generation timed out without a valid result: {exc}"
                )
            }

    def resolve_grounded_spec_fast_with_timeout(
        self,
        *,
        timeout_seconds: float,
        submit_with_context: Callable[..., Future[Any]],
        resolve_grounded_spec_fast_inner: Callable[..., dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="grounded-spec-fast-total")
        future = submit_with_context(executor, resolve_grounded_spec_fast_inner, **kwargs)
        try:
            return future.result(timeout=timeout_seconds)
        except FuturesTimeoutError as exc:
            executor.shutdown(wait=False, cancel_futures=True)
            raise TimeoutError(
                f"Timed out waiting for fast grounded spec generation after {int(timeout_seconds)}s."
            ) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=False)

    def resolve_grounded_spec_fast_inner(
        self,
        *,
        prompt: str,
        doc_refs: list[Any],
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        template_revision_id: str,
        prompt_turn_id: str,
        creative_direction: dict[str, Any],
        grounded_spec_schema: dict[str, Any],
        grounded_spec_system_prompt: str,
        grounded_spec_user_prompt: Callable[..., str],
        generate_structured_with_retry: Callable[..., dict[str, Any]],
        generate_json_object_with_retry: Callable[..., dict[str, Any]],
        normalize_model_payload: Callable[[Any], dict[str, Any]],
    ) -> dict[str, Any]:
        user_prompt = grounded_spec_user_prompt(
            prompt=prompt,
            doc_refs=doc_refs,
            target_platform=target_platform,
            preview_profile=preview_profile,
            template_revision_id=template_revision_id,
            prompt_turn_id=prompt_turn_id,
            creative_direction=creative_direction,
            outline={},
            compact=True,
        )
        try:
            payload = generate_structured_with_retry(
                role="spec_analysis",
                schema_name="grounded_spec_fast_v1",
                schema=grounded_spec_schema,
                system_prompt=grounded_spec_system_prompt,
                user_prompt=user_prompt,
            )
            spec = GroundedSpecModel.model_validate(normalize_model_payload(payload["payload"]))
            return {"spec": spec, "model": payload["model"], "model_sequence": [str(payload["model"])]}
        except Exception as strict_exc:
            try:
                payload = generate_json_object_with_retry(
                    role="spec_analysis",
                    schema_name="grounded_spec_fast_v1",
                    schema=grounded_spec_schema,
                    system_prompt=grounded_spec_system_prompt,
                    user_prompt=user_prompt,
                )
                spec = GroundedSpecModel.model_validate(normalize_model_payload(payload["payload"]))
                return {
                    "spec": spec,
                    "model": payload["model"],
                    "model_sequence": [str(payload["model"])],
                    "warning_kind": "fast_relaxed_json_recovery",
                    "warning_stage": "spec_relaxed_mode_used",
                    "warning_title": "Fast GroundedSpec used compact relaxed JSON recovery.",
                    "warning": f"Fast GroundedSpec strict mode failed and compact relaxed JSON mode was used: {strict_exc}",
                }
            except Exception as relaxed_exc:
                return {
                    "error": (
                        "Fast GroundedSpec generation failed: "
                        f"strict mode error: {strict_exc}; relaxed mode error: {relaxed_exc}"
                    )
                }

    def generate_grounded_spec_pair(
        self,
        *,
        workspace_id: str,
        prompt: str,
        doc_refs: list[Any],
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        template_revision_id: str,
        prompt_turn_id: str,
        creative_direction: dict[str, Any],
        relaxed: bool,
        compact: bool,
        grounded_spec_outline_schema: Callable[[], dict[str, Any]],
        grounded_spec_outline_user_prompt: Callable[..., str],
        grounded_spec_outline_system_prompt: Callable[[], str],
        grounded_spec_section_system_prompt: Callable[[str], str],
        grounded_spec_partial_schema: Callable[[list[str]], dict[str, Any]],
        grounded_spec_section_user_prompt: Callable[..., str],
        generate_structured_with_retry: Callable[..., dict[str, Any]],
        generate_json_object_with_retry: Callable[..., dict[str, Any]],
        normalize_model_payload: Callable[[Any], dict[str, Any]],
        sanitize_grounded_spec_outline: Callable[[dict[str, Any]], dict[str, Any]],
        submit_with_context: Callable[..., Future[Any]],
        generate_grounded_spec_section: Callable[..., dict[str, Any]],
        append_trace: Callable[[str, str, str, dict[str, Any]], None],
        section_timeout_seconds: float,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        outline_schema = grounded_spec_outline_schema()
        outline_user_prompt = grounded_spec_outline_user_prompt(
            prompt=prompt,
            doc_refs=doc_refs,
            target_platform=target_platform,
            preview_profile=preview_profile,
            template_revision_id=template_revision_id,
            prompt_turn_id=prompt_turn_id,
            creative_direction=creative_direction,
            compact=compact,
        )
        if relaxed:
            outline_payload = generate_json_object_with_retry(
                role="spec_analysis",
                schema_name="grounded_spec_outline_v1",
                schema=outline_schema,
                system_prompt=grounded_spec_outline_system_prompt(),
                user_prompt=outline_user_prompt,
            )
        else:
            outline_payload = generate_structured_with_retry(
                role="spec_analysis",
                schema_name="grounded_spec_outline_v1",
                schema=outline_schema,
                system_prompt=grounded_spec_outline_system_prompt(),
                user_prompt=outline_user_prompt,
            )
        outline = sanitize_grounded_spec_outline(normalize_model_payload(outline_payload["payload"]))
        core_fields = ["product_goal", "actors", "domain_entities", "user_flows"]
        requirements_fields = [
            "ui_requirements",
            "api_requirements",
            "persistence_requirements",
            "integration_requirements",
            "security_requirements",
            "platform_constraints",
            "non_functional_requirements",
        ]
        governance_fields = ["assumptions", "unknowns", "contradictions"]
        sections_started = time.perf_counter()

        executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="grounded-spec")
        futures = {
            "core": submit_with_context(
                executor,
                generate_grounded_spec_section,
                section_id="core",
                section_title="Core domain and workflow",
                field_names=core_fields,
                prompt=prompt,
                doc_refs=doc_refs,
                target_platform=target_platform,
                preview_profile=preview_profile,
                template_revision_id=template_revision_id,
                prompt_turn_id=prompt_turn_id,
                creative_direction=creative_direction,
                outline=outline,
                relaxed=relaxed,
                compact=compact,
                grounded_spec_partial_schema=grounded_spec_partial_schema,
                grounded_spec_section_user_prompt=grounded_spec_section_user_prompt,
                grounded_spec_section_system_prompt=grounded_spec_section_system_prompt,
                generate_structured_with_retry=generate_structured_with_retry,
                generate_json_object_with_retry=generate_json_object_with_retry,
            ),
            "requirements": submit_with_context(
                executor,
                generate_grounded_spec_section,
                section_id="requirements",
                section_title="Runtime requirements",
                field_names=requirements_fields,
                prompt=prompt,
                doc_refs=doc_refs,
                target_platform=target_platform,
                preview_profile=preview_profile,
                template_revision_id=template_revision_id,
                prompt_turn_id=prompt_turn_id,
                creative_direction=creative_direction,
                outline=outline,
                relaxed=relaxed,
                compact=compact,
                grounded_spec_partial_schema=grounded_spec_partial_schema,
                grounded_spec_section_user_prompt=grounded_spec_section_user_prompt,
                grounded_spec_section_system_prompt=grounded_spec_section_system_prompt,
                generate_structured_with_retry=generate_structured_with_retry,
                generate_json_object_with_retry=generate_json_object_with_retry,
            ),
            "governance": submit_with_context(
                executor,
                generate_grounded_spec_section,
                section_id="governance",
                section_title="Assumptions and gaps",
                field_names=governance_fields,
                prompt=prompt,
                doc_refs=doc_refs,
                target_platform=target_platform,
                preview_profile=preview_profile,
                template_revision_id=template_revision_id,
                prompt_turn_id=prompt_turn_id,
                creative_direction=creative_direction,
                outline=outline,
                relaxed=relaxed,
                compact=compact,
                grounded_spec_partial_schema=grounded_spec_partial_schema,
                grounded_spec_section_user_prompt=grounded_spec_section_user_prompt,
                grounded_spec_section_system_prompt=grounded_spec_section_system_prompt,
                generate_structured_with_retry=generate_structured_with_retry,
                generate_json_object_with_retry=generate_json_object_with_retry,
            ),
        }
        completed, pending = wait(set(futures.values()), timeout=section_timeout_seconds, return_when=ALL_COMPLETED)
        del completed
        section_payloads: dict[str, dict[str, Any]] = {}
        section_errors: dict[str, str] = {}
        try:
            for section_name, future in futures.items():
                if future in pending:
                    section_errors[section_name] = "timeout"
                    continue
                try:
                    section_payloads[section_name] = future.result()
                except Exception as exc:
                    section_errors[section_name] = str(exc)
            if pending:
                executor.shutdown(wait=False, cancel_futures=True)
            else:
                executor.shutdown(wait=False, cancel_futures=False)
        finally:
            if pending:
                for future in pending:
                    future.cancel()

        if section_errors:
            raise RuntimeError(
                "GroundedSpec generation returned incomplete sections without a valid agent response: "
                f"{section_errors}"
            )
        append_trace(
            workspace_id,
            "spec_sections_parallel",
            "GroundedSpec sections completed in parallel.",
            {
                "duration_ms": int((time.perf_counter() - sections_started) * 1000),
                "sections": ["core", "requirements", "governance"],
            },
        )

        core_payload_normalized = normalize_model_payload(section_payloads["core"]["payload"])
        requirements_payload_normalized = normalize_model_payload(section_payloads["requirements"]["payload"])
        governance_payload_normalized = normalize_model_payload(section_payloads["governance"]["payload"])

        merged_payload = {
            "schema_version": "1.0.0",
            "metadata": {
                "workspace_id": workspace_id,
                "conversation_id": f"conv_{workspace_id}",
                "prompt_turn_id": prompt_turn_id,
                "template_revision_id": template_revision_id,
                "language": "en",
                "created_at": utc_now().isoformat(),
            },
            "target_platform": target_platform.value,
            "doc_refs": [
                item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                for item in doc_refs
            ],
            **core_payload_normalized,
            **requirements_payload_normalized,
            **governance_payload_normalized,
        }
        payload = {
            "model": (
                section_payloads.get("governance", {}).get("model")
                or section_payloads.get("requirements", {}).get("model")
                or section_payloads.get("core", {}).get("model")
                or "grounded-spec-sections"
            ),
            "payload": merged_payload,
            "response_mode": "grounded_spec_sections",
        }
        return outline_payload, payload, outline

    @staticmethod
    def generate_grounded_spec_section(
        *,
        section_id: str,
        section_title: str,
        field_names: list[str],
        prompt: str,
        doc_refs: list[Any],
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        template_revision_id: str,
        prompt_turn_id: str,
        creative_direction: dict[str, Any],
        outline: dict[str, Any],
        relaxed: bool,
        compact: bool,
        grounded_spec_partial_schema: Callable[[list[str]], dict[str, Any]],
        grounded_spec_section_user_prompt: Callable[..., str],
        grounded_spec_section_system_prompt: Callable[[str], str],
        generate_structured_with_retry: Callable[..., dict[str, Any]],
        generate_json_object_with_retry: Callable[..., dict[str, Any]],
    ) -> dict[str, Any]:
        schema = grounded_spec_partial_schema(field_names)
        user_prompt = grounded_spec_section_user_prompt(
            section_id=section_id,
            section_title=section_title,
            field_names=field_names,
            prompt=prompt,
            doc_refs=doc_refs,
            target_platform=target_platform,
            preview_profile=preview_profile,
            template_revision_id=template_revision_id,
            prompt_turn_id=prompt_turn_id,
            creative_direction=creative_direction,
            outline=outline,
            compact=compact,
        )
        if relaxed:
            return generate_json_object_with_retry(
                role="spec_analysis",
                schema_name=f"grounded_spec_{section_id}_v1",
                schema=schema,
                system_prompt=grounded_spec_section_system_prompt(section_title),
                user_prompt=user_prompt,
            )
        return generate_structured_with_retry(
            role="spec_analysis",
            schema_name=f"grounded_spec_{section_id}_v1",
            schema=schema,
            system_prompt=grounded_spec_section_system_prompt(section_title),
            user_prompt=user_prompt,
        )
