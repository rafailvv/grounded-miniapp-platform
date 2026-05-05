from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.ai.openai_client import OpenAIClient
from app.ai.model_registry import CODEX_MINI_MODEL, models_for_role
from app.models.common import GenerationMode
from app.models.domain import CheckExecutionRecord, CreateRunRequest, DraftAction, RunCheckResult, WorkspaceRecord, utc_now
from app.modules.miniapp_agent_loop.agent_file_state import AgentFileStateCache
from app.modules.miniapp_agent_loop.agent_command_policy import DEFAULT_COMMAND_POLICY, decide_workspace_command
from app.modules.miniapp_agent_loop.agent_coordinator import AgentCoordinator
from app.modules.miniapp_agent_loop.agent_hooks import AgentHookManager
from app.modules.miniapp_agent_loop.agent_kernel import agent_tool_kind, plan_agent_tool_batches
from app.modules.miniapp_agent_loop.agent_memory_store import AgentMemoryStore
from app.modules.miniapp_agent_loop.agent_process_manager import AgentProcessManager, HeadTailOutputBuffer
from app.modules.miniapp_agent_loop.agent_scratchpad import AgentScratchpad
from app.modules.miniapp_agent_loop.agent_transcript import AgentTranscriptStore
from app.modules.miniapp_agent_loop.agent_tool_registry import AgentToolRegistry
from app.modules.miniapp_agent_loop.diagnostics_delta import AgentDiagnosticsDelta
from app.modules.miniapp_agent_loop.agent_worker_manager import AgentWorkerManager
from app.modules.miniapp_agent_loop.agent_worker_branch_loop import AgentWorkerBranchLoop
from app.modules.miniapp_agent_loop.agent_worker_runtime import AgentWorkerRuntime
from app.modules.miniapp_agent_loop.agent_worker_tasks import AgentWorkerTaskPlanner
from app.models.artifacts import ApplyPatchResult
from app.modules.miniapp_agent_loop.context_pressure import AgentContextPressureAnalyzer
from app.modules.miniapp_agent_loop.edit_validator import AgentEditValidator
from app.modules.miniapp_agent_loop.repair_packets import RepairTransitionPolicy
from app.modules.miniapp_agent_loop.rollout_trace import RolloutTraceRecorder
from app.modules.miniapp_agent_loop.semantic_tools import semantic_scan
from app.modules.miniapp_agent_loop.agent_tool_runtime import validate_workspace_command
from app.modules.miniapp_agent_loop.turn_diff_tracker import AgentTurnDiffTracker
from app.modules.miniapp_agent_loop.types import AgentTurnPlan
from app.modules.miniapp_agent_loop.verification_worker import VerificationWorker
from app.modules.workspace_code_agent_runtime.browser_replay import BrowserProofReplay
from app.modules.workspace_code_agent_runtime.check_orchestrator import WorkspaceAgentCheckOrchestrator
from app.modules.workspace_code_agent_runtime.process_recovery import AgentProcessRecovery
from app.modules.workspace_code_agent_runtime.prompt_contract import agent_system_prompt
from app.modules.workspace_code_agent_runtime.runtime import WorkspaceCodeAgentRuntime
from app.services.check_runner import CheckRunner
from app.services.miniapp_contract import MiniAppContractCompiler, MiniAppContractMaterializer
from app.services.repair_catalog import RepairCatalog
from app.services.workspace.run_service import RunService
from app.services.workflow_acceptance import build_acceptance_contract, build_implementation_plan, extract_prompt_planning_hints


def _prompt_analysis(
    *,
    resource: str = "заявка",
    client: list[str] | None = None,
    specialist: list[str] | None = None,
    manager: list[str] | None = None,
    screen_plan: dict | None = None,
) -> dict:
    client_fields = client or []
    specialist_fields = specialist or []
    manager_fields = manager or []
    return {
        "prompt_summary": "structured test analysis",
        "resource_hint": resource,
        "field_hints": [*client_fields, *specialist_fields, *manager_fields],
        "role_field_hints": {
            "client": client_fields,
            "specialist": specialist_fields,
            "manager": manager_fields,
        },
        "role_action_prompts": {
            "client": ["; ".join(client_fields)] if client_fields else [],
            "specialist": ["; ".join(specialist_fields)] if specialist_fields else [],
            "manager": ["; ".join(manager_fields)] if manager_fields else [],
        },
        "routeable_screen_plan": screen_plan or {},
    }


def _contract_with_analysis(
    prompt: str,
    *,
    generation_mode: GenerationMode = GenerationMode.FAST,
    resource: str = "заявка",
    client: list[str] | None = None,
    specialist: list[str] | None = None,
    manager: list[str] | None = None,
    screen_plan: dict | None = None,
) -> dict:
    return build_acceptance_contract(
        prompt=prompt,
        intent="create",
        generation_mode=generation_mode,
        prompt_analysis=_prompt_analysis(
            resource=resource,
            client=client,
            specialist=specialist,
            manager=manager,
            screen_plan=screen_plan,
        ),
    )


def test_code_agent_defaults_to_mini_for_all_generation_modes() -> None:
    for mode in (GenerationMode.FAST, GenerationMode.BALANCED, GenerationMode.QUALITY):
        assert models_for_role("agent_turn", model_profile="", generation_mode=mode) == CODEX_MINI_MODEL
        assert models_for_role("repair", model_profile="", generation_mode=mode) == CODEX_MINI_MODEL
        assert models_for_role("summarize", model_profile="", generation_mode=mode) == CODEX_MINI_MODEL


def test_quality_create_prompt_with_error_states_stays_quality_mode() -> None:
    service = object.__new__(RunService)
    workspace = WorkspaceRecord(name="test", path="/tmp/ws")
    request = CreateRunRequest(
        prompt="Создай приложение. Quality: добавь empty/loading/error states.",
        intent="create",
        generation_mode=GenerationMode.QUALITY,
    )

    assert service._resolve_generation_mode(workspace, request, "create") == GenerationMode.QUALITY


def test_prompt_planning_hints_extract_role_fields_from_colon_and_action_sentences() -> None:
    hints = extract_prompt_planning_hints(
        "Клиент оставляет заявку: компания, офис, категория материалов, количество, бюджет, срок поставки, контакт и комментарий. "
        "Специалист видит заявку, подбирает поставщика, уточняет наличие, цену, срок доставки, замену при отсутствии и рабочий статус. "
        "Менеджер контролирует бюджет и срочность, назначает приоритет, утверждает лимит или замену, оставляет управленческий комментарий.",
        prompt_analysis=_prompt_analysis(
            client=["компания", "офис", "категория материалов", "количество", "бюджет", "срок поставки", "контакт", "комментарий"],
            specialist=["поставщика", "наличие", "цену", "срок доставки", "замену при отсутствии", "рабочий статус"],
            manager=["приоритет", "лимит", "управленческий комментарий"],
        ),
    )

    assert hints["resource_hint"] == "заявка"
    assert hints["role_field_hints"]["client"][:4] == [
        "компания",
        "офис",
        "категория материалов",
        "количество",
    ]
    assert "поставщика" in hints["role_field_hints"]["specialist"]
    assert "наличие" in hints["role_field_hints"]["specialist"]
    assert "приоритет" in hints["role_field_hints"]["manager"]
    assert "утверждает лимит или замену" not in hints["role_field_hints"]["client"]


def test_prompt_planning_hints_do_not_turn_workflow_actions_into_form_fields() -> None:
    hints = extract_prompt_planning_hints(
        "Клиент: HR-менеджер оставляет заявку на обучение: компания, подразделение, тема обучения, количество участников, бюджет, контакт. "
        "Специалист: методист обрабатывает заявку: программа, тренер, стоимость, доступные даты, рабочий статус. "
        "Менеджер: руководитель согласует: приоритет, лимит бюджета, итоговое решение. "
        "Нужно приложение с ролями /client, /specialist, /manager: создание заявки, список заявок, выбор заявки специалистом и менеджером, обновление статуса, сохранение данных после перезагрузки.",
        prompt_analysis=_prompt_analysis(
            client=["компания", "подразделение", "тема обучения", "количество участников", "бюджет", "контакт"],
            specialist=["программа", "тренер", "стоимость", "доступные даты", "рабочий статус"],
            manager=["приоритет", "лимит бюджета", "итоговое решение"],
        ),
    )

    all_fields = [
        field
        for fields in hints["role_field_hints"].values()
        for field in fields
    ]

    assert "компания" in hints["role_field_hints"]["client"]
    assert "программа" in hints["role_field_hints"]["specialist"]
    assert "приоритет" in hints["role_field_hints"]["manager"]
    assert "компания" not in hints["role_field_hints"]["manager"]
    assert "контакт" not in hints["role_field_hints"]["manager"]
    assert "создание заявки" not in all_fields
    assert "список заявок" not in all_fields
    assert "выбор заявки специалистом" not in all_fields
    assert "менеджером" not in all_fields


