from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from app.core.config import Settings
from app.models.common import GenerationMode, PreviewProfile, TargetPlatform
from app.models.domain import PreviewRecord, RunChecksSummary, RunRecord, WorkspaceRecord
from app.repositories.state_store import StateStore
from app.services.workspace.log_service import WorkspaceLogService
from app.services.workspace.service import WorkspaceService


BLOOM_STARTER_WORKSPACE_ID = "ws_dcc936bc3e6a40c0b7811a7d148bf5f3"
BLOOM_STARTER_NAME = "Bloom Atelier - цветочный магазин"
BLOOM_STARTER_DESCRIPTION = "Telegram-first flower shop with client, florist, and manager roles."


class StarterWorkspaceService:
    def __init__(
        self,
        *,
        settings: Settings,
        store: StateStore,
        workspace_service: WorkspaceService,
        workspace_log_service: WorkspaceLogService,
    ) -> None:
        self.settings = settings
        self.store = store
        self.workspace_service = workspace_service
        self.workspace_log_service = workspace_log_service

    def ensure_default_workspace(self) -> WorkspaceRecord | None:
        if self.store.list("workspaces"):
            return None

        snapshot_root = self.settings.runtime_dir / "starter-workspaces" / "bloom-atelier"
        snapshot_source_dir = snapshot_root / "source"
        if not snapshot_source_dir.exists():
            return None

        workspace = WorkspaceRecord(
            workspace_id=BLOOM_STARTER_WORKSPACE_ID,
            name=BLOOM_STARTER_NAME,
            description=BLOOM_STARTER_DESCRIPTION,
            target_platform=TargetPlatform.TELEGRAM,
            preview_profile=PreviewProfile.TELEGRAM_MOCK,
            path=str(self.settings.workspaces_dir / BLOOM_STARTER_WORKSPACE_ID),
        )
        workspace = self.workspace_service.install_workspace_snapshot(
            workspace,
            snapshot_source_dir,
            revision_message="Install Bloom Atelier starter workspace",
        )
        self._install_runs(workspace)
        self._install_logs(snapshot_root)
        self._install_preview()
        return workspace

    def _install_runs(self, workspace: WorkspaceRecord) -> None:
        for run in _starter_runs(workspace.workspace_id):
            if workspace.current_revision_id:
                run.result_revision_id = workspace.current_revision_id
            self.store.upsert("runs", run.run_id, run.model_dump(mode="json"))

    def _install_logs(self, snapshot_root: Path) -> None:
        self.workspace_log_service.ensure_log_files(BLOOM_STARTER_WORKSPACE_ID)
        api_log = snapshot_root / "logs" / "api.log"
        if api_log.exists():
            shutil.copyfile(api_log, self.workspace_log_service.api_log_path(BLOOM_STARTER_WORKSPACE_ID))

    def _install_preview(self) -> None:
        preview = PreviewRecord(
            workspace_id=BLOOM_STARTER_WORKSPACE_ID,
            status="stopped",
            stage="idle",
            runtime_mode=self.settings.preview_runtime_mode,
            logs=[
                "Bloom Atelier starter workspace installed.",
                "Preview runtime will start when the workspace is opened or rebuilt.",
            ],
        )
        self.store.upsert("previews", BLOOM_STARTER_WORKSPACE_ID, preview.model_dump(mode="json"))


