from app.modules.miniapp_generation_runtime.grounded_spec_hygiene import GroundedSpecHygieneRuntime
from app.modules.miniapp_generation_runtime.grounded_spec_modeling import GroundedSpecModelingRuntime
from app.modules.miniapp_generation_runtime.grounded_spec_orchestration import GroundedSpecOrchestrationRuntime
from app.modules.miniapp_generation_runtime.grounded_spec_payloads import GroundedSpecPayloadsRuntime
from app.modules.miniapp_generation_runtime.grounded_spec_prompts import GroundedSpecPromptsRuntime
from app.modules.miniapp_generation_runtime.grounded_spec_resolution import GroundedSpecResolutionRuntime
from app.modules.miniapp_generation_runtime.grounded_spec_stabilization import GroundedSpecStabilizationRuntime


class MiniappGroundedSpecBuilder(
    GroundedSpecOrchestrationRuntime,
    GroundedSpecPromptsRuntime,
    GroundedSpecPayloadsRuntime,
    GroundedSpecResolutionRuntime,
    GroundedSpecModelingRuntime,
    GroundedSpecStabilizationRuntime,
    GroundedSpecHygieneRuntime,
):
    pass