def test_prompt_planning_hints_skip_actor_action_and_mode_instruction_fields() -> None:
    hints = extract_prompt_planning_hints(
        "Клиент: HR-менеджер оставляет заявку на корпоративное обучение, указывает компанию, количество участников, тему обучения, желаемые даты, формат, бюджет и комментарий. "
        "Специалист: методист видит заявку, подбирает программу, тренера, длительность, стоимость, материалы и рабочий статус. "
        "Менеджер: руководитель видит все заявки и результат методиста, назначает приоритет, утверждает лимит бюджета, выбирает финальные даты и оставляет управленческий комментарий. "
        "Balanced режим: сделай дизайн лучше fast, метрики для менеджера, состояния empty/loading/error/success. "
        "Строго используй contract-owned schema/routes/field keys из miniapp/app/generated/miniapp_contract.json; не создавай English aliases и не меняй /status route.",
        prompt_analysis=_prompt_analysis(
            client=["компанию", "количество участников", "тему обучения", "желаемые даты", "формат", "бюджет", "комментарий"],
            specialist=["программу", "тренера", "длительность", "стоимость", "материалы", "рабочий статус"],
            manager=["приоритет", "лимит бюджета", "финальные даты", "управленческий комментарий"],
        ),
    )

    all_fields = {
        field
        for fields in hints["role_field_hints"].values()
        for field in fields
    }
    manager_fields = set(hints["role_field_hints"]["manager"])

    assert "HR-менеджер оставляет заявку на обучение" not in all_fields
    assert "методист видит заявку" not in all_fields
    assert "руководитель видит все заявки" not in all_fields
    assert "на обучение" not in all_fields
    assert "на корпоративное обучение" not in all_fields
    assert "компанию" in hints["role_field_hints"]["client"]
    assert "программу" in hints["role_field_hints"]["specialist"]
    assert "приоритет" in hints["role_field_hints"]["manager"]
    assert "лимит бюджета" in hints["role_field_hints"]["manager"]
    assert not any("отклоняет" in field for field in all_fields)
    assert not any("Balanced режим" in field for field in manager_fields)
    assert not any("contract-owned" in field for field in all_fields)


def test_agent_prompt_is_tool_loop_contract_not_domain_template() -> None:
    prompt = agent_system_prompt()

    assert "plan, inspect, patch the draft, run checks/browser proof" in prompt
    assert "The user prompt is the only product source" in prompt
    assert "three separate multi-page role apps" in prompt
    assert "Do not add mock data, seed data, demo data, sample data" in prompt
    assert "never create mapped attributes named metadata" in prompt
    assert "do not assert brittle implementation literals" in prompt
    assert "Never use visible technical placeholders" in prompt
    assert "Do not leave empty static/<role>/<child>/ directories" in prompt
    assert "role entries must include a root field" in prompt
    assert "return the persisted fields at the top level" in prompt
    assert "miniapp/app/generated/miniapp_contract.json" in prompt
    assert "Do not invent parallel English aliases" in prompt


def test_implementation_plan_has_prompt_derived_routeable_screen_intents() -> None:
    prompt = (
        "Я владелец пространства для мероприятий. Клиент должен выбрать формат, дату, "
        "количество гостей и дополнительные услуги, специалист должен подготовить план, "
        "обновлять статус подготовки и оставлять комментарии, менеджер должен видеть "
        "загрузку команды, выручку и позиции, где есть задержки."
    )
    screen_plan_payload = {
        "multi_page_recommended": True,
        "roles": {
            "client": [{"intent": "overview"}, {"intent": "create_or_configure"}],
            "specialist": [{"intent": "overview"}, {"intent": "detail_or_update"}],
            "manager": [{"intent": "overview"}, {"intent": "summary_or_insight"}],
        },
    }
    contract = _contract_with_analysis(
        prompt,
        generation_mode=GenerationMode.BALANCED,
        resource="мероприятие",
        client=["формат", "дату", "количество гостей", "дополнительные услуги"],
        specialist=["план", "статус подготовки", "комментарии"],
        manager=["загрузка команды", "выручка", "задержки"],
        screen_plan=screen_plan_payload,
    )
    plan = build_implementation_plan(
        prompt=prompt,
        intent="create",
        generation_mode=GenerationMode.BALANCED,
        acceptance_contract=contract,
    )

    screen_plan = plan["routeable_screen_plan"]

    assert screen_plan["multi_page_recommended"] is True
    assert screen_plan["no_fixed_page_count"] is True
    assert screen_plan["route_names_owned_by_agent"] is True
    assert any(item["intent"] == "create_or_configure" for item in screen_plan["roles"]["client"])
    assert any(item["intent"] == "detail_or_update" for item in screen_plan["roles"]["specialist"])
    assert any(item["intent"] == "summary_or_insight" for item in screen_plan["roles"]["manager"])


def test_acceptance_contract_carries_prompt_field_hints() -> None:
    prompt = "Клиент указывает компанию, адрес, дату, бюджет и комментарий. Менеджер видит выручку."

    contract = _contract_with_analysis(
        prompt,
        client=["компанию", "адрес", "дату", "бюджет", "комментарий"],
        manager=["выручку"],
    )

    assert contract["api_contract"]["field_hints"][:3] == ["компанию", "адрес", "дату"]
    assert contract["api_contract"]["role_field_hints"]["client"][:3] == ["компанию", "адрес", "дату"]
    assert "prompt_hints" in contract


def test_acceptance_contract_splits_role_owned_fields_and_resource_hint() -> None:
    prompt = (
        "Клиент создает заявку, указывает компанию, телефон и бюджет. "
        "Специалист назначает материалы, рассчитывает стоимость и добавляет комментарий цеха. "
        "Менеджер может пометить приоритет и добавить управленческий комментарий."
    )

    contract = _contract_with_analysis(
        prompt,
        client=["компанию", "телефон", "бюджет"],
        specialist=["материалы", "стоимость", "комментарий цеха"],
        manager=["приоритет", "управленческий комментарий"],
    )

    role_fields = contract["api_contract"]["role_field_hints"]
    assert role_fields["client"] == ["компанию", "телефон", "бюджет"]
    assert "материалы" in role_fields["specialist"]
    assert "управленческий комментарий" in role_fields["manager"]
    assert contract["api_contract"]["resource_hint"] == "заявка"


def test_acceptance_contract_does_not_merge_followup_visibility_sentence_into_client_fields() -> None:
    prompt = (
        "Клиент создает заявку, указывает компанию, бюджет и комментарий. "
        "Видит статус, расчет стоимости и дату визита после перезагрузки. "
        "Специалист рассчитывает стоимость и добавляет комментарий бригады."
    )

    contract = _contract_with_analysis(
        prompt,
        client=["компанию", "бюджет", "комментарий"],
        specialist=["стоимость", "комментарий бригады"],
    )

    assert contract["api_contract"]["role_field_hints"]["client"] == ["компанию", "бюджет", "комментарий"]
    assert "стоимость" in contract["api_contract"]["role_field_hints"]["specialist"]


def test_frontend_prompt_specificity_blocks_generic_scaffold(tmp_path: Path) -> None:
    prompt = (
        "Клиент создает заказ на офисный ланч, указывает компанию, адрес, дату, количество персон, "
        "бюджет, предпочтения по меню и комментарий. Специалист назначает меню, ставит статус "
        "приготовления/доставки, добавляет комментарий кухни и время готовности. Менеджер видит "
        "выручку, средний чек, проблемные задержки и управленческий комментарий."
    )
    contract = _contract_with_analysis(
        prompt,
        resource="заказ",
        client=["компания", "адрес", "дату", "количество персон", "бюджет", "предпочтения по меню", "комментарий"],
        specialist=["меню", "статус приготовления/доставки", "комментарий кухни", "время готовности"],
        manager=["выручку", "средний чек", "проблемные задержки", "управленческий комментарий"],
    )
    role_text = {
        "client": "<h1>Create Видит</h1><label>Title <input name='title'></label><label>Note <textarea name='note'></textarea></label><h2>Shared records</h2>",
        "specialist": "<h1>Process Видит</h1><button>Update status</button><h2>Shared records</h2>",
        "manager": "<h1>Review Видит</h1><button>Update status</button><h2>Shared records</h2>",
    }

    issues = CheckRunner._frontend_prompt_specificity_issues(
        source_dir=tmp_path,
        contract=contract,
        role_text=role_text,
        backend_text="class ContractItemCreate(BaseModel):\n    title: str\n    note: str\n",
    )
    codes = {issue.code for issue in issues}

    assert "platform.generic_scaffold_leakage" in codes
    assert "platform.prompt_specificity_missing_fields" in codes
    assert any(issue.repair_recipe and issue.repair_recipe["required_next_tool"] == "read_files" for issue in issues)


def test_frontend_prompt_specificity_accepts_prompt_owned_surfaces(tmp_path: Path) -> None:
    prompt = (
        "Клиент создает заказ на офисный ланч, указывает компанию, адрес, дату, количество персон, "
        "бюджет, предпочтения по меню и комментарий. Специалист назначает меню, ставит статус "
        "приготовления/доставки, добавляет комментарий кухни и время готовности. Менеджер видит "
        "выручку, средний чек, проблемные задержки, требует внимания и управленческий комментарий."
    )
    contract = _contract_with_analysis(
        prompt,
        resource="заказ",
        client=["компания", "адрес", "дата", "количество персон", "бюджет", "предпочтения по меню", "комментарий"],
        specialist=["меню", "статус приготовления", "доставка", "комментарий кухни", "время готовности"],
        manager=["выручка", "средний чек", "проблемные задержки", "требует внимания", "управленческий комментарий"],
    )
    role_text = {
        "client": "Компания Адрес Дата Количество персон Бюджет Предпочтения по меню Комментарий Офисный ланч",
        "specialist": "Назначить меню Статус приготовления Доставка Комментарий кухни Время готовности Новые заказы",
        "manager": "Выручка Средний чек Проблемные задержки Требует внимания Управленческий комментарий Заказы",
    }

    issues = CheckRunner._frontend_prompt_specificity_issues(
        source_dir=tmp_path,
        contract=contract,
        role_text=role_text,
        backend_text="company address date budget menu preferences kitchen comment revenue average check delay",
    )

    assert issues == []