def _starter_runs(workspace_id: str) -> list[RunRecord]:
    base = datetime(2026, 5, 13, 23, 46, 32, tzinfo=timezone.utc)
    return [
        _run(
            workspace_id=workspace_id,
            run_id="run_manual_bloom_create_1f87189460",
            created_at=base,
            intent="create",
            summary="Создан цветочный магазин, каталог, checkout и общий FastAPI/SQLite API.",
            prompt=(
                "У меня небольшой цветочный магазин, хочу сделать удобное мини-приложение для Telegram, "
                "чтобы люди могли сразу открыть каталог, выбрать красивый букет, увидеть цену, добавить в корзину, "
                "оформить доставку и потом смотреть статус заказа. Еще нужно, чтобы флорист и управляющий видели те же заказы."
            ),
            touched_files=[
                "miniapp/app/routes/flower_shop.py",
                "miniapp/app/static/shared/shop.css",
                "miniapp/app/static/shared/shop.js",
                "miniapp/app/static/client/index.html",
                "miniapp/app/static/client/catalog/index.html",
                "miniapp/app/static/client/cart/index.html",
                "miniapp/app/static/client/orders/index.html",
                "miniapp/app/static/client/order/index.html",
                "miniapp/app/generated/route_manifest.json",
                "miniapp/tests/test_flower_shop.py",
            ],
            total_tokens=408_540,
            input_tokens=382_600,
            output_tokens=25_940,
            reasoning_tokens=8_120,
            turns=9,
            activity=[
                ("activity", "Проанализировал задачу предпринимателя: нужен Telegram-магазин цветов с каталогом, корзиной и связанными заказами."),
                ("tool", "Создал FastAPI routes для shop state, bouquets, checkout, orders, inventory и analytics."),
                ("tool", "Собрал клиентские страницы /client, /client/catalog, /client/cart, /client/orders и /client/order."),
                ("tool", "Добавил SQLite persistence для букетов, заказов, событий timeline и остатков."),
                ("check", "Запустил backend workflow smoke: checkout создает заказ и timeline."),
                ("check", "Проверил, что клиент видит заказ после reload через общий API."),
                ("activity", "Подготовил apply result и сохранил workspace source."),
            ],
        ),
        _run(
            workspace_id=workspace_id,
            run_id="run_manual_bloom_edit_5029eed69c",
            created_at=base + timedelta(minutes=4),
            intent="edit",
            summary="Добавлены многостраничные роли, manager controls и florist workflow.",
            prompt=(
                "Добавьте, пожалуйста, отдельные рабочие экраны для моей команды. Покупатель должен видеть витрину, корзину и заказы. "
                "Флорист должен видеть очередь заказов, принимать заказ в работу, отмечать сборку, замены и готовность. "
                "Управляющий должен видеть каталог, заказы, проблемы, остатки и выручку."
            ),
            touched_files=[
                "miniapp/app/routes/flower_shop.py",
                "miniapp/app/static/shared/shop.css",
                "miniapp/app/static/shared/app_helpers.js",
                "miniapp/app/static/specialist/index.html",
                "miniapp/app/static/specialist/queue/index.html",
                "miniapp/app/static/specialist/order/index.html",
                "miniapp/app/static/specialist/stock/index.html",
                "miniapp/app/static/specialist/app.js",
                "miniapp/app/static/manager/index.html",
                "miniapp/app/static/manager/catalog/index.html",
                "miniapp/app/static/manager/orders/index.html",
                "miniapp/app/static/manager/analytics/index.html",
                "miniapp/app/static/manager/app.js",
            ],
            total_tokens=264_940,
            input_tokens=246_180,
            output_tokens=18_760,
            reasoning_tokens=5_480,
            turns=6,
            activity=[
                ("activity", "Разделил продукт на client, specialist и manager surfaces."),
                ("tool", "Добавил страницы очереди флориста, склада, каталога менеджера и аналитики."),
                ("check", "Проверил, что PATCH заказа меняет статус для всех ролей через общий API."),
            ],
        ),
        _run(
            workspace_id=workspace_id,
            run_id="run_manual_bloom_fix_064fc92700",
            created_at=base + timedelta(minutes=8),
            intent="refine",
            summary="Отполированы мобильная верстка, человекочитаемые статусы, timeline и acceptance checks.",
            prompt=(
                "Нужно довести приложение до нормального вида перед показом. Сделайте больше воздуха сверху, чтобы ничего не прилипало "
                "к верхней панели Telegram. Статусы должны быть понятными человеческими словами, без странных английских кодов. "
                "Добавьте историю по заказу, чтобы клиент видел шаги: заказ создан, флорист собирает, готово, передано курьеру. "
                "Проверьте все страницы, чтобы нигде не было ошибок загрузки, а карточки букетов выглядели красиво с настоящими фотографиями цветов."
            ),
            touched_files=[
                "miniapp/app/static/shared/shop.css",
                "miniapp/app/static/client/styles.css",
                "miniapp/app/static/specialist/styles.css",
                "miniapp/app/static/manager/styles.css",
                "miniapp/app/static/assets/bouquets/bouquet-1.jpg",
                "miniapp/app/static/assets/bouquets/bouquet-2.jpg",
                "miniapp/app/static/assets/bouquets/bouquet-3.jpg",
                "miniapp/app/static/assets/bouquets/bouquet-4.jpg",
                "miniapp/app/static/assets/bouquets/bouquet-5.jpg",
                "miniapp/app/static/assets/bouquets/bouquet-6.jpg",
                "miniapp/app/static/assets/bouquets/bouquet-7.jpg",
                "miniapp/app/static/assets/bouquets/bouquet-8.jpg",
                "miniapp/app/static/assets/bouquets/bouquet-9.jpg",
                "miniapp/app/static/shared/shop.js",
                "miniapp/app/routes/flower_shop.py",
                "miniapp/tests/test_flower_shop.py",
            ],
            total_tokens=212_300,
            input_tokens=198_420,
            output_tokens=13_880,
            reasoning_tokens=4_260,
            turns=5,
            activity=[
                ("activity", "Увеличил верхние отступы под Telegram chrome и выровнял mobile cards."),
                ("tool", "Подключил реальные изображения букетов и стилизовал быстрые фильтры."),
                ("check", "Browser smoke прошел client, specialist и manager pages без ошибок загрузки."),
            ],
        ),
    ]


def _run(
    *,
    workspace_id: str,
    run_id: str,
    created_at: datetime,
    intent: Literal["create", "edit", "refine"],
    summary: str,
    prompt: str,
    touched_files: list[str],
    total_tokens: int,
    input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
    turns: int,
    activity: list[tuple[str, str]],
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        workspace_id=workspace_id,
        prompt=prompt,
        intent=intent,
        target_role_scope=["client", "specialist", "manager"],
        generation_mode=GenerationMode.BALANCED,
        status="completed",
        apply_status="applied",
        draft_status="approved",
        draft_ready=True,
        iteration_count=1,
        current_stage="completed",
        progress_percent=100,
        summary=summary,
        checks_summary=RunChecksSummary(validators="passed", build="passed", preview="passed", gate_status="passed", issues=[]),
        touched_files=touched_files,
        role_coverage={"client": "covered", "specialist": "covered", "manager": "covered"},
        generated_tests={"generated_app_python_tests": "passed", "browser_flow_smoke": "passed"},
        token_usage={
            "total_tokens": total_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "turns": turns,
        },
        agent_activity_events=[
            {
                "type": event_type,
                "message": message,
                "created_at": (created_at + timedelta(seconds=index * 24)).isoformat(),
            }
            for index, (event_type, message) in enumerate(activity)
        ],
        apply_result={"status": "applied", "changed_files": touched_files},
        preview_refresh_status="passed",
        created_at=created_at,
        updated_at=created_at + timedelta(minutes=3, seconds=20),
    )
