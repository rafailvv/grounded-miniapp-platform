from __future__ import annotations

from typing import Any

from app.models.common import GenerationMode, PreviewProfile, TargetPlatform
from app.models.grounded_spec import GroundedSpecModel
from app.modules.miniapp_generation_runtime.grounded_spec_payloads import GroundedSpecPayloadsRuntime
from app.modules.miniapp_generation_runtime.grounded_spec_prompts import GroundedSpecPromptsRuntime
from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class GroundedSpecOrchestrationRuntime(MiniappGenerationRuntimeOwner):
    def _resolve_grounded_spec(
        self,
        *,
        workspace_id: str,
        prompt: str,
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        doc_refs: list[Any],
        template_revision_id: str,
        prompt_turn_id: str,
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.service.openrouter_client.enabled:
            return {"error": "GroundedSpec generation requires OpenAI configuration."}
        if generation_mode == GenerationMode.FAST:
            return self._resolve_grounded_spec_fast(
                prompt=prompt,
                doc_refs=doc_refs,
                target_platform=target_platform,
                preview_profile=preview_profile,
                template_revision_id=template_revision_id,
                prompt_turn_id=prompt_turn_id,
                generation_mode=generation_mode,
                creative_direction=creative_direction,
            )
        try:
            outline_payload, payload, _outline = self._generate_grounded_spec_pair_with_timeout(
                timeout_seconds=float(self.service.GROUNDED_SPEC_TOTAL_TIMEOUT_SECONDS),
                workspace_id=workspace_id,
                prompt=prompt,
                doc_refs=doc_refs,
                target_platform=target_platform,
                preview_profile=preview_profile,
                template_revision_id=template_revision_id,
                prompt_turn_id=prompt_turn_id,
                creative_direction=creative_direction,
                relaxed=False,
            )
            spec = GroundedSpecModel.model_validate(GroundedSpecPayloadsRuntime._normalize_model_payload(payload["payload"]))
            model_path = [str(outline_payload["model"]), str(payload["model"])]
            return {"spec": spec, "model": payload["model"], "model_sequence": model_path}
        except Exception as strict_exc:
            if isinstance(strict_exc, TimeoutError):
                return {
                    "error": (
                        "GroundedSpec generation timed out before a valid response was produced. "
                        f"Thin grounded-spec generation timed out without a valid result: {strict_exc}"
                    )
                }
            if self.service._is_retryable_llm_error(strict_exc):
                try:
                    outline_payload, payload, _outline = self._generate_grounded_spec_pair_with_timeout(
                        timeout_seconds=float(self.service.GROUNDED_SPEC_TOTAL_TIMEOUT_SECONDS),
                        workspace_id=workspace_id,
                        prompt=prompt,
                        doc_refs=doc_refs,
                        target_platform=target_platform,
                        preview_profile=preview_profile,
                        template_revision_id=template_revision_id,
                        prompt_turn_id=prompt_turn_id,
                        creative_direction=creative_direction,
                        relaxed=False,
                        compact=True,
                    )
                    spec = GroundedSpecModel.model_validate(GroundedSpecPayloadsRuntime._normalize_model_payload(payload["payload"]))
                    model_path = [str(outline_payload["model"]), str(payload["model"])]
                    return {
                        "spec": spec,
                        "model": payload["model"],
                        "model_sequence": model_path,
                        "warning_kind": "provider_retry_recovery",
                        "warning_stage": "spec_provider_retry_recovered",
                        "warning_title": "GroundedSpec recovered after transient provider failure.",
                        "warning": f"GroundedSpec recovered after transient provider failure: {strict_exc}",
                    }
                except Exception as provider_recovery_exc:
                    strict_exc = RuntimeError(
                        "GroundedSpec strict mode failed after transient-provider recovery attempts: "
                        f"{strict_exc}; compact retry error: {provider_recovery_exc}"
                    )
            try:
                outline_payload, payload, _outline = self._generate_grounded_spec_pair_with_timeout(
                    timeout_seconds=float(self.service.GROUNDED_SPEC_TOTAL_TIMEOUT_SECONDS),
                    workspace_id=workspace_id,
                    prompt=prompt,
                    doc_refs=doc_refs,
                    target_platform=target_platform,
                    preview_profile=preview_profile,
                    template_revision_id=template_revision_id,
                    prompt_turn_id=prompt_turn_id,
                    creative_direction=creative_direction,
                    relaxed=True,
                    compact=True,
                )
                spec = GroundedSpecModel.model_validate(GroundedSpecPayloadsRuntime._normalize_model_payload(payload["payload"]))
                model_path = [str(outline_payload["model"]), str(payload["model"])]
                return {
                    "spec": spec,
                    "model": payload["model"],
                    "model_sequence": model_path,
                    "warning_kind": "relaxed_json_recovery",
                    "warning_stage": "spec_relaxed_mode_used",
                    "warning_title": "GroundedSpec used relaxed JSON recovery after strict-mode failure.",
                    "warning": f"GroundedSpec strict mode failed and relaxed JSON mode was used: {strict_exc}",
                }
            except Exception as relaxed_exc:
                return {
                    "error": (
                        "GroundedSpec generation failed: "
                        f"strict mode error: {strict_exc}; relaxed mode error: {relaxed_exc}"
                    )
                }

    def _generate_grounded_spec_pair_with_timeout(
        self,
        *,
        timeout_seconds: float,
        **kwargs: Any,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        return self.service.grounded_spec_builder.generate_grounded_spec_pair_with_timeout(
            timeout_seconds=timeout_seconds,
            submit_with_context=self.service._submit_with_context,
            generate_grounded_spec_pair=self._generate_grounded_spec_pair,
            **kwargs,
        )

    def _resolve_grounded_spec_fast(
        self,
        *,
        workspace_id: str,
        prompt: str,
        doc_refs: list[Any],
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        template_revision_id: str,
        prompt_turn_id: str,
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
    ) -> dict[str, Any]:
        return self.service.grounded_spec_builder.resolve_grounded_spec_fast(
            timeout_seconds=float(self.service.GROUNDED_SPEC_TOTAL_TIMEOUT_SECONDS),
            resolve_grounded_spec_fast_with_timeout=self.service._resolve_grounded_spec_fast_with_timeout,
            workspace_id=workspace_id,
            prompt=prompt,
            doc_refs=doc_refs,
            target_platform=target_platform,
            preview_profile=preview_profile,
            template_revision_id=template_revision_id,
            prompt_turn_id=prompt_turn_id,
            generation_mode=generation_mode,
            creative_direction=creative_direction,
        )

    def _resolve_grounded_spec_fast_with_timeout(
        self,
        *,
        timeout_seconds: float,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self.service.grounded_spec_builder.resolve_grounded_spec_fast_with_timeout(
            timeout_seconds=timeout_seconds,
            submit_with_context=self.service._submit_with_context,
            resolve_grounded_spec_fast_inner=self.service._resolve_grounded_spec_fast_inner,
            **kwargs,
        )

    def _resolve_grounded_spec_fast_inner(
        self,
        *,
        workspace_id: str | None = None,
        prompt: str,
        doc_refs: list[Any],
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        template_revision_id: str,
        prompt_turn_id: str,
        generation_mode: GenerationMode | None = None,
        creative_direction: dict[str, Any],
    ) -> dict[str, Any]:
        del workspace_id, generation_mode
        return self.service.grounded_spec_builder.resolve_grounded_spec_fast_inner(
            prompt=prompt,
            doc_refs=doc_refs,
            target_platform=target_platform,
            preview_profile=preview_profile,
            template_revision_id=template_revision_id,
            prompt_turn_id=prompt_turn_id,
            creative_direction=creative_direction,
            grounded_spec_schema=GroundedSpecModel.model_json_schema(),
            grounded_spec_system_prompt=GroundedSpecPromptsRuntime._grounded_spec_system_prompt(),
            grounded_spec_user_prompt=GroundedSpecPromptsRuntime._grounded_spec_user_prompt,
            generate_structured_with_retry=self.service._generate_structured_with_retry,
            generate_json_object_with_retry=self.service._generate_json_object_with_retry,
            normalize_model_payload=GroundedSpecPayloadsRuntime._normalize_model_payload,
        )

    def _generate_grounded_spec_pair(
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
        compact: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        return self.service.grounded_spec_builder.generate_grounded_spec_pair(
            workspace_id=workspace_id,
            prompt=prompt,
            doc_refs=doc_refs,
            target_platform=target_platform,
            preview_profile=preview_profile,
            template_revision_id=template_revision_id,
            prompt_turn_id=prompt_turn_id,
            creative_direction=creative_direction,
            relaxed=relaxed,
            compact=compact,
            grounded_spec_outline_schema=GroundedSpecPromptsRuntime._grounded_spec_outline_schema,
            grounded_spec_outline_user_prompt=GroundedSpecPromptsRuntime._grounded_spec_outline_user_prompt,
            grounded_spec_outline_system_prompt=GroundedSpecPromptsRuntime._grounded_spec_outline_system_prompt,
            grounded_spec_section_system_prompt=GroundedSpecPromptsRuntime._grounded_spec_section_system_prompt,
            grounded_spec_partial_schema=GroundedSpecPromptsRuntime._grounded_spec_partial_schema,
            grounded_spec_section_user_prompt=GroundedSpecPromptsRuntime._grounded_spec_section_user_prompt,
            generate_structured_with_retry=self.service._generate_structured_with_retry,
            generate_json_object_with_retry=self.service._generate_json_object_with_retry,
            normalize_model_payload=GroundedSpecPayloadsRuntime._normalize_model_payload,
            sanitize_grounded_spec_outline=self.service._sanitize_grounded_spec_outline,
            submit_with_context=self.service._submit_with_context,
            generate_grounded_spec_section=self._generate_grounded_spec_section,
            append_trace=self.service._append_trace,
            section_timeout_seconds=float(self.service.GROUNDED_SPEC_SECTION_TIMEOUT_SECONDS),
        )

    def _generate_grounded_spec_section(
        self,
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
    ) -> dict[str, Any]:
        return self.service.grounded_spec_builder.generate_grounded_spec_section(
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
            relaxed=relaxed,
            compact=compact,
            grounded_spec_partial_schema=GroundedSpecPromptsRuntime._grounded_spec_partial_schema,
            grounded_spec_section_user_prompt=GroundedSpecPromptsRuntime._grounded_spec_section_user_prompt,
            grounded_spec_section_system_prompt=GroundedSpecPromptsRuntime._grounded_spec_section_system_prompt,
            generate_structured_with_retry=self.service._generate_structured_with_retry,
            generate_json_object_with_retry=self.service._generate_json_object_with_retry,
        )