def test_frontend_prompt_specificity_requires_visible_client_form_fields(tmp_path: Path) -> None:
    prompt = "Клиент указывает компанию, адрес, материалы и комментарий. Специалист обновляет статус."
    contract = _contract_with_analysis(
        prompt,
        client=["компания", "адрес", "материалы", "комментарий"],
        specialist=["статус"],
    )
    role_text = {
        "client": "Компания Адрес Материалы Комментарий const FIELD_LABELS = { materials: 'Материалы', comment: 'Комментарий' }",
        "specialist": "Статус Материалы Комментарий",
        "manager": "Статус заявки",
    }
    role_html = {
        "client": "<form><label>компания<input name='company'></label><label>адрес<input name='address'></label></form>",
        "specialist": "Статус Материалы Комментарий",
        "manager": "Статус заявки",
    }

    issues = CheckRunner._frontend_prompt_specificity_issues(
        source_dir=tmp_path,
        contract=contract,
        role_text=role_text,
        role_html_text=role_html,
        backend_text="",
    )

    assert any(issue.code == "platform.prompt_specificity_missing_fields" for issue in issues)


def test_frontend_prompt_specificity_blocks_role_fields_in_client_form(tmp_path: Path) -> None:
    prompt = (
        "Клиент создает заявку, указывает компанию, телефон и бюджет. "
        "Специалист добавляет комментарий цеха и дату готовности. "
        "Менеджер может пометить приоритет."
    )
    contract = _contract_with_analysis(
        prompt,
        client=["компания", "телефон", "бюджет"],
        specialist=["комментарий цеха", "дату готовности"],
        manager=["приоритет"],
    )
    role_text = {
        "client": "компания телефон бюджет комментарий цеха дата готовности",
        "specialist": "комментарий цеха дата готовности",
        "manager": "приоритет",
    }
    role_html = {
        "client": (
            "<form><label>компания<input name='company'></label>"
            "<label>телефон<input name='phone'></label>"
            "<label>бюджет<input name='budget'></label>"
            "<label>комментарий цеха<textarea name='workshop_comment'></textarea></label></form>"
        ),
        "specialist": "<form><label>комментарий цеха<textarea></textarea></label></form>",
        "manager": "<form><label>приоритет<select></select></label></form>",
    }

    issues = CheckRunner._frontend_prompt_specificity_issues(
        source_dir=tmp_path,
        contract=contract,
        role_text=role_text,
        role_html_text=role_html,
        backend_text="",
    )

    assert any(issue.code == "platform.prompt_specificity_cross_role_fields_in_client_form" for issue in issues)


def test_frontend_prompt_specificity_allows_client_comment_without_specialist_comment_leak(tmp_path: Path) -> None:
    prompt = (
        "Клиент создает заявку, указывает компанию, дату и комментарий. "
        "Специалист добавляет комментарий бригады и дату визита."
    )
    contract = _contract_with_analysis(
        prompt,
        client=["компания", "дата", "комментарий"],
        specialist=["комментарий бригады", "дата визита"],
    )
    role_text = {
        "client": "компания дата комментарий",
        "specialist": "комментарий бригады дата визита",
        "manager": "",
    }
    role_html = {
        "client": "<form><label>компания<input name='company'></label><label>дата<input name='date'></label><label>комментарий<textarea name='comment'></textarea></label></form>",
        "specialist": "<form><label>комментарий бригады<textarea></textarea></label><label>дата визита<input></label></form>",
        "manager": "",
    }

    issues = CheckRunner._frontend_prompt_specificity_issues(
        source_dir=tmp_path,
        contract=contract,
        role_text=role_text,
        role_html_text=role_html,
        backend_text="",
    )

    assert not any(issue.code == "platform.prompt_specificity_cross_role_fields_in_client_form" for issue in issues)


def test_frontend_prompt_specificity_does_not_confuse_delivery_term_with_supplier(tmp_path: Path) -> None:
    prompt = (
        "Клиент оставляет заявку: компания, желаемый срок поставки и комментарий. "
        "Специалист подбирает поставщика и уточняет наличие."
    )
    contract = _contract_with_analysis(
        prompt,
        client=["компания", "желаемый срок поставки", "комментарий"],
        specialist=["поставщика", "наличие"],
    )
    role_text = {
        "client": "компания желаемый срок поставки комментарий",
        "specialist": "поставщика наличие",
        "manager": "",
    }
    role_html = {
        "client": (
            "<form><label>компания<input name='company'></label>"
            "<label>желаемый срок поставки<input name='delivery'></label>"
            "<label>комментарий<textarea name='comment'></textarea></label></form>"
        ),
        "specialist": "<form><label>поставщика<input></label><label>наличие<input></label></form>",
        "manager": "",
    }

    issues = CheckRunner._frontend_prompt_specificity_issues(
        source_dir=tmp_path,
        contract=contract,
        role_text=role_text,
        role_html_text=role_html,
        backend_text="",
    )

    assert not any(issue.code == "platform.prompt_specificity_cross_role_fields_in_client_form" for issue in issues)


def test_cross_role_update_visibility_requires_client_renderer(tmp_path: Path) -> None:
    static_root = tmp_path / "miniapp/app/static"
    (static_root / "client").mkdir(parents=True)
    role_text = {
        "client": "function itemDetails(item) { return item.status + item.materials; }",
        "specialist": 'fetch("/api/items/1/status", { method: "PATCH", body: JSON.stringify({ status: "production", calculated_price: "1000", ready_date: "2026-06-05", workshop_comment: "ready" }) });',
        "manager": "",
    }

    issues = CheckRunner._cross_role_update_visibility_issues(static_root=static_root, role_text=role_text)

    assert issues
    assert issues[0].code == "platform.cross_role_update_not_rendered_in_client"
    assert set(issues[0].repair_recipe["evidence"]["missing_client_fields"]) >= {
        "calculated_price",
        "ready_date",
        "workshop_comment",
    }


def test_manager_must_render_specialist_result_fields() -> None:
    role_field_hints = {
        "specialist": ["программу", "тренера", "стоимость"],
        "manager": ["приоритет", "лимит бюджета"],
    }
    bad_manager = """
      const FIELD_LABELS = { programmu: "программу", trenera: "тренера", stoimost: "стоимость", prioritet: "приоритет" };
      function itemDetails(item) {
        const pairs = [
          ["kompaniyu", item.kompaniyu],
          ["prioritet", item.prioritet],
        ];
        return pairs.map(([key, value]) => `${FIELD_LABELS[key]}: ${value}`).join("");
      }
    """
    good_manager = """
      const FIELD_LABELS = { programmu: "программу", trenera: "тренера", stoimost: "стоимость", prioritet: "приоритет" };
      function itemDetails(item) {
        const rows = Object.entries(FIELD_LABELS).filter(([key]) => item[key]).map(([key, label]) => `${label}: ${item[key]}`);
        return rows.join("");
      }
    """

    issue = CheckRunner._manager_specialist_field_visibility_issue(
        manager_text=bad_manager,
        role_field_hints=role_field_hints,
    )
    ok = CheckRunner._manager_specialist_field_visibility_issue(
        manager_text=good_manager,
        role_field_hints=role_field_hints,
    )

    assert issue is not None
    assert issue.code == "platform.manager_missing_specialist_result_visibility"
    assert ok is None


def test_contract_materializer_manager_surface_has_dashboard_and_quality_css() -> None:
    prompt = (
        "Клиент указывает компанию, бюджет и комментарий. "
        "Специалист добавляет программу, тренера и стоимость. "
        "Менеджер видит результат специалиста, утверждает лимит и согласует заявку."
    )
    contract = MiniAppContractCompiler.compile(
        workspace_id="ws_test",
        run_id="run_test",
        prompt=prompt,
        intent="create",
        generation_mode=GenerationMode.QUALITY,
        acceptance_contract=_contract_with_analysis(
            prompt,
            generation_mode=GenerationMode.QUALITY,
            client=["компания", "бюджет", "комментарий"],
            specialist=["программа", "тренер", "стоимость"],
            manager=["лимит", "согласование"],
        ),
    )

    html = MiniAppContractMaterializer._role_html(role="manager", resource=contract.resources[0])
    js = MiniAppContractMaterializer._role_js(role="manager", resource=contract.resources[0])
    css = MiniAppContractMaterializer._role_css(role="manager", generation_mode=GenerationMode.QUALITY)

    assert set(CheckRunner._role_action_signals("manager", html + "\n" + js + "\n" + css)) == {
        "dashboard",
        "oversight_action",
    }
    assert CheckRunner._role_design_depth_issue("manager", css, html + "\n" + js, GenerationMode.QUALITY) is None
    assert "contract-metrics" in html
    assert "focus-visible" in css


def test_role_prompt_update_payload_requires_real_specialist_controls() -> None:
    prompt = (
        "Клиент создает заявку. Специалист назначает материалы, рассчитывает стоимость, "
        "ставит статус производства, добавляет комментарий цеха и дату готовности. "
        "Менеджер видит приоритет."
    )
    contract = _contract_with_analysis(
        prompt,
        client=[],
        specialist=["материалы", "стоимость", "статус производства", "комментарий цеха", "дата готовности"],
        manager=["приоритет"],
    )
    role_text = {
        "client": "",
        "specialist": 'const WORKFLOW_FIELDS = { estimated_cost: "расчет стоимости" }; fetch("/api/items/1/status", { method: "PATCH", body: JSON.stringify({ status: "processed", updated_by: ROLE }) });',
        "manager": '<select id="priority"></select> fetch("/api/items/1/status", { method: "PATCH", body: JSON.stringify({ priority: "high", management_comment: "ok", updated_by: ROLE }) });',
    }
    role_html = {
        "client": "",
        "specialist": "<button>Save progress</button>",
        "manager": "<select id='priority'></select><textarea id='management'></textarea>",
    }

    issues = CheckRunner._role_prompt_update_payload_issues(
        contract=contract,
        role_text=role_text,
        role_html_text=role_html,
    )

    assert [issue.location for issue in issues] == ["miniapp/app/static/specialist"]
    assert issues[0].code == "platform.prompt_specificity_missing_role_update_payload"


def test_role_surface_blocks_generic_workflow_copy_and_raw_status_rendering() -> None:
    technical = CheckRunner._technical_role_copy_markers(
        '<p>No workflow entries yet.</p><button type="button">Save progress</button>'
    )
    raw_status = CheckRunner._raw_status_render_markers(
        """
        function render(item) {
          return `<span>${escapeHtml(item.status || "new")}</span>`;
          return `<p><strong>Приоритет:</strong> ${escapeHtml(item.priority)}</p>`;
        }
        buildLine('Статус', item.status);
        function statusLabel(status) { return STATUS_LABELS[status] || status || "Новый"; }
        function humanStatus(status) {
          const labels = { ready: "Готово" };
          return labels[status] || status || "Новая";
        }
        """
    )

    assert "no workflow entries yet" in technical
    assert ">save progress<" in technical
    assert "template escape(item.status)" in raw_status
    assert "template item.priority raw enum" in raw_status
    assert "buildLine status=item.status" in raw_status
    assert "status label raw passthrough" in raw_status

    safe_status = CheckRunner._raw_status_render_markers(
        """
        const STATUS_LABELS = { new: "Новый", ready: "Готов" };
        function statusLabel(value) {
          return STATUS_LABELS[String(value || "").toLowerCase()] || "На обработке";
        }
        function render(item) {
          return `<span>${escapeHtml(statusLabel(item.status))}</span>`;
        }
        """
    )
    assert safe_status == []


def test_hidden_state_class_requires_effective_css_rule() -> None:
    issue = CheckRunner._hidden_state_css_issue(
        "client",
        '<div id="contract-empty" class="state hidden">Пока нет заявок</div>',
        ".state { display: grid; }",
    )
    ok = CheckRunner._hidden_state_css_issue(
        "client",
        '<div id="contract-empty" class="state hidden">Пока нет заявок</div>',
        ".hidden { display: none; }",
    )

    assert issue is not None
    assert issue.code == "platform.hidden_state_class_without_css"
    assert ok is None


def test_role_surface_blocks_visible_http_api_copy_without_blocking_js_methods() -> None:
    markers = CheckRunner._technical_visible_html_copy_markers(
        '<p>PATCH сохраняет изменения</p><p>/api/vizitas</p><p>Через защищённое API</p><p>Client</p><script>fetch("/api/vizitas", { method: "PATCH" })</script>'
    )
    js_markers = CheckRunner._technical_visible_html_copy_markers(
        '<script>fetch("/api/vizitas", { method: "PATCH" })</script>'
    )

    assert markers == ["visible HTTP method", "visible API path", "visible API term", "visible role slug"]
    assert js_markers == []


def test_role_prompt_update_payload_accepts_formdata_dynamic_patch_fields() -> None:
    prompt = (
        "Клиент создает заявку. Специалист назначает бригаду, рассчитывает стоимость, "
        "добавляет комментарий бригады и дату визита. Менеджер видит приоритет."
    )
    contract = _contract_with_analysis(
        prompt,
        client=[],
        specialist=["бригаду", "стоимость", "комментарий бригады", "дату визита"],
        manager=["приоритет"],
    )
    role_text = {
        "client": "",
        "specialist": '''
          const updateForm = document.getElementById("contract-update-form");
          updateForm.addEventListener("submit", async (event) => {
            const formData = new FormData(updateForm);
            const payload = { status: String(formData.get("status") || "processed"), updated_by: ROLE };
            for (const [key, value] of formData.entries()) {
              if (key !== "item_id" && key !== "status") payload[key] = String(value || "");
            }
            await fetch(`/api/items/${formData.get("item_id")}/status`, { method: "PATCH", body: JSON.stringify(payload) });
          });
        ''',
        "manager": '<select id="priority"></select> fetch("/api/items/1/status", { method: "PATCH", body: JSON.stringify({ priority: "high", updated_by: ROLE }) });',
    }
    role_html = {
        "client": "",
        "specialist": (
            "<form id='contract-update-form'>"
            "<select name='item_id'></select><input name='brigadu'>"
            "<input name='stoimost'><textarea name='kommentariyBrigady'></textarea>"
            "<input name='datuVizita'><select name='status'></select></form>"
        ),
        "manager": "<select id='priority'></select>",
    }

    issues = CheckRunner._role_prompt_update_payload_issues(
        contract=contract,
        role_text=role_text,
        role_html_text=role_html,
    )

    assert issues == []


def test_frontend_form_field_reads_accept_dynamic_formdata_entries_payload() -> None:
    js_source = '''
      const form = document.getElementById("contract-create-form");
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const formData = new FormData(form);
        const payload = {};
        for (const [key, value] of formData.entries()) {
          payload[key] = String(value || "");
        }
        payload.created_by = ROLE;
        await fetch(API_BASE, { method: "POST", body: JSON.stringify(payload) });
      });
    '''

    assert CheckRunner._js_reads_form_field(js_source, "kompaniya")
    assert CheckRunner._js_effective_form_payload_fields(js_source, {"kompaniya", "byudzhet"}) == {
        "kompaniya",
        "byudzhet",
        "created_by",
    }


def test_miniapp_contract_uses_prompt_fields_instead_of_items_slug() -> None:
    prompt = "Клиент указывает компанию, адрес и бюджет для корпоративного ланча."
    acceptance_contract = _contract_with_analysis(
        prompt,
        resource="корпоративный ланч",
        client=["компанию", "адрес", "бюджет для корпоративного ланча"],
    )
    implementation_plan = build_implementation_plan(
        prompt=prompt,
        intent="create",
        generation_mode=GenerationMode.FAST,
        acceptance_contract=acceptance_contract,
    )

    contract = MiniAppContractCompiler.compile(
        workspace_id="ws_test",
        run_id="run_test",
        prompt=prompt,
        intent="create",
        generation_mode=GenerationMode.FAST,
        acceptance_contract=acceptance_contract,
        implementation_plan=implementation_plan,
    )

    resource = contract.resources[0]
    assert resource.slug != "items"
    assert set(resource.field_labels.values()) >= {"компанию", "адрес", "бюджет для корпоративного ланча"}
    assert any(field.startswith("komp") for field in resource.field_labels)


def test_miniapp_contract_uses_role_scoped_fields_and_business_resource_name() -> None:
    prompt = (
        "Клиент создает заявку, указывает компанию, телефон и бюджет. "
        "Специалист добавляет комментарий цеха и дату готовности."
    )
    acceptance_contract = _contract_with_analysis(
        prompt,
        client=["компанию", "телефон", "бюджет"],
        specialist=["комментарий цеха", "дату готовности"],
    )

    contract = MiniAppContractCompiler.compile(
        workspace_id="ws_test",
        run_id="run_test",
        prompt=prompt,
        intent="create",
        generation_mode=GenerationMode.FAST,
        acceptance_contract=acceptance_contract,
    )

    resource = contract.resources[0]
    assert "zayav" in resource.slug
    assert resource.display_name == "Заявка"
    assert set(resource.role_field_labels["client"].values()) == {"компанию", "телефон", "бюджет"}
    assert "комментарий цеха" in set(resource.role_field_labels["specialist"].values())
    create_endpoint = next(endpoint for endpoint in resource.endpoints if endpoint.method == "POST")
    assert "kommentariyCeha" not in create_endpoint.request_fields


def test_repair_catalog_classifies_prompt_specificity_failure() -> None:
    packet = RepairCatalog.classify_issue(
        {
            "code": "platform.prompt_specificity_missing_fields",
            "message": "Client create flow does not expose enough prompt-derived fields.",
            "check": "frontend_interaction_static_smoke",
        }
    )

    assert packet["signature"] == "workflow.prompt_specificity_mismatch"
    assert packet["required_next_tool"] == "read_files"
    assert packet["deterministic"] is True


def test_frontend_backend_payload_validator_accepts_pydantic_extra_allow(tmp_path: Path) -> None:
    backend_text = '''
from pydantic import BaseModel, ConfigDict

class ContractItemStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str = "updated"
    updated_by: str = "manager"
'''
    schemas = CheckRunner._backend_update_schema_contracts(backend_text)
    js_path = tmp_path / "a/b/c/d/manager/app.js"
    js_path.parent.mkdir(parents=True)
    js_path.write_text("", encoding="utf-8")

    issues = CheckRunner._frontend_backend_patch_payload_issues(
        "manager",
        js_path,
        'fetch("/api/items/1/status", { method: "PATCH", body: JSON.stringify({ priority: "Высокий", updated_by: ROLE }) });',
        backend_patch_schemas=schemas,
    )

    assert schemas[0]["accepted"] >= {"*", "status", "updated_by"}
    assert issues == []


def test_platform_shell_stabilizer_adds_shell_assets_to_plain_html() -> None:
    html = (
        "<!doctype html><html><head>"
        '<link rel="stylesheet" href="/static/client/styles.css">'
        "</head><body><section>Content</section>"
        '<script defer src="/static/client/app.js"></script>'
        "</body></html>"
    )

    updated = WorkspaceCodeAgentRuntime._ensure_html_platform_shell(html)

    assert '/static/shared/base.css' in updated
    assert '/static/preview_bridge.js' in updated
    assert 'class="page-shell"' in updated
    assert "telegram-top-safe-offset" in updated


def test_tool_round_limits_allow_real_agent_inspection_cycles(monkeypatch) -> None:
    monkeypatch.delenv("WORKSPACE_AGENT_TOOL_ROUND_LIMIT", raising=False)
    monkeypatch.delenv("WORKSPACE_AGENT_FAST_TOOL_ROUND_LIMIT", raising=False)
    monkeypatch.delenv("WORKSPACE_AGENT_BALANCED_TOOL_ROUND_LIMIT", raising=False)
    monkeypatch.delenv("WORKSPACE_AGENT_QUALITY_TOOL_ROUND_LIMIT", raising=False)

    assert WorkspaceCodeAgentRuntime._tool_round_limit(GenerationMode.FAST) >= 2
    assert WorkspaceCodeAgentRuntime._tool_round_limit(GenerationMode.BALANCED) >= 4
    assert WorkspaceCodeAgentRuntime._tool_round_limit(GenerationMode.QUALITY) >= 6

    monkeypatch.setenv("WORKSPACE_AGENT_FAST_TOOL_ROUND_LIMIT", "3")
    assert WorkspaceCodeAgentRuntime._tool_round_limit(GenerationMode.FAST) == 3


def test_agent_tools_batch_reads_and_serialize_mutations() -> None:
    plan = plan_agent_tool_batches(
        [
            {"tool": "read_files", "targets": ["miniapp/app/main.py"]},
            {"tool": "semantic_scan", "targets": ["miniapp/app"]},
            {"tool": "browser_verify", "targets": ["/client"]},
            {"tool": "apply_patch_to_draft", "targets": ["miniapp/app/main.py"], "diff": "@@\n-old\n+new\n"},
            {"tool": "write_file", "targets": ["miniapp/app/static/client/app.js"], "content": "console.log(1);\n"},
        ]
    )

    assert agent_tool_kind("read_files") == "read_only"
    assert agent_tool_kind("apply_patch_to_draft") == "mutating"
    assert AgentToolRegistry.spec("browser_verify") is not None
    assert AgentToolRegistry.spec("browser_verify").kind == "verification"  # type: ignore[union-attr]
    assert [item["tool"] for item in plan.read_only_requests] == ["read_files", "semantic_scan", "browser_verify"]
    assert [item["tool"] for item in plan.mutating_requests] == ["apply_patch_to_draft", "write_file"]
    assert [[item["tool"] for item in batch.requests] for batch in plan.ordered_batches] == [
        ["read_files", "semantic_scan"],
        ["browser_verify"],
        ["apply_patch_to_draft"],
        ["write_file"],
    ]


def test_agent_registry_exposes_real_openai_tool_contract() -> None:
    tools = AgentToolRegistry.openai_tools()
    encoded = json.dumps(tools)
    names = {str(tool["name"]) for tool in tools}

    assert any(tool["name"] == "apply_patch_to_draft" for tool in tools)
    assert AgentToolRegistry.spec("browser_verify") is not None
    assert "browser_verify" not in names
    assert all("." not in name and name != "browser_verify" for name in names)
    assert ("tool_" + "requests") not in encoded
    assert all(tool["type"] == "function" for tool in tools)


def test_role_surface_issues_accepts_generation_mode(tmp_path: Path) -> None:
    css = """
    .dashboard { display: grid; }
    .card { padding: 12px; }
    .button { min-height: 44px; }
    .form { display: grid; }
    .input { border: 1px solid #ccd; }
    .list { display: grid; }
    .status { font-weight: 700; }
    .metric { font-variant-numeric: tabular-nums; }
    """
    for role in ("client", "specialist", "manager"):
        role_dir = tmp_path / "miniapp" / "app" / "static" / role
        role_dir.mkdir(parents=True)
        (role_dir / "index.html").write_text(
            f"<main><section class='dashboard'><h1>{role} workflow app</h1><button class='button'>Save</button></section></main>",
            encoding="utf-8",
        )
        (role_dir / "app.js").write_text("document.body.dataset.ready = 'true';\n", encoding="utf-8")
        (role_dir / "styles.css").write_text(css, encoding="utf-8")

    issues, coverage, _neutral = CheckRunner._role_surface_issues(tmp_path, generation_mode=GenerationMode.BALANCED)

    assert isinstance(issues, list)
    assert set(coverage) == {"client", "specialist", "manager"}


def test_openai_tool_step_extracts_response_function_calls() -> None:
    parsed = OpenAIClient._extract_response_tool_step(
        {
            "id": "resp_1",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Reading first."}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "read_files",
                    "arguments": '{"targets":["miniapp/app/main.py"],"reason":"inspect"}',
                },
            ],
        }
    )

    assert parsed["assistant_message"] == "Reading first."
    assert parsed["tool_calls"][0]["tool"] == "read_files"  # type: ignore[index]
    assert parsed["tool_calls"][0]["tool_use_id"] == "call_1"  # type: ignore[index]


def test_openai_tool_result_messages_are_model_conversation_items() -> None:
    messages = [{"tool_use_id": "call_1", "tool": "read_files", "output": {"ok": True}}]

    response_items = OpenAIClient._responses_tool_result_items(messages)
    chat_items = OpenAIClient._chat_tool_result_messages(messages)

    assert response_items == [{"type": "function_call_output", "call_id": "call_1", "output": '{"ok": true}'}]
    assert chat_items[0]["role"] == "assistant"
    assert chat_items[0]["tool_calls"][0]["id"] == "call_1"  # type: ignore[index]
    assert chat_items[1] == {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'}


def test_agent_transcript_tracks_pending_tool_results_and_reduced_graph() -> None:
    transcript = AgentTranscriptStore()
    transcript.append_model_turn(
        "run_1",
        attempt=1,
        tool_round=0,
        response_id="resp_1",
        assistant_message="Read files.",
        tool_calls=[{"tool_use_id": "call_1", "tool": "read_files", "targets": ["miniapp/app/main.py"]}],
        model="gpt-5.4-mini",
    )
    transcript.append_tool_results("run_1", [{"tool_use_id": "call_1", "tool": "read_files", "files": []}])

    context = transcript.next_model_context("run_1")
    snapshot = transcript.snapshot("run_1")

    assert context["previous_response_id"] == "resp_1"
    assert context["tool_result_messages"][0]["tool_use_id"] == "call_1"
    assert snapshot["counts"]["model_turn"] == 1
    assert snapshot["counts"]["tool_result"] == 1
    assert snapshot["reduced_graph"][0]["event_type"] == "model_turn"


def test_agent_transcript_does_not_feed_unlinked_internal_results_to_model() -> None:
    transcript = AgentTranscriptStore()

    transcript.append_tool_results("run_1", [{"tool": "internal_repair_packet", "output": {"next": "patch"}}])
    context = transcript.next_model_context("run_1")
    snapshot = transcript.snapshot("run_1")

    assert context["tool_result_messages"] == []
    assert snapshot["counts"]["tool_result_unlinked"] == 1


def test_agent_transcript_persists_and_restores_pending_tool_results() -> None:
    writes: list[dict[str, object]] = []
    transcript = AgentTranscriptStore()
    transcript.configure_persistence("run_1", writer=lambda payload: writes.append(payload))
    transcript.append_model_turn(
        "run_1",
        attempt=1,
        tool_round=0,
        response_id="resp_1",
        assistant_message="Search files.",
        tool_calls=[{"tool_use_id": "call_1", "tool": "search_files"}],
        model="gpt-5.4-mini",
    )
    transcript.append_tool_results("run_1", [{"tool_use_id": "call_1", "tool": "search_files", "matches": []}])

    restored = AgentTranscriptStore()
    restored.restore("run_1", writes[-1])
    context = restored.next_model_context("run_1")

    assert len(writes) >= 3
    assert context["previous_response_id"] == "resp_1"
    assert context["tool_result_messages"][0]["tool_use_id"] == "call_1"


def test_agent_edit_validator_rejects_unsafe_or_invalid_file_changes() -> None:
    unsafe = AgentEditValidator.normalize_plan(
        AgentTurnPlan(
            outcome="changes_ready",
            file_changes=[DraftAction(file_path="../miniapp/app/main.py", operation="replace", content="x", reason="bad")],
        )
    )
    invalid_patch = AgentEditValidator.normalize_plan(
        AgentTurnPlan(
            outcome="changes_ready",
            file_changes=[
                DraftAction(
                    file_path="miniapp/app/static/client/app.js",
                    operation="patch",
                    diff="not a unified diff",
                    reason="bad",
                )
            ],
        )
    )

    assert unsafe.failure_signature == "generation.invalid_edit_operation:unsafe_path"
    assert invalid_patch.failure_signature == "generation.invalid_edit_operation:invalid_patch_diff"
    assert unsafe.metadata["repair_packets"][0]["code"] == "unsafe_path"
    assert invalid_patch.metadata["repair_packets"][0]["required_next_tool"] in {"read_files", "write_file"}


def test_agent_file_state_cache_reports_freshness(tmp_path: Path) -> None:
    root = tmp_path
    target = root / "miniapp/app/static/client/app.js"
    target.parent.mkdir(parents=True)
    target.write_text("console.log('a');\n", encoding="utf-8")
    cache = AgentFileStateCache()

    assert cache.freshness(run_id="run_1", root=root, path="miniapp/app/static/client/app.js")["status"] == "unread"
    assert cache.read(run_id="run_1", root=root, path="miniapp/app/static/client/app.js", read_text=lambda path: (root / path).read_text(encoding="utf-8"))
    assert cache.freshness(run_id="run_1", root=root, path="miniapp/app/static/client/app.js")["status"] == "fresh"
    target.write_text("console.log('b');\n", encoding="utf-8")
    assert cache.freshness(run_id="run_1", root=root, path="miniapp/app/static/client/app.js")["status"] == "stale"


def test_repair_transition_policy_forces_read_then_write() -> None:
    packet = AgentEditValidator.repair_packet_for_issue(
        code="invalid_patch_diff",
        message="bad patch",
        file_changes=[
            DraftAction(
                file_path="miniapp/app/static/client/app.js",
                operation="patch",
                diff="bad",
                reason="test",
            )
        ],
        repeated_count=2,
    )

    read_decision = RepairTransitionPolicy.decide(
        repair_packets=[packet],
        repeated_failure_signatures={str(packet["failure_signature"]): 2},
        latest_files_read=[],
    )
    write_decision = RepairTransitionPolicy.decide(
        repair_packets=[packet],
        repeated_failure_signatures={str(packet["failure_signature"]): 2},
        latest_files_read=["miniapp/app/static/client/app.js"],
    )

    assert read_decision.active is True
    assert read_decision.forced_tool_names == ["read_files"]
    assert write_decision.forced_tool_names == ["write_file"]


def test_repair_transition_policy_allows_patch_after_non_patch_conflict_read() -> None:
    packet = AgentEditValidator.repair_packet_for_issue(
        code="old_string_not_found",
        message="old string missing",
        file_changes=[
            DraftAction(
                file_path="miniapp/app/static/client/app.js",
                operation="patch",
                diff="bad",
                reason="test",
            )
        ],
        repeated_count=2,
    )

    write_decision = RepairTransitionPolicy.decide(
        repair_packets=[packet],
        repeated_failure_signatures={str(packet["failure_signature"]): 2},
        latest_files_read=["miniapp/app/static/client/app.js"],
    )

    assert write_decision.forced_tool_names == ["write_file", "apply_patch_to_draft"]


def test_repair_catalog_returns_operational_packets() -> None:
    packet = RepairCatalog.classify_issue(
        {
            "check": "frontend_interaction_static_smoke",
            "details": "workflow_patch_payload_field_mismatch: frontend sends PATCH fields not accepted",
            "paths": ["miniapp/app/static/manager/app.js"],
        }
    )

    assert packet["signature"] == "workflow.payload_schema_mismatch"
    assert packet["required_next_tool"] == "read_files"
    assert packet["verification_command"]
    assert packet["deterministic"] is True


def test_diagnostics_delta_only_reports_changed_failures() -> None:
    previous = AgentDiagnosticsDelta.snapshot(
        [
            RunCheckResult(name="changed_files_static", status="failed", details="old", logs=["old"]),
            RunCheckResult(name="api_workflow_smoke", status="failed", details="same", logs=["same"]),
        ]
    )
    current = AgentDiagnosticsDelta.snapshot(
        [
            RunCheckResult(name="changed_files_static", status="failed", details="new", logs=["new"]),
            RunCheckResult(name="browser_flow_smoke", status="failed", details="added", logs=["added"]),
            RunCheckResult(name="api_workflow_smoke", status="failed", details="same", logs=["same"]),
        ]
    )

    delta = AgentDiagnosticsDelta.delta(previous, current)

    assert delta["status"] == "changed"
    assert [item["name"] for item in delta["added"]] == ["browser_flow_smoke"]
    assert delta["changed"][0]["current"]["name"] == "changed_files_static"


def test_safe_diagnostic_commands_are_scoped() -> None:
    assert validate_workspace_command("python -m unittest discover") is None
    assert validate_workspace_command("node --test tests/generated_app.test.mjs") is None
    assert validate_workspace_command("rg api miniapp/app") is None
    assert validate_workspace_command("npm install") is not None
    assert validate_workspace_command("rm -rf miniapp") is not None
    assert validate_workspace_command("curl https://example.com") is not None


def test_command_policy_returns_typed_decisions() -> None:
    allowed = decide_workspace_command("python -m py_compile miniapp/app/main.py")
    miniapp_cwd = decide_workspace_command("cd miniapp && node --check tests/generated_app.test.mjs")
    denied = decide_workspace_command("npm install")
    examples = DEFAULT_COMMAND_POLICY.validation_examples()

    assert allowed.allowed is True
    assert allowed.action == "allow"
    assert miniapp_cwd.allowed is True
    assert miniapp_cwd.cwd_policy == "miniapp"
    assert denied.allowed is False
    assert denied.action == "forbidden"
    assert all(item["status"] == "passed" for item in examples)


def test_process_manager_streams_head_tail_and_rg_no_match_is_success(tmp_path: Path) -> None:
    root = tmp_path
    (root / "miniapp/app").mkdir(parents=True)
    (root / "miniapp/app/main.py").write_text("print('ready')\n", encoding="utf-8")
    events: list[dict[str, object]] = []
    decision = decide_workspace_command("rg missing-token miniapp/app")

    result = AgentProcessManager().run(
        draft_source=root,
        command="rg missing-token miniapp/app",
        decision=decision,
        timeout_seconds=5,
        max_output_chars=800,
        progress_callback=lambda payload: events.append(payload),
    ).as_dict()

    assert result["exit_code"] == 1
    assert str(result["process_id"]).startswith("proc_")
    assert result["semantic_status"] == "no_matches"
    assert result["success"] is True
    assert any(event.get("status") == "started" for event in events)
    assert any(event.get("status") == "completed" for event in events)


def test_process_manager_keeps_completed_output_readable(tmp_path: Path) -> None:
    root = tmp_path
    (root / "miniapp/app").mkdir(parents=True)
    (root / "miniapp/app/main.py").write_text("print('ready')\n", encoding="utf-8")
    manager = AgentProcessManager()
    decision = decide_workspace_command("python3 -m py_compile miniapp/app/main.py")

    result = manager.run(
        draft_source=root,
        command="python3 -m py_compile miniapp/app/main.py",
        decision=decision,
        timeout_seconds=5,
        max_output_chars=800,
    ).as_dict()
    output = manager.read_output(str(result["process_id"]), stream="stderr")

    assert result["success"] is True
    assert output["process_id"] == result["process_id"]
    assert manager.snapshot()["processes"][0]["status"] == "completed"  # type: ignore[index]


def test_head_tail_output_buffer_omits_middle() -> None:
    buffer = HeadTailOutputBuffer(max_chars=20)
    buffer.append("a" * 30)
    buffer.append("b" * 30)
    snapshot = buffer.snapshot()

    assert snapshot["total_chars"] == 60
    assert snapshot["omitted_chars"] > 0
    assert "omitted" in snapshot["excerpt"]


def test_context_pressure_recommends_compaction_for_large_payload() -> None:
    pressure = AgentContextPressureAnalyzer().analyze_payload(
        {
            "file_contexts": {"miniapp/app/main.py": "x" * 45_000},
            "tool_results": [{"tool": "run_command", "stdout": "y" * 45_000}],
            "agent_memory": {"notes": "z" * 25_000},
        }
    )

    assert pressure["compact_recommended"] is True
    assert {item["kind"] for item in pressure["suggestions"]} & {"narrow_file_context", "spill_tool_results", "compact_memory"}


def test_context_pressure_detects_duplicate_reads_from_transcript() -> None:
    transcript = {
        "events": [
            {
                "event_type": "model_turn",
                "payload": {
                    "tool_calls": [
                        {"tool_use_id": "read_1", "tool": "read_files", "targets": ["miniapp/app/main.py"]},
                    ]
                },
            },
            {
                "event_type": "tool_call",
                "payload": {
                    "tool_use_id": "read_2",
                    "tool": "read_files",
                    "arguments": {"targets": ["miniapp/app/main.py"]},
                },
            },
            {
                "event_type": "tool_call",
                "payload": {
                    "tool_use_id": "read_3",
                    "tool": "read_files",
                    "arguments": {"targets": ["miniapp/app/static/client/app.js"]},
                },
            },
        ]
    }
    pressure = AgentContextPressureAnalyzer().analyze_transcript(
        transcript,
        current_file_contexts={"miniapp/app/main.py": "x" * 20_000, "miniapp/app/static/client/app.js": "y" * 80},
    )

    assert pressure["compact_recommended"] is True
    assert pressure["duplicate_file_reads"][0]["path"] == "miniapp/app/main.py"
    assert pressure["suggestions"][0]["kind"] == "avoid_duplicate_reads"


def test_hook_manager_records_lifecycle_events() -> None:
    hooks = AgentHookManager()
    hooks.record("run_1", "pre_tool_use", status="started", payload={"tool": "read_files"})
    hooks.record("run_1", "post_tool_use", status="completed", payload={"tool": "read_files"})
    snapshot = hooks.snapshot("run_1")

    assert snapshot["event_count"] == 2
    assert snapshot["counts"]["pre_tool_use:started"] == 1
    assert snapshot["counts"]["post_tool_use:completed"] == 1


def test_coordinator_scratchpad_and_memory_compact_context(tmp_path: Path) -> None:
    root = tmp_path
    (root / "miniapp/app").mkdir(parents=True)
    (root / "miniapp/app/main.py").write_text("from fastapi import FastAPI\n", encoding="utf-8")
    coordinator = AgentCoordinator(
        run_id="run_1",
        generation_mode=GenerationMode.BALANCED,
        implementation_plan={"summary": "Build a role workflow", "roles": ["client"], "primary_entities": ["item"]},
    )
    scratchpad = AgentScratchpad(run_id="run_1")
    scratchpad.set_plan({"summary": "Build a role workflow", "roles": ["client"]}, coordinator.snapshot()["todo_plan"])  # type: ignore[arg-type]
    scratchpad.record_compact_boundary(
        plan={"summary": "Build a role workflow"},
        diff_summary="miniapp/app/main.py changed",
        failed_signatures=["ui_state_not_visible"],
        next_action="patch visible state rendering",
    )
    memory = AgentMemoryStore()
    memory.add("run_1", "reference", "Check miniapp/app/main.py and /api/entities before reuse.")
    stale_checks = memory.verify_stale_claims("run_1", root)

    assert coordinator.snapshot()["worker_specs"]
    assert coordinator.verification_completed() is False
    coordinator.complete_phase("browser_verifying")
    assert coordinator.verification_completed() is True
    assert "Build a role workflow" in scratchpad.snapshot()["files"]["plan.md"]  # type: ignore[index]
    assert scratchpad.snapshot()["compact_boundaries"][0]["failed_signatures"] == ["ui_state_not_visible"]  # type: ignore[index]
    assert stale_checks[0]["paths"][0]["exists"] is True  # type: ignore[index]


def test_coordinator_todo_gate_requires_real_build_and_verification() -> None:
    coordinator = AgentCoordinator(
        run_id="run_1",
        generation_mode=GenerationMode.FAST,
        implementation_plan={"summary": "Build a generic role workflow"},
    )

    assert coordinator.ready_to_finalize() is False
    coordinator.complete_phase("planning")
    coordinator.complete_phase("reading")
    coordinator.complete_phase("editing")
    coordinator.complete_phase("checking")
    assert coordinator.ready_to_finalize() is False
    assert coordinator.incomplete_required_todos()[0]["phase"] == "browser_verifying"
    coordinator.complete_phase("browser_verifying")

    assert coordinator.ready_to_finalize() is True


def test_scratchpad_stores_next_action_as_durable_file() -> None:
    scratchpad = AgentScratchpad(run_id="run_1")
    scratchpad.set_next_action(action="repair failed selector", reason="browser_proof_failed", payload={"selector": "#save"})

    snapshot = scratchpad.snapshot()

    assert snapshot["files"]["next_action.json"]["action"] == "repair failed selector"  # type: ignore[index]
    assert snapshot["files"]["next_action.json"]["payload"] == {"selector": "#save"}  # type: ignore[index]


def test_file_state_cache_reuses_and_invalidates_reads(tmp_path: Path) -> None:
    root = tmp_path
    target = root / "miniapp/app/main.py"
    target.parent.mkdir(parents=True)
    target.write_text("one\n", encoding="utf-8")
    cache = AgentFileStateCache()

    first = cache.read(run_id="run_1", root=root, path="miniapp/app/main.py", read_text=lambda path: (root / path).read_text())
    second = cache.read(run_id="run_1", root=root, path="miniapp/app/main.py", read_text=lambda path: (root / path).read_text())
    cache.invalidate("run_1", ["miniapp/app/main.py"])
    target.write_text("two\n", encoding="utf-8")
    third = cache.read(run_id="run_1", root=root, path="miniapp/app/main.py", read_text=lambda path: (root / path).read_text())

    assert first == "one\n"
    assert second == "one\n"
    assert third == "two\n"
    assert cache.snapshot("run_1")["entry_count"] == 1


def test_turn_diff_tracker_records_changed_lines(tmp_path: Path) -> None:
    class WorkspaceStub:
        def __init__(self) -> None:
            self.content = "alpha\n"

        def try_read_text_file(self, workspace_id: str, path: str, run_id: str | None = None) -> str:
            del workspace_id, path, run_id
            return self.content

    class ApplyResult:
        status = "applied"
        conflict_reason = None

    workspace = WorkspaceStub()
    tracker = AgentTurnDiffTracker()
    tracker.capture_baseline(workspace_service=workspace, workspace_id="ws_1", run_id="run_1", turn=1, paths=["miniapp/app/main.py"])  # type: ignore[arg-type]
    workspace.content = "alpha\nbeta\n"
    record = tracker.record_result(
        workspace_service=workspace,  # type: ignore[arg-type]
        workspace_id="ws_1",
        run_id="run_1",
        turn=1,
        paths=["miniapp/app/main.py"],
        apply_result=ApplyResult(),
        owner_for_path=lambda path: "backend_api",
    )

    assert record.changed_line_counts["miniapp/app/main.py"]["added"] == 1
    assert tracker.snapshot("run_1")["turn_count"] == 1


def test_semantic_scan_extracts_generic_routes_forms_and_handlers(tmp_path: Path) -> None:
    root = tmp_path
    (root / "miniapp/app/routes").mkdir(parents=True)
    (root / "miniapp/app/static/client").mkdir(parents=True)
    (root / "miniapp/app/routes/app_api.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n@router.post('/api/entities')\ndef create_entity():\n    return {}\n",
        encoding="utf-8",
    )
    (root / "miniapp/app/static/client/index.html").write_text(
        "<form id='client-main-form'><input name='title'><button>Save</button></form><script src='app.js'></script>",
        encoding="utf-8",
    )
    (root / "miniapp/app/static/client/app.js").write_text(
        "document.querySelector('#client-main-form')?.addEventListener('submit', () => fetch('/api/entities'));\n",
        encoding="utf-8",
    )

    result = semantic_scan(root=root, targets=["miniapp/app"])

    assert result["python"][0]["routes"][0]["name"] == "create_entity"  # type: ignore[index]
    assert result["html"][0]["forms"][0]["id"] == "client-main-form"  # type: ignore[index]
    assert "#client-main-form" in result["javascript"][0]["selectors"]  # type: ignore[index]


def test_worker_manager_rejects_conflicting_owned_edits() -> None:
    report = AgentWorkerManager.validate_non_conflicting(
        [
            DraftAction(file_path="miniapp/app/static/client/app.js", operation="patch", diff="@@\n-a\n+b\n", reason="first"),
            DraftAction(file_path="miniapp/app/static/client/app.js", operation="patch", diff="@@\n-b\n+c\n", reason="second"),
        ]
    )

    assert report["ok"] is False
    assert report["conflicts"][0]["path"] == "miniapp/app/static/client/app.js"  # type: ignore[index]


def test_balanced_quality_use_serial_contract_runtime_writes() -> None:
    for mode in (GenerationMode.BALANCED, GenerationMode.QUALITY):
        mailbox = AgentWorkerManager.mailbox_for_plan(
            generation_mode=mode,
            implementation_plan={"contract_runtime_v1": {"enabled": True}},
        )
        prompt_payload = WorkspaceCodeAgentRuntime._worker_branching_prompt_payload(
            generation_mode=mode,
            implementation_plan={"contract_runtime_v1": {"enabled": True}},
        )

        assert mailbox["enabled"] is False
        assert mailbox["write_coordination"] == "serial_contract_runtime_writes"
        assert prompt_payload["enabled"] is False
        assert prompt_payload["reason"] == "serial_contract_runtime_writes"


def test_worker_task_planner_builds_self_contained_owner_prompts() -> None:
    tasks = AgentWorkerTaskPlanner.worker_tasks(
        generation_mode=GenerationMode.QUALITY,
        implementation_plan={"principle": "plan_inspect_build_verify_repair_final_browser_proof", "primary_entities": ["entity"]},
    )

    by_id = {task["worker_id"]: task for task in tasks}

    assert "client_ui" in by_id
    assert "Own only these paths" in by_id["client_ui"]["prompt"]
    assert "miniapp/app/generated/miniapp_contract.json" in by_id["client_ui"]["prompt"]
    assert "Do not introduce parallel aliases" in by_id["client_ui"]["prompt"]
    assert by_id["client_ui"]["mode_contract"]["depth"] == "deep"
    assert "mobile layout works" in by_id["client_ui"]["self_check"][3]


def test_worker_runtime_prepares_isolated_drafts_and_merge_reports(tmp_path: Path) -> None:
    source = tmp_path / "draft"
    (source / "miniapp/app/static/client").mkdir(parents=True)
    (source / "miniapp/app/static/client/app.js").write_text("console.log('ready');\n", encoding="utf-8")
    runtime = AgentWorkerRuntime()

    prepared = runtime.prepare(
        run_id="run_1",
        generation_mode=GenerationMode.BALANCED,
        draft_source=source,
        worker_specs=[
            {"worker_id": "client_ui", "owner_scope": "client role app"},
            {"worker_id": "manager_ui", "owner_scope": "manager role app"},
        ],
    )
    report = runtime.merge_report(
        "run_1",
        [
            DraftAction(file_path="miniapp/app/static/client/app.js", operation="patch", diff="@@\n-a\n+b\n", reason="one"),
            DraftAction(file_path="miniapp/app/static/client/app.js", operation="patch", diff="@@\n-b\n+c\n", reason="two"),
        ],
    )

    assert prepared["enabled"] is True
    assert len(prepared["workers"]) == 2  # type: ignore[arg-type]
    assert Path(prepared["workers"][0]["source_dir"]).exists()  # type: ignore[index]
    assert prepared["workers"][0]["agent_loop_ref"] == "worker_agent_loop:run_1:client_ui"  # type: ignore[index]
    assert report["status"] == "conflict"

    branch_results = runtime.record_branch_results(
        "run_1",
        [DraftAction(file_path="miniapp/app/static/client/app.js", operation="patch", diff="@@\n-a\n+b\n", reason="one")],
    )

    assert branch_results[0]["agent_loop"] == "branch_scoped"
    assert branch_results[0]["self_check"]["owned_paths_only"] is True  # type: ignore[index]


def test_worker_runtime_prepares_workspace_branch_run_ids(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "miniapp/app/static/client").mkdir(parents=True)
    (source / "miniapp/app/static/client/app.js").write_text("console.log('base');\n", encoding="utf-8")

    class WorkspaceStub:
        def clone_draft(self, workspace_id: str, source_run_id: str, target_run_id: str) -> Path:
            del workspace_id, source_run_id
            target = tmp_path / target_run_id
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
            return target

    runtime = AgentWorkerRuntime()
    prepared = runtime.prepare_workspace_branches(
        workspace_id="ws",
        run_id="run_main",
        generation_mode=GenerationMode.BALANCED,
        workspace_service=WorkspaceStub(),
        worker_specs=[{"worker_id": "client_ui", "owner_scope": "client role app"}],
    )

    worker = prepared["workers"][0]  # type: ignore[index]
    assert worker["branch_run_id"] == "run_main__worker__client_ui"
    assert Path(worker["source_dir"], "miniapp/app/static/client/app.js").exists()


def test_worker_branch_loop_runs_own_tool_transcript_and_patch(tmp_path: Path) -> None:
    branch_source = tmp_path / "branch"
    (branch_source / "miniapp/app/static/client").mkdir(parents=True)
    (branch_source / "miniapp/app/static/client/app.js").write_text("console.log('base');\n", encoding="utf-8")
    (branch_source / "miniapp/app/static/client/index.html").write_text(
        "<main><form id='main-form'><button type='submit'>Save</button></form></main>",
        encoding="utf-8",
    )
    (branch_source / "miniapp/app/static/client/styles.css").write_text(
        ".page{display:block}.card{padding:12px}.button{min-height:44px}\n",
        encoding="utf-8",
    )
    for slug in ("list", "detail"):
        page_dir = branch_source / "miniapp/app/static/client" / slug
        page_dir.mkdir(parents=True)
        (page_dir / "index.html").write_text("<main><button>Open</button></main>", encoding="utf-8")

    class OpenAIStub:
        def generate_agent_tool_step(self, **_: object) -> dict[str, object]:
            return {
                "model": "test-model",
                "payload": {
                    "assistant_message": "client branch edited",
                    "response_id": "resp_worker_1",
                    "tool_calls": [
                        {
                            "tool": "write_file",
                            "tool_use_id": "call_1",
                            "file_path": "miniapp/app/static/client/app.js",
                            "content": (
                                "document.querySelector('#main-form')?.addEventListener('submit', event => {"
                                "event.preventDefault(); fetch('/api/entities', { method: 'POST', body: '{}' }); });\n"
                            ),
                            "reason": "create owned client behavior",
                        }
                    ],
                },
                "cache_stats": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
            }

    class WorkspaceStub:
        def __init__(self) -> None:
            self.written: list[str] = []

        def file_tree(self, workspace_id: str, run_id: str | None = None) -> list[dict[str, str]]:
            del workspace_id, run_id
            return [{"path": "miniapp/app/static/client/app.js", "type": "file"}]

        def try_read_text_file(self, workspace_id: str, relative_path: str, run_id: str | None = None) -> str | None:
            del workspace_id, run_id
            path = branch_source / relative_path
            return path.read_text(encoding="utf-8") if path.exists() else None

        def diff(self, workspace_id: str, run_id: str | None = None) -> str:
            del workspace_id, run_id
            return "\n".join(f"diff --git a/{path} b/{path}" for path in self.written)

        def build_patch_envelope_for_file_changes(self, workspace_id: str, run_id: str, file_changes: list[DraftAction]) -> list[DraftAction]:
            del workspace_id, run_id
            return file_changes

        def apply_patch_envelope_to_draft(self, workspace_id: str, run_id: str, envelope: list[DraftAction]) -> ApplyPatchResult:
            del run_id
            for change in envelope:
                target = branch_source / change.file_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(change.content or ""), encoding="utf-8")
                self.written.append(change.file_path)
            return ApplyPatchResult(workspace_id=workspace_id, run_id="branch", status="applied", changed_files=list(self.written))

    workspace = WorkspaceStub()
    result = AgentWorkerBranchLoop(openai_client=OpenAIStub(), workspace_service=workspace).run(  # type: ignore[arg-type]
        workspace_id="ws",
        parent_run_id="run_main",
        branch_run_id="run_main__worker__client_ui",
        branch_source=branch_source,
        generation_mode=GenerationMode.FAST,
        model_profile="",
        user_prompt="Build a role-separated mobile mini-app.",
        worker_task={"worker_id": "client_ui", "owner_scope": "client role app"},
        worker_prefix={"plan": "generic"},
        max_steps=1,
    )

    assert result.status == "changes_ready"
    assert result.transcript["counts"]["model_turn"] == 1
    assert result.transcript["counts"]["file_change"] == 1
    assert result.changed_files == ["miniapp/app/static/client/app.js"]
    assert "method: 'POST'" in (branch_source / "miniapp/app/static/client/app.js").read_text(encoding="utf-8")


def test_verification_worker_requires_real_browser_proof() -> None:
    report = VerificationWorker.verify(
        latest_execution=None,
        preview_details={},
        acceptance_contract={"required": True},
        require_browser_proof=True,
    ).model_dump()

    assert report["status"] == "failed"
    assert any(issue["kind"] == "missing_required_proof" for issue in report["issues"])  # type: ignore[index]


def test_verification_worker_rejects_incomplete_browser_proof() -> None:
    execution = CheckExecutionRecord(
        workspace_id="ws_1",
        run_id="run_1",
        changed_files=[],
        results=[
            RunCheckResult(name="api_workflow_smoke", status="passed", details="ok", command="api"),
            RunCheckResult(
                name="browser_flow_smoke",
                status="passed",
                details="ok",
                command="browser",
                diagnostics={"roles_checked": ["client"], "ui_steps": []},
            ),
        ],
        started_at=utc_now(),
        completed_at=utc_now(),
        duration_ms=1,
    )

    report = VerificationWorker.verify(
        latest_execution=execution,
        preview_details={},
        acceptance_contract={"required": True},
        require_browser_proof=True,
    ).model_dump()

    issue_kinds = {issue["kind"] for issue in report["issues"]}  # type: ignore[index]
    assert report["status"] == "failed"
    assert "browser_proof_missing_roles" in issue_kinds
    assert "browser_proof_missing_created_marker" in issue_kinds


def test_check_orchestrator_selects_full_create_gate() -> None:
    plan = WorkspaceAgentCheckOrchestrator.plan(
        focused_visual_edit=False,
        create_intent=True,
        acceptance_required=True,
        generation_mode=GenerationMode.FAST,
        has_draft_diff=True,
    )

    assert plan.scope_mode == "agentic"
    assert plan.check_profile == "full"
    assert plan.check_attempt == 1


def test_browser_replay_extracts_failed_step_packet() -> None:
    execution = CheckExecutionRecord(
        workspace_id="ws_1",
        run_id="run_1",
        changed_files=[],
        results=[
            RunCheckResult(
                name="browser_flow_smoke",
                status="failed",
                details="failed",
                command="browser",
                logs=["button did not update state"],
                diagnostics={
                    "failed_step": "client_submit",
                    "failed_selector": "#main-form",
                    "failed_route": "/client",
                    "console_errors": ["ReferenceError"],
                },
            )
        ],
        started_at=utc_now(),
        completed_at=utc_now(),
        duration_ms=1,
    )

    packet = BrowserProofReplay.failed_step_packet(execution)

    assert packet is not None
    assert packet["failed_step"] == "client_submit"
    assert BrowserProofReplay.should_rerun_step_first(packet) is True


def test_process_recovery_marks_running_processes_stale() -> None:
    checkpoint = {
        "process_summary": {
            "active_processes": [
                {"process_id": "proc_1", "command": "rg token miniapp/app", "status": "running"},
            ]
        }
    }

    restored = AgentProcessRecovery.restore_view(checkpoint)
    process_checkpoint = AgentProcessRecovery.checkpoint({"active_processes": [], "processes": [{"process_id": "proc_2"}]})

    assert restored["stale_processes"][0]["status"] == "stale_after_restart"  # type: ignore[index]
    assert process_checkpoint["active_count"] == 0


def test_rollout_trace_reduces_tool_events() -> None:
    trace = RolloutTraceRecorder()
    trace.append("run_1", "tool_batch", {"tool": "read_files"})
    trace.append("run_1", "tool_batch", {"tool": "run_checks"})
    snapshot = trace.snapshot("run_1")

    assert snapshot["event_count"] == 2
    assert snapshot["tool_counts"] == {"read_files": 1, "run_checks": 1}
